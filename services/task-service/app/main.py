"""
task-service/app/main.py
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from sqlalchemy import text
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from app.config import settings
from app.database import engine, Base
from app.routes.tasks import router as tasks_router, close_sender
from app.routes.projects import router as projects_router
from app.telemetry import init_telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    MIGRATION STRATEGY (identical to user-service):
    Alembic runs via entrypoint.sh BEFORE uvicorn starts, so the schema
    is already at head by the time this function runs. The create_all()
    below is a dev fallback for running uvicorn directly without entrypoint.sh.
    """
    logger.info("Task Service starting...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database ready")
    init_telemetry(app, service_name="task-service", db_engine=engine)
    yield
    await close_sender()
    await engine.dispose()
    logger.info("Task Service stopped")


app = FastAPI(
    title="Task Service",
    version="1.0.0",
    docs_url="/docs" if settings.environment != "production" else None,
    lifespan=lifespan,
)

# SECURITY: No CORSMiddleware on internal services. See user-service comment.
app.include_router(tasks_router,    prefix="/api/v1/tasks",    tags=["Tasks"])
app.include_router(projects_router, prefix="/api/v1/projects", tags=["Projects"])


@app.get("/health")
async def liveness() -> dict:
    return {"status": "ok", "service": "task-service"}


@app.get("/health/ready")
async def readiness() -> dict:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as exc:
        logger.error("Database health check failed: %s", exc)
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection unavailable",
        )


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
