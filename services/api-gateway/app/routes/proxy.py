"""
api-gateway/app/routes/proxy.py

Transparent reverse proxy with hardened header handling.

SECURITY DESIGN:
  - Gateway-controlled headers (X-User-Id, X-Forwarded-*) are STRIPPED from
    client requests before we set them ourselves. This prevents identity
    spoofing via case-variant header injection (e.g. client sends lowercase
    "x-user-id" which becomes a distinct key in a case-sensitive dict, then
    gets forwarded alongside the gateway's Title-Case version — the backend's
    case-insensitive header matching may pick the attacker's value).
  - The "Host" header is never forwarded from the client. Forwarding Host
    allows attackers to poison backend redirects, CORS checks, and virtual
    host routing.
  - X-Forwarded-For uses APPEND semantics (preserves the full IP chain)
    rather than overwrite (which discards load balancer IP history).
  - request.client can be None in some ASGI transports; guarded throughout.
"""
import logging
from fastapi import APIRouter, Request, Response, HTTPException
import httpx

from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Map URL prefixes to upstream service URLs.
# Built once at module load from validated config — no runtime mutation.
ROUTE_MAP = {
    "/api/v1/users":         settings.user_service_url,
    "/api/v1/auth":          settings.user_service_url,
    "/api/v1/tasks":         settings.task_service_url,
    "/api/v1/projects":      settings.task_service_url,
    "/api/v1/notifications": settings.notification_service_url,
}

# Standard hop-by-hop headers that must never be forwarded.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailers", "upgrade", "proxy-authorization",
})

# Headers that the GATEWAY sets exclusively, based on validated JWT claims or
# its own networking context.  Any client-supplied value for these must be
# stripped BEFORE we forward the request, otherwise a client can inject
# spoofed identity or routing metadata into the backend.
_GATEWAY_OWNED = frozenset({
    "x-user-id",
    "x-user-email",
    "x-user-roles",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-original-url",    # NGINX/IIS header — backends may use for routing
    "x-rewrite-url",     # IIS header — same risk
    "host",              # Always set to upstream host, never forward client's
})


async def _get_client() -> httpx.AsyncClient:
    """Return the shared HTTP client from app state."""
    from app.main import http_client
    if http_client is None:
        raise HTTPException(status_code=503, detail="Gateway not ready")
    return http_client


def _resolve_upstream(path: str) -> tuple[str, str]:
    """
    Map a request path to its upstream base URL.
    Returns (upstream_base_url, upstream_path).
    Raises 404 if no route matches.
    """
    for prefix, upstream in ROUTE_MAP.items():
        if path.startswith(prefix):
            return upstream, path
    raise HTTPException(status_code=404, detail=f"No route for path: {path}")


@router.api_route(
    "/api/v1/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy(full_path: str, request: Request) -> Response:
    """Catch-all proxy: strip unsafe headers, inject trusted ones, forward."""
    path = f"/api/v1/{full_path}"
    upstream_url, upstream_path = _resolve_upstream(path)

    # Build target URL (including query string if present)
    target = f"{upstream_url}{upstream_path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    # ── Strip ALL client headers that are either hop-by-hop or gateway-owned ──
    # This is the critical defence against header injection attacks. We rebuild
    # the header dict from scratch rather than trusting client-supplied values
    # for any key the gateway controls.
    forward_headers: dict[str, str] = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in _GATEWAY_OWNED
    }

    # ── X-Forwarded-For: append (don't overwrite) ────────────────────────────
    # Overwriting discards the real client IP chain when there's an upstream LB.
    client_ip = request.client.host if request.client else None
    if client_ip:
        prior_xff = request.headers.get("x-forwarded-for", "")
        forward_headers["X-Forwarded-For"] = (
            f"{prior_xff}, {client_ip}" if prior_xff else client_ip
        )

    forward_headers["X-Forwarded-Proto"] = request.url.scheme
    forward_headers["X-Forwarded-Host"] = request.url.hostname or ""

    # ── Inject gateway-validated identity headers ─────────────────────────────
    # Only set if the JWT middleware populated request.state. Using getattr with
    # a default avoids AttributeError if state was never set (exempt paths).
    if user_id := getattr(request.state, "user_id", None):
        forward_headers["X-User-Id"] = str(user_id)
    if user_email := getattr(request.state, "user_email", None):
        forward_headers["X-User-Email"] = user_email
    if user_roles := getattr(request.state, "user_roles", None):
        forward_headers["X-User-Roles"] = ",".join(user_roles) if user_roles else ""

    body = await request.body()
    client = await _get_client()

    try:
        upstream_response = await client.request(
            method=request.method,
            url=target,
            headers=forward_headers,
            content=body,
        )
    except httpx.ConnectError:
        logger.error("Upstream unreachable: %s", target)
        raise HTTPException(status_code=503, detail="Upstream service unavailable")
    except httpx.TimeoutException:
        logger.error("Upstream timeout: %s", target)
        raise HTTPException(status_code=504, detail="Upstream service timed out")

    # Strip hop-by-hop headers from upstream response before returning
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
