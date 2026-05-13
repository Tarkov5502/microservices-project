"""
Async SQLAlchemy database engine + session factory.

Key concepts:
  - async engine: non-blocking DB calls (no thread pool needed)
  - Session: unit of work — groups DB operations into a transaction
  - get_db(): FastAPI dependency that provides a session per request
"""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import settings
from app.models import Base

# asyncpg is the async PostgreSQL driver (much faster than psycopg2 for async)
# We convert postgresql:// → postgresql+asyncpg:// for the async engine
async_url = settings.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    async_url,
    echo=settings.environment == "development",  # Log SQL in dev only
    # All four pool knobs are driven from settings so each environment can
    # right-size them. The defaults (10 / 20) target a 30-conn ceiling per
    # process, which multiplied by the HPA replica count gives the peak
    # concurrent connections to Postgres. Dev should drop these to 5 / 5
    # against a B1ms server (≈50-conn cap), prod can raise them on a
    # GP_Standard sku.
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle,  # Recycle stale connections
    pool_pre_ping=True,                     # Test connection before each use
    pool_timeout=settings.db_pool_timeout,  # Fail fast under exhaustion
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,  # Keep objects usable after commit
)


async def get_db() -> AsyncSession:
    """
    FastAPI dependency — yields a DB session for a single request.
    The session is automatically committed/rolled back and closed.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
