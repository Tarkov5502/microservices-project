"""
alembic/env.py — Alembic environment configuration for user-service.

This file connects Alembic to the application's SQLAlchemy models and
async database engine. It must run migrations synchronously (Alembic's
core API is sync) even though the app uses async SQLAlchemy.

The standard pattern is to use run_sync() inside an async context to
bridge the two worlds.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import the app's models so Alembic knows what tables to manage.
# The 'target_metadata' below tells --autogenerate which models to diff against.
from app.models import Base
from app.config import settings

# this is the Alembic Config object — gives access to values in alembic.ini
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── THIS is how Alembic knows about your tables ─────────────────────────────
# autogenerate compares this metadata against the real DB schema to produce
# the upgrade/downgrade steps in each new migration file.
target_metadata = Base.metadata

# Pull database URL from app config (never from alembic.ini — no credentials in VCS)
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — generates SQL scripts without
    a live DB connection. Useful for review or applying via DBA.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations using an async engine.
    Alembic's internals are synchronous so we use run_sync() to bridge.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # No connection pool — migrations run once
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live database."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
