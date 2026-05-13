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
    pool_size=10,          # Keep 10 persistent connections
    max_overflow=20,       # Allow 20 extra connections at peak
    pool_recycle=3600,     # Recycle connections after 1 hour
    pool_pre_ping=True,    # Test connection before using (handles DB restarts)
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
