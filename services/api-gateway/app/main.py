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
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from app.config import settings
from app.routes.proxy import router as proxy_router
from app.middleware.auth import JWTAuthMiddleware
from app.middleware.rate_limiter import RateLimiterMiddleware

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


# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="API Gateway",
    description="Central entry point for the microservices platform",
    version="1.0.0",
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)

# ─── Middleware Stack (order matters — last added = first executed) ────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimiterMiddleware, max_requests=100, window_seconds=60)
app.add_middleware(JWTAuthMiddleware, exempt_paths=["/health", "/health/ready", "/metrics"])

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

    # Add timing header so clients can see how long the gateway took
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
    # Check connectivity to backend services
    services = {
        "user-service": settings.user_service_url,
        "task-service": settings.task_service_url,
    }
    unhealthy = []
    for name, url in services.items():
        try:
            resp = await http_client.get(f"{url}/health", timeout=5.0)
            if resp.status_code != 200:
                unhealthy.append(name)
        except Exception:
            unhealthy.append(name)

    if unhealthy:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "degraded", "unhealthy": unhealthy},
        )
    return {"status": "ready", "services": list(services.keys())}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
