"""
002_email_verification.py — Add email_verified + email_verified_at columns.

Revision:        002_email_verification
Down revision:   001_initial
"""
from alembic import op
import sqlalchemy as sa


revision: str = "002_email_verification"
down_revision: str | None = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "email_verified",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email_verified")
