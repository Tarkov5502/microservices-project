"""
001_initial_schema.py — Initial migration: create projects and tasks tables.

Revision: 001_initial
Down revision: None (first migration)

WHY TWO TABLES IN ONE MIGRATION?
  Projects must exist before tasks because tasks.project_id is a foreign key
  to projects.id. A single migration guarantees they're created in the right
  order. If these were separate migrations, an accidental downgrade of 002
  (tasks) would leave an orphaned projects table — clean, but if you also
  downgraded 001, the FK constraint would have prevented partial state.

  Keeping them together also means 'alembic downgrade base' cleanly removes
  both tables in one operation.

ENUM TYPES:
  PostgreSQL native ENUMs are created separately and referenced by the columns.
  SQLAlchemy's create_constraint=False skips the check constraint (handled by
  the native ENUM type's constraint in PG). The ENUM types are dropped in
  downgrade() before the tables that reference them.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from alembic import op


revision: str = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create ENUM types first — tables will reference them
    task_status = sa.Enum(
        "todo", "in_progress", "in_review", "done", "cancelled",
        name="taskstatus",
    )
    task_priority = sa.Enum(
        "low", "medium", "high", "critical",
        name="taskpriority",
    )
    task_status.create(op.get_bind(), checkfirst=True)
    task_priority.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", task_status, nullable=False, server_default="todo"),
        sa.Column("priority", task_priority, nullable=False, server_default="medium"),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("creator_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("assignee_id", UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("tasks")
    op.drop_table("projects")
    # Drop ENUM types after tables that reference them
    sa.Enum(name="taskpriority").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="taskstatus").drop(op.get_bind(), checkfirst=True)
