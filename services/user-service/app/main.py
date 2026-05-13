"""
user-service/app/main.py

Handles user registration, login, profile management, and JWT issuance.
Uses PostgreSQL for persistence and Redis for session caching.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from sqlalchemy import select, text
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from app.config import settings
from app.security_headers import SecurityHeadersMiddleware
from app.identity_signing import IdentityVerifierMiddleware
from app.database import AsyncSessionLocal, engine, Base
from app.models import User
from app.routes.auth import router as auth_router
from app.routes.users import router as users_router
from app.telemetry import init_telemetry
from app.redis_client import get_redis, close_redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def _bootstrap_initial_admin() -> None:
    """
    Promote the user identified by INITIAL_ADMIN_EMAIL to admin, if set.

    SOLVES THE FIRST-ADMIN CHICKEN/EGG PROBLEM:
      The /admin/users/{id}/promote endpoint requires the caller to already
      be an admin. On a fresh deployment there are zero admins, so the route
      is unreachable. This function bridges the gap.

    BEHAVIOUR:
      - If INITIAL_ADMIN_EMAIL is unset → no-op. Production should leave it
        unset after first use.
      - If the env var is set but no user with that email exists yet → log a
        notice and continue. The next startup (after they register) will
        promote them.
      - If the user exists and is already admin → no-op.
      - If the user exists and is not admin → set is_admin=True and commit.

    The bootstrap user still has to register through the normal flow first
    so a real bcrypt hash gets stored. This function NEVER creates accounts;
    it only flips a flag on an existing one.
    """
    email = settings.initial_admin_email
    if not email:
        return

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()
            if user is None:
                logger.info(
                    "INITIAL_ADMIN_EMAIL=%s is set but no such user exists yet — "
                    "register the account, then restart this service to promote it.",
                    email,
                )
                return
            if user.is_admin:
                logger.info("Initial admin %s already promoted — no action taken.", email)
                return
            user.is_admin = True
            await session.commit()
            logger.warning(
                "Promoted %s to admin via INITIAL_ADMIN_EMAIL bootstrap. "
                "Unset this env var now to prevent accidental future re-promotion.",
                email,
            )
    except Exception as exc:
        # Failure to bootstrap an admin must not prevent the service from
        # starting — the rest of the API still works for normal users.
        logger.error("Initial admin bootstrap failed: %s", exc)


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
    await _bootstrap_initial_admin()
    await get_redis()  # Establish Redis connection at startup (non-fatal if unavailable)
    init_telemetry(app, service_name="user-service", db_engine=engine)
    yield
    logger.info("User Service shutting down")
    await close_redis()
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
# Defence-in-depth security headers. Applied to every response so that
# even if NetworkPolicy is misconfigured and a browser reaches this
# service directly, our responses are still hardened. Cheap and additive.
app.add_middleware(SecurityHeadersMiddleware)

# Verify that X-User-* headers were signed by the gateway. Reject any
# request that claims an identity without a valid signature. Health
# and metrics endpoints are exempt because they're hit by kubelet /
# Prometheus, not via the gateway.
app.add_middleware(
    IdentityVerifierMiddleware,
    secret=settings.interservice_hmac_secret,
    exempt_paths=["/health", "/health/ready", "/metrics"],
)

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
