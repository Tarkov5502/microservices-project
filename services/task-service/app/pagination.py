"""
task-service/app/pagination.py — Cursor-based pagination utilities.

WHY CURSOR PAGINATION INSTEAD OF LIMIT/OFFSET?
  OFFSET has two failure modes at scale:

  1. CORRECTNESS: If a new task is inserted between page 1 and page 2 fetches,
     every existing row shifts down by one. Page 2 then returns the last item
     of page 1 again (duplicate) and skips what would have been the first item
     of page 2 (gap). With a busy task list this is constant.

  2. PERFORMANCE: OFFSET N forces the DB to scan and discard N rows before
     returning results. At offset 50,000, PostgreSQL reads 50,000 rows and
     throws them away. Cursor pagination uses a WHERE clause with an index,
     so query time is O(1) regardless of page depth.

HOW IT WORKS:
  Each item has a (created_at, id) compound sort key. The cursor encodes the
  last item returned on the previous page. The next query filters:
    WHERE (created_at, id) < (cursor_created_at, cursor_id)
  This is a keyset scan — PostgreSQL can satisfy it with the compound index.

  The cursor is base64url-encoded JSON so it's opaque and URL-safe:
    {"t": "2024-01-15T10:30:00.000000+00:00", "i": "uuid-string"}

  Clients receive next_cursor in the response. They pass it back as ?cursor=...
  to get the next page. When next_cursor is null, there are no more pages.

THREAD SAFETY: All functions are pure (no side effects). Safe to call concurrently.
"""
import base64
import json
import uuid
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):
    """
    Paginated response with cursor for next page.

    Usage in route:
        return CursorPage[TaskResponse](items=tasks, next_cursor=next_cursor)

    Client usage:
        GET /api/v1/tasks?limit=50
        # Response: {"items": [...50 tasks], "next_cursor": "eyJ0IjogIi4uLiJ9"}
        GET /api/v1/tasks?limit=50&cursor=eyJ0IjogIi4uLiJ9
        # Response: {"items": [...next 50], "next_cursor": null}   ← last page
    """
    items: list[T]
    next_cursor: str | None
    count: int  # Number of items in this page (not the total)


def encode_cursor(created_at: datetime, item_id: uuid.UUID) -> str:
    """Encode a (created_at, id) pair into an opaque URL-safe cursor string."""
    payload = {"t": created_at.isoformat(), "i": str(item_id)}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """
    Decode a cursor back into (created_at, id).
    Raises ValueError on malformed input (caller converts to 400).
    """
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(payload["t"]), uuid.UUID(payload["i"])
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc


def make_cursor_page(items: list[Any], limit: int) -> tuple[list[Any], str | None]:
    """
    Given a list of ORM objects fetched with limit+1, return (page_items, next_cursor).

    Fetch ONE EXTRA item beyond the requested limit. If we got limit+1 results,
    there are more pages. Use the (limit+1)th item to generate the cursor, then
    return only the first `limit` items to the client.
    """
    if len(items) > limit:
        next_item = items[limit]
        return items[:limit], encode_cursor(next_item.created_at, next_item.id)
    return items, None
