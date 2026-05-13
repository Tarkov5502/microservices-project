"""
api-gateway/app/routes/proxy.py

Transparent reverse proxy with:
  - Hardened header handling (identity injection, hop-by-hop stripping)
  - Circuit breaker per upstream service (fast-fail on repeated failures)
  - Retry with exponential backoff for transient errors (GET only)
  - X-Request-ID propagation for end-to-end request tracing
  - Upstream-name tagging for structured logging

RESILIENCE DESIGN:
┌─────────────────────────────────────────────────────────────────┐
│  Client Request                                                  │
│       │                                                          │
│  ┌────▼────────────────┐                                        │
│  │  Circuit Breaker    │  OPEN → immediate 503 (no network I/O) │
│  │  (per upstream)     │                                        │
│  └────┬────────────────┘                                        │
│       │ CLOSED or HALF_OPEN                                      │
│  ┌────▼────────────────┐                                        │
│  │  HTTP Request       │  with X-Request-ID, X-User-Id, etc.   │
│  └────┬────────────────┘                                        │
│       │                                                          │
│  ┌────▼────────────────┐  502/503/504 or ConnectError?          │
│  │  Retry (GET only)   │──► wait backoff_secs → retry           │
│  │  max 3 attempts     │  Non-GET? Return immediately           │
│  └────┬────────────────┘                                        │
│       │ success or exhausted retries                            │
│  ┌────▼────────────────┐                                        │
│  │  Record result      │  success → breaker.record_success()    │
│  │  in Circuit Breaker │  failure → breaker.record_failure()    │
│  └─────────────────────┘                                        │
└─────────────────────────────────────────────────────────────────┘

RETRY SAFETY:
  Retries ONLY apply to GET requests. POST/PATCH/DELETE are NOT retried
  because they are not guaranteed idempotent. Retrying a "create task"
  could produce duplicate resources. GET requests are safe to retry by
  HTTP spec — the response must be the same for the same request state.

  Retry-able responses: 502 Bad Gateway, 503 Service Unavailable,
                        504 Gateway Timeout, and connection-level errors.
  Non-retryable: 4xx (client errors), 500 (server bugs — not transient).

BACKOFF SCHEDULE:
  Attempt 1: immediate
  Attempt 2: 0.1s delay
  Attempt 3: 0.2s delay  (total max wait: 0.3s for transient blips)

HEADER SECURITY:
  See original security docstring — unchanged from prior version.
"""
import asyncio
import logging
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import StreamingResponse
import httpx

from app.config import settings
from app.circuit_breaker import registry as cb_registry

logger = logging.getLogger(__name__)
router = APIRouter()

ROUTE_MAP = {
    "/api/v1/users":         settings.user_service_url,
    "/api/v1/auth":          settings.user_service_url,
    "/api/v1/tasks":         settings.task_service_url,
    "/api/v1/projects":      settings.task_service_url,
    "/api/v1/notifications": settings.notification_service_url,
}

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailers", "upgrade", "proxy-authorization",
})

_GATEWAY_OWNED = frozenset({
    "x-user-id", "x-user-email", "x-user-roles",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    "x-original-url", "x-rewrite-url", "host",
    "x-http-method-override", "x-method-override", "x-http-method",
    # Note: x-request-id is NOT in this set — we preserve the client's value
    # (after sanitising it in CorrelationMiddleware) or use the generated one.
})

# Retry only on these response codes — all indicate transient infrastructure issues.
_RETRYABLE_STATUS = frozenset({502, 503, 504})

# Backoff delays in seconds between retry attempts (attempt 1 is immediate).
_BACKOFF_SCHEDULE = [0.0, 0.1, 0.2]
_MAX_ATTEMPTS = len(_BACKOFF_SCHEDULE)


async def _get_client() -> httpx.AsyncClient:
    from app.main import http_client
    if http_client is None:
        raise HTTPException(status_code=503, detail="Gateway not ready")
    return http_client


def _resolve_upstream(path: str) -> tuple[str, str, str]:
    """Returns (upstream_base_url, upstream_path, service_name)."""
    for prefix, upstream in ROUTE_MAP.items():
        if path.startswith(prefix):
            # Derive a simple service name for logging/circuit-breaker key
            service_name = upstream.split("//")[-1].split(":")[0]
            return upstream, path, service_name
    raise HTTPException(status_code=404, detail=f"No route for path: {path}")


def _build_forward_headers(request: Request) -> dict[str, str]:
    """
    Build the header dict to forward upstream.
    Strips hop-by-hop and gateway-owned headers from client input,
    then injects gateway-authoritative values.
    """
    headers: dict[str, str] = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in _GATEWAY_OWNED
    }

    # X-Forwarded-For: append (not overwrite) to preserve full chain
    client_ip = request.client.host if request.client else None
    if client_ip:
        prior = request.headers.get("x-forwarded-for", "")
        headers["X-Forwarded-For"] = f"{prior}, {client_ip}" if prior else client_ip

    headers["X-Forwarded-Proto"] = request.url.scheme
    headers["X-Forwarded-Host"] = request.url.hostname or ""

    # Inject gateway-validated identity (from JWT middleware)
    if user_id := getattr(request.state, "user_id", None):
        headers["X-User-Id"] = str(user_id)
    if user_email := getattr(request.state, "user_email", None):
        headers["X-User-Email"] = user_email
    if user_roles := getattr(request.state, "user_roles", None):
        headers["X-User-Roles"] = ",".join(user_roles) if user_roles else ""

    # Propagate correlation ID — threads this request through all service logs
    if request_id := getattr(request.state, "request_id", None):
        headers["X-Request-ID"] = request_id

    return headers


async def _dispatch_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    service_name: str,
) -> httpx.Response:
    """
    Execute HTTP request with circuit breaker guard and conditional retry.

    Raises HTTPException on terminal failure (open circuit, exhausted retries).
    Returns upstream response on success.
    """
    breaker = cb_registry.get(service_name)

    # Fast-fail: don't touch the network if circuit is open
    if breaker.is_open():
        logger.warning(
            "Circuit breaker [%s] is OPEN — rejecting request immediately "
            "(upstream has been failing; will probe again after cooldown)",
            service_name,
        )
        raise HTTPException(
            status_code=503,
            detail=f"Service '{service_name}' is temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "30"},
        )

    is_idempotent = method.upper() == "GET"
    attempts = _MAX_ATTEMPTS if is_idempotent else 1
    last_exc: Exception | None = None

    for attempt in range(attempts):
        if attempt > 0:
            delay = _BACKOFF_SCHEDULE[attempt]
            logger.info(
                "Retry %d/%d for %s %s (backoff: %.1fs)",
                attempt + 1, attempts, method, url, delay,
            )
            await asyncio.sleep(delay)

        try:
            response = await client.request(
                method=method, url=url, headers=headers, content=body
            )

            if response.status_code in _RETRYABLE_STATUS and is_idempotent and attempt < attempts - 1:
                # Log and retry — don't record failure yet
                logger.warning(
                    "Upstream %s returned %d (attempt %d/%d) — will retry",
                    service_name, response.status_code, attempt + 1, attempts,
                )
                last_exc = None  # Not an exception — just a bad status
                continue

            # Success or a non-retryable response
            if response.status_code < 500 or response.status_code not in _RETRYABLE_STATUS:
                breaker.record_success()
            elif response.status_code in _RETRYABLE_STATUS:
                breaker.record_failure()

            return response

        except httpx.ConnectError as exc:
            last_exc = exc
            logger.error("ConnectError to %s (attempt %d): %s", service_name, attempt + 1, exc)
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.error("Timeout to %s (attempt %d): %s", service_name, attempt + 1, exc)

    # All attempts exhausted or single non-idempotent call failed
    breaker.record_failure()
    if isinstance(last_exc, httpx.ConnectError):
        raise HTTPException(status_code=503, detail="Upstream service unreachable")
    if isinstance(last_exc, httpx.TimeoutException):
        raise HTTPException(status_code=504, detail="Upstream service timed out")
    # Got a retryable status on all attempts
    raise HTTPException(
        status_code=503,
        detail=f"Service '{service_name}' returned an error after {attempts} attempts",
    )


@router.api_route(
    "/api/v1/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy(full_path: str, request: Request) -> Response:
    """
    Catch-all proxy: strip unsafe headers → circuit breaker check →
    inject trusted headers → forward with retry → strip hop-by-hop from response.

    SSE HANDLING:
      When the client sends Accept: text/event-stream we switch to streaming
      mode. httpx.AsyncClient.stream() keeps the connection open and yields
      bytes as they arrive — exactly what SSE needs. The buffering path
      (client.request()) would wait for EOF before returning, causing an
      infinite hang on a keep-alive stream.
    """
    path = f"/api/v1/{full_path}"
    upstream_url, upstream_path, service_name = _resolve_upstream(path)

    target = f"{upstream_url}{upstream_path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    forward_headers = _build_forward_headers(request)
    body = await request.body()
    client = await _get_client()

    # ── SSE streaming path ──────────────────────────────────────────────────
    is_sse = request.headers.get("accept", "").startswith("text/event-stream")
    if is_sse and request.method.upper() == "GET":
        breaker = cb_registry.get(service_name)
        if breaker.is_open():
            raise HTTPException(status_code=503, detail=f"Service '{service_name}' unavailable")

        async def _sse_generator():
            """Proxy an SSE stream from upstream, forwarding bytes verbatim."""
            try:
                async with client.stream(
                    "GET", target, headers=forward_headers, timeout=None
                ) as resp:
                    breaker.record_success()
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            except Exception as exc:
                breaker.record_failure()
                logger.error("SSE stream error from %s: %s", service_name, exc)

        return StreamingResponse(
            _sse_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ── Standard (buffered) proxy path ─────────────────────────────────────
    upstream_response = await _dispatch_with_retry(
        client=client,
        method=request.method,
        url=target,
        headers=forward_headers,
        body=body,
        service_name=service_name,
    )

    response_headers = {
        k: v
        for k, v in upstream_response.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
