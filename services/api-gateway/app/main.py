"""
api-gateway/app/main.py

The API Gateway is the single entry point for all client requests.
It validates JWTs, applies rate limiting, and proxies requests to
the appropriate backend microservice.

Architecture pattern: Gateway Aggregation / Backend for Frontend (BFF)
"""

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

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Prometheus Metrics ───────────────────────────────────────────────────────
# Metrics are automatically scraped by Prometheus via the /metrics endpoint.
# Counters: monotonically increasing (never decrease)
# Histograms: measure distribution (latency buckets)
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
# We reuse an httpx.AsyncClient for all outbound requests to backend services.
# Connection pooling is critical — creating a new connection per request is slow!
http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle handler."""
    global http_client
    logger.info("API Gateway starting up...")

    # Create the shared HTTP client with connection pooling
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    logger.info("HTTP client pool initialized")
    yield  # Application runs here

    # Cleanup on shutdown
    logger.info("API Gateway shutting down...")
    await http_client.aclose()
    logger.info("HTTP client pool closed")


# ─── FastAPI App ────────────────────────────────────────────
_is_prod = settings.environment == "production"
app = FastAPI(
    title="API Gateway",
    description="Central entry point for the microservices platform",
    version="1.0.0",
    # FIX #5: Hiding /docs is not enough — /openapi.json is its own endpoint
    # and gives attackers a complete map of every route and schema.
    # Both must be disabled in production.
    docs_url="/docs" if not _is_prod else None,
    redoc_url=None,
    openapi_url="/openapi.json" if not _is_prod else None,
    lifespan=lifespan,
)

# ─── Middleware Stack (order matters — last added = first executed) ────────
#
# Execution order (innermost → outermost on request path):
#   JWTAuth → RateLimiter → CORS → SecurityHeaders → [client]
#
# Execution order on response path (reversed):
#   [route handler] → JWTAuth → RateLimiter → CORS → SecurityHeaders → [client]
#
# SecurityHeaders is OUTERMOST so it applies its headers last,
# meaning no inner middleware can accidentally override them.

# FIX #6: Apply security headers to every response.
app.add_middleware(SecurityHeadersMiddleware)

# SECURITY: Do NOT combine allow_origins=["*"] with allow_credentials=True.
# Starlette 0.37+ raises ValueError on startup for that combination.
# This gateway does not use cookie-based auth, so credentials=False is correct.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins or [],  # Must be set via env var in prod
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)
# FIX #1: Pass auth_max_requests so login/register get a tighter bucket.
app.add_middleware(
    RateLimiterMiddleware,
    max_requests=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
    auth_max_requests=settings.auth_rate_limit_requests,
    redis_url=settings.redis_url,
)
app.add_middleware(
    JWTAuthMiddleware,
    exempt_paths=["/health", "/health/ready", "/metrics"],
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

    # NOTE: X-Response-Time removed in production — response timing leaks
    # allow attackers to infer server-side behaviour (e.g. whether a user
    # exists by comparing bcrypt timing). Keep it only in non-prod.
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
    """Kubernetes readiness probe — can we handle requests?"""
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
        """Return service name if unhealthy, None if healthy."""
        try:
            resp = await http_client.get(f"{url}/health", timeout=5.0)
            return name if resp.status_code != 200 else None
        except Exception:
            return name

    # FIX: check all services concurrently, not sequentially.
    # Sequential: total time = sum(each probe timeout).
    # Concurrent: total time = max(any probe timeout).
    results = await asyncio.gather(*[_check(n, u) for n, u in services.items()])
    unhealthy = [r for r in results if r is not None]

    if unhealthy:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "degraded", "unhealthy": unhealthy},
        )
    return {"status": "ready", "services": list(services.keys())}


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    """
    Prometheus metrics endpoint.

    SECURITY: Only accessible from within the cluster. The NGINX Ingress
    Controller is configured to return 403 for external requests to /metrics
    via the 'server-snippet' annotation. This application-level guard is a
    second line of defence — check for the absence of X-Forwarded-For which
    is set by the ingress for all externally-originated requests.
    """
    if request.headers.get("x-forwarded-for"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Metrics endpoint is not accessible externally",
        )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
