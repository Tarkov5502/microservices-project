"""
SQLAlchemy models — maps Python classes to database tables.

SQLAlchemy ORM: instead of writing raw SQL, you define Python classes.
The library generates SQL and handles connection pooling.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Boolean, DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """All models inherit from this — provides the metadata registry."""
    pass


class User(Base):
    """
    The users table. Each row = one user account.

    Mapped[] + mapped_column() is SQLAlchemy 2.0 style — fully typed!
    """
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,   # Auto-generate UUID on insert
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)

    # NEVER store plaintext passwords. Store bcrypt hashes only!
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # server_default: the DB sets this, not the application
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Email verification state. We don't BLOCK login on email_verified=False
    # today — a real product decision should determine whether unverified
    # accounts can do anything. We track the flag and timestamp so future
    # business logic can branch on it, and so audit log entries can carry it.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email}>"
