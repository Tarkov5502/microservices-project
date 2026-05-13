"""
user-service/app/main.py

Handles user registration, login, profile management, and JWT issuance.
Uses PostgreSQL for persistence and Redis for session caching.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

from app.config import settings
from app.database import engine, Base
from app.routes.auth import router as auth_router
from app.routes.users import router as users_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

REQUEST_COUNT = Counter("user_service_requests_total", "Total requests", ["method", "path", "status"])
REQUEST_LATENCY = Histogram("user_service_request_duration_seconds", "Latency", ["method", "path"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("User Service starting — creating tables if needed...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ready")
    yield
    logger.info("User Service shutting down")
    await engine.dispose()


app = FastAPI(
    title="User Service",
    version="1.0.0",
    docs_url="/docs" if settings.environment != "production" else None,
    lifespan=lifespan,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
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
    except Exception as e:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
