"""
001_initial_schema.py — Initial migration: create users table.

Revision: 001
Down revision: None (this is the first migration)

HOW TO READ THIS FILE:
  upgrade()   = forward: apply this change (run on 'alembic upgrade head')
  downgrade() = reverse: undo this change (run on 'alembic downgrade -1')

WHY NOT JUST USE create_all()?
  See alembic.ini for the full explanation. Short version: create_all() is
  blind to schema changes after initial creation. This file IS the create_all
  replacement — Alembic runs it once, marks it applied, and never runs it again.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers used by Alembic
revision: str = "001_initial"
down_revision = None      # First migration — no predecessor
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True, index=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True, index=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"),
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
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("users")
