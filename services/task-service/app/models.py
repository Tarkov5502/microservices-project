"""
task-service/app/models.py — Task and Project database models.

INDEXES — WHY COMPOUND?
  Cursor pagination queries look like:

    SELECT * FROM tasks
    WHERE creator_id = $1
    AND (created_at < $2 OR (created_at = $2 AND id < $3))
    ORDER BY created_at DESC, id DESC
    LIMIT 51;

  PostgreSQL needs to efficiently filter by creator_id, then sort/filter on
  (created_at, id) within that partition. A plain index on creator_id finds
  the right rows, but then PostgreSQL must sort them all in memory. With
  thousands of tasks this becomes a sequential scan + sort — O(N) per page.

  A compound index on (creator_id, created_at, id) lets PostgreSQL satisfy
  both the equality predicate AND the keyset inequality from a single index
  scan, making pagination O(1) regardless of dataset size.

  PostgreSQL can use an index in both forward and reverse direction, so
  (created_at ASC, id ASC) in the index definition covers ORDER BY DESC
  queries too. No need to define separate DESC indexes for this use case.

SOFT DELETE:
  Projects use a soft-delete pattern (is_active=False) rather than hard
  DELETE. The compound index includes is_active so the DB can filter active
  projects without touching inactive ones.

ON UPDATE TRIGGERS (SQLAlchemy caveat):
  updated_at uses onupdate=func.now(). SQLAlchemy's ORM onupdate fires when
  it detects a changed column in the UPDATE statement. It does NOT fire if
  you use Session.execute(update(...)) directly — only setattr() + flush().
  All mutation routes use setattr(), so this works correctly.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    String, Text, Boolean, DateTime, ForeignKey,
    Enum as SAEnum, Index, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TaskStatus(str, enum.Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskPriority(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    tasks: Mapped[list["Task"]] = relationship("Task", back_populates="project", cascade="all, delete-orphan")

    __table_args__ = (
        # Covers: WHERE owner_id=? AND is_active=TRUE ORDER BY created_at DESC, id DESC
        # list_projects queries always filter on owner_id + is_active then sort by (created_at, id).
        # This index makes cursor pagination O(1) — no full-table sort needed.
        Index(
            "ix_projects_owner_active_cursor",
            "owner_id",
            "is_active",
            "created_at",
            "id",
        ),
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(SAEnum(TaskStatus), default=TaskStatus.TODO, nullable=False)
    priority: Mapped[TaskPriority] = mapped_column(SAEnum(TaskPriority), default=TaskPriority.MEDIUM)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    creator_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    project: Mapped["Project"] = relationship("Project", back_populates="tasks")

    __table_args__ = (
        # Covers: WHERE creator_id=? [AND project_id=?] [AND status=?]
        #         ORDER BY created_at DESC, id DESC
        # The leading creator_id column matches the equality filter. PostgreSQL
        # then uses the compound tail (created_at, id) for the keyset ORDER/WHERE.
        Index(
            "ix_tasks_creator_cursor",
            "creator_id",
            "created_at",
            "id",
        ),
        # Same shape for assignee lookups. list_tasks shows tasks WHERE
        # creator_id=? OR assignee_id=? — both branches benefit from their own index.
        # PostgreSQL will BitmapOr the two index scans together.
        Index(
            "ix_tasks_assignee_cursor",
            "assignee_id",
            "created_at",
            "id",
        ),
    )
