"""
user-service/app/main.py

Handles user registration, login, profile management, and JWT issuance.
Uses PostgreSQL for persistence and Redis for session caching.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from sqlalchemy import text
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from app.config import settings
from app.database import engine, Base
from app.routes.auth import router as auth_router
from app.routes.users import router as users_router
from app.telemetry import init_telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    MIGRATION STRATEGY:
    We run 'alembic upgrade head' via the entrypoint.sh script BEFORE uvicorn
    starts. That means by the time this lifespan function runs, the schema is
    already at the correct version.

    The create_all() call below is a FALLBACK for local development without
    the entrypoint script (e.g., running 'uvicorn app.main:app' directly).
    In production (Docker/K8s), entrypoint.sh runs Alembic first, so this
    create_all() is a no-op (tables already exist).

    This dual approach means:
      - Local dev: works without running alembic manually
      - Production: Alembic runs first, schema is versioned and auditable
    """
    logger.info("User Service starting...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database ready")
    init_telemetry(app, service_name="user-service", db_engine=engine)
    yield
    logger.info("User Service shutting down")
    await engine.dispose()


app = FastAPI(
    title="User Service",
    version="1.0.0",
    docs_url="/docs" if settings.environment != "production" else None,
    lifespan=lifespan,
)

# SECURITY: Internal services must NOT expose CORS headers. They are only
# reachable from the API Gateway inside the cluster via NetworkPolicy.
# Enabling CORS here would allow any origin to bypass the gateway if the
# service were ever accidentally exposed.
app.include_router(auth_router,  prefix="/api/v1/auth",  tags=["Authentication"])
app.include_router(users_router, prefix="/api/v1/users", tags=["Users"])


@app.get("/health")
async def liveness() -> dict:
    return {"status": "ok", "service": "user-service"}


@app.get("/health/ready")
async def readiness() -> dict:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as exc:
        # Log the full exception internally; return a generic message to the
        # caller. Leaking DB hostnames, driver errors, or schema names in HTTP
        # responses aids attackers in fingerprinting the infrastructure.
        logger.error("Database health check failed: %s", exc)
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection unavailable",
        )


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
