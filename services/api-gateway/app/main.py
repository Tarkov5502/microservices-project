"""
api-gateway/app/main.py

The API Gateway is the single entry point for all client requests.
Validates JWTs, applies rate limiting, proxies requests to backend services.

Architecture pattern: Gateway Aggregation / Backend for Frontend (BFF)

RESILIENCE STACK (innermost → outermost on request path):
  JWTAuth → Correlation → RateLimiter → CORS → SecurityHeaders → [client]

  JWTAuth validates the token and injects user identity into request.state.
  Correlation generates or validates X-Request-ID for end-to-end tracing.
  RateLimiter enforces per-IP sliding windows, backed by Redis.
  CORS enforces origin allowlist.
  SecurityHeaders adds HSTS, CSP, X-Frame-Options to every response.

  Proxy layer (proxy.py) adds circuit breaker + retry on top of this stack.
"""
import asyncio
import time
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from app.config import settings
from app.routes.proxy import router as proxy_router
from app.middleware.auth import JWTAuthMiddleware
from app.middleware.rate_limiter import RateLimiterMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.middleware.correlation import CorrelationMiddleware
from app.circuit_breaker import registry as cb_registry
from app.telemetry import init_telemetry

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Prometheus Metrics ───────────────────────────────────────────────────────
REQUEST_COUNT = Counter(
    "gateway_requests_total",
    "Total requests processed by the API gateway",
    ["method", "path", "status_code"],
)
REQUEST_LATENCY = Histogram(
    "gateway_request_duration_seconds",
    "Request latency in seconds",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# ─── HTTP Client Pool ─────────────────────────────────────────────────────────
http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    logger.info("API Gateway starting up...")

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    logger.info("HTTP client pool initialized")

    # Initialise distributed tracing — no-op if OTEL_EXPORTER_OTLP_ENDPOINT unset
    init_telemetry(app, service_name="api-gateway", instrument_httpx=True)

    yield

    logger.info("API Gateway shutting down...")
    await http_client.aclose()
    logger.info("HTTP client pool closed")


# ─── FastAPI App ───────────────────────────────────────────────────────────────
_is_prod = settings.environment == "production"
app = FastAPI(
    title="API Gateway",
    description="Central entry point for the microservices platform",
    version="1.0.0",
    docs_url="/docs" if not _is_prod else None,
    redoc_url=None,
    openapi_url="/openapi.json" if not _is_prod else None,
    lifespan=lifespan,
)

# ─── Middleware Stack ──────────────────────────────────────────────────────────
# Starlette applies middleware in LIFO order (last added = first to execute
# on the request path). Reading top-to-bottom:
#   SecurityHeaders → CORS → RateLimiter → Correlation → JWTAuth → [route]
# On response path it's reversed:
#   [route] → JWTAuth → Correlation → RateLimiter → CORS → SecurityHeaders

app.add_middleware(SecurityHeadersMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins or [],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
)

app.add_middleware(
    RateLimiterMiddleware,
    max_requests=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
    auth_max_requests=settings.auth_rate_limit_requests,
    redis_url=settings.redis_url,
)

# CorrelationMiddleware runs BEFORE JWTAuth so the request_id is available
# to the auth middleware for inclusion in audit log lines.
app.add_middleware(CorrelationMiddleware)

app.add_middleware(
    JWTAuthMiddleware,
    # Exact paths that need no token (health + metrics probes)
    exempt_paths=["/health", "/health/ready", "/metrics"],
    # Prefix exemptions: every /api/v1/auth/* route is unauthenticated by design.
    # Login/register have no token yet. Refresh carries an opaque refresh token
    # (not a JWT). Logout must work with an expired JWT so sessions can always
    # be cleanly revoked.
    exempt_prefixes=["/api/v1/auth/"],
)

# ─── Routes ───────────────────────────────────────────────────────────────────
app.include_router(proxy_router)


# ─── Middleware: Request Timing + Metrics ─────────────────────────────────────
@app.middleware("http")
async def record_metrics(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - start

    REQUEST_COUNT.labels(
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
    ).inc()
    REQUEST_LATENCY.labels(
        method=request.method,
        path=request.url.path,
    ).observe(elapsed)

    if settings.environment != "production":
        response.headers["X-Response-Time"] = f"{elapsed:.4f}s"
    return response


# ─── Health Endpoints ─────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def liveness() -> dict:
    """Kubernetes liveness probe — is the process alive?"""
    return {"status": "ok", "service": "api-gateway"}


@app.get("/health/ready", tags=["Health"])
async def readiness() -> dict:
    """
    Kubernetes readiness probe — can we handle requests?

    Reports:
      - http_client initialisation status
      - upstream service reachability (all three, in parallel)
      - circuit breaker state per upstream (CLOSED/OPEN/HALF_OPEN)
    """
    if http_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "starting", "reason": "HTTP client not yet initialized"},
        )

    services = {
        "user-service":         settings.user_service_url,
        "task-service":         settings.task_service_url,
        "notification-service": settings.notification_service_url,
    }

    async def _check(name: str, url: str) -> str | None:
        try:
            resp = await http_client.get(f"{url}/health", timeout=5.0)
            return name if resp.status_code != 200 else None
        except Exception:
            return name

    # Concurrent health checks — total time = max(single probe), not sum
    results = await asyncio.gather(*[_check(n, u) for n, u in services.items()])
    unhealthy = [r for r in results if r is not None]

    breaker_states = cb_registry.all_states()

    if unhealthy:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "degraded",
                "unhealthy": unhealthy,
                "circuit_breakers": breaker_states,
            },
        )
    return {
        "status": "ready",
        "services": list(services.keys()),
        "circuit_breakers": breaker_states,
    }


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    """
    Prometheus metrics endpoint.

    ACCESS CONTROL IS ENFORCED AT THE NETWORK LAYER, NOT HERE.
      - In Kubernetes: NetworkPolicy 'allow-prometheus-scraping' permits only
        pods in the monitoring namespace to reach :8000/metrics.
      - At the ingress: NGINX's location block strips /metrics from external
        traffic (see kubernetes/ingress/ingress.yaml).
      - Locally: docker-compose exposes :8000 directly; on a dev laptop the
        endpoint is intentionally reachable for inspection.

    A previous version of this handler used the presence of X-Forwarded-For
    as a "this came from outside" signal. That was fragile in two directions:
      1. Any well-behaved internal proxy that sets XFF (e.g. a service mesh
         sidecar, or a future shared egress proxy) would be incorrectly
         blocked.
      2. An attacker doesn't get to control whether they're allowed in based
         on a header they can also set — so the check has never been an
         actual security boundary.

    Network-level controls are the correct place to enforce this.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
