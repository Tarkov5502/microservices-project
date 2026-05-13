"""
002_compound_indexes.py — Add compound indexes for cursor pagination.

Revision: 002_compound_indexes
Down revision: 001_initial

WHY THESE INDEXES?
  Cursor pagination queries use keyset filtering — they filter on a leading
  equality column (creator_id, assignee_id, owner_id) then apply an
  inequality on (created_at, id) to find the next page. Example:

    SELECT * FROM tasks
    WHERE creator_id = $1
      AND (
        created_at < $2
        OR (created_at = $2 AND id < $3)
      )
    ORDER BY created_at DESC, id DESC
    LIMIT 51;

  Without these indexes, PostgreSQL must:
    1. Index scan on creator_id             → N rows
    2. Sort all N rows by (created_at, id)  → O(N log N)
    3. Scan from the top to find the page   → O(N)

  With a compound index on (creator_id, created_at, id), PostgreSQL can:
    1. Seek directly to the creator_id partition in the index
    2. Walk the (created_at, id) sub-index to find the keyset position
    3. Scan forward exactly limit+1 rows

  This is O(log N + page_size) regardless of how many tasks the user has.
  The difference is invisible at 100 rows and catastrophic at 100,000.

BITMAP INDEX SCAN (assignee OR creator):
  list_tasks filters: WHERE creator_id=? OR assignee_id=?
  PostgreSQL satisfies this with a BitmapOr of two index scans:
    - Scan ix_tasks_creator_cursor for creator_id rows
    - Scan ix_tasks_assignee_cursor for assignee_id rows
    - Union the result sets in a bitmap
  Both indexes must exist for this to work efficiently.

INDEX ORDER vs QUERY ORDER:
  The index is defined (created_at ASC, id ASC). PostgreSQL can scan an
  index in reverse order at zero extra cost, so ORDER BY created_at DESC, id
  DESC is equally efficient. No need for separate DESC indexes.

PARTIAL INDEX NOTE:
  A partial index WHERE is_active=TRUE on projects would be even smaller
  and faster, but it only helps queries that filter on is_active. Since
  some admin queries intentionally list all projects (active + inactive),
  the full compound index is more broadly useful here.
"""
import sqlalchemy as sa
from alembic import op

revision: str = "002_compound_indexes"
down_revision: str = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Projects ──────────────────────────────────────────────────────────────
    # Covers: WHERE owner_id=? AND is_active=TRUE ORDER BY created_at DESC, id DESC
    # Used by list_projects with cursor pagination.
    op.create_index(
        "ix_projects_owner_active_cursor",
        "projects",
        ["owner_id", "is_active", "created_at", "id"],
    )

    # ── Tasks (creator) ───────────────────────────────────────────────────────
    # Covers: WHERE creator_id=? ORDER BY created_at DESC, id DESC
    op.create_index(
        "ix_tasks_creator_cursor",
        "tasks",
        ["creator_id", "created_at", "id"],
    )

    # ── Tasks (assignee) ──────────────────────────────────────────────────────
    # Covers: WHERE assignee_id=? ORDER BY created_at DESC, id DESC
    # Together with ix_tasks_creator_cursor, enables BitmapOr for
    # WHERE creator_id=? OR assignee_id=? queries.
    op.create_index(
        "ix_tasks_assignee_cursor",
        "tasks",
        ["assignee_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_assignee_cursor", table_name="tasks")
    op.drop_index("ix_tasks_creator_cursor", table_name="tasks")
    op.drop_index("ix_projects_owner_active_cursor", table_name="projects")
