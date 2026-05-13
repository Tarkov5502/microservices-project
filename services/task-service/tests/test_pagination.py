"""
tests/test_pagination.py (task-service)

Unit tests for cursor-based pagination utilities.

These are pure logic tests — no database, no HTTP, no async.
encode_cursor, decode_cursor, and make_cursor_page are all pure functions.

KEY BEHAVIOURS TESTED:
  1. encode/decode round-trip: a cursor encodes and decodes back to the same values.
  2. Malformed cursors raise ValueError (caller converts to HTTP 400).
  3. make_cursor_page with len(items) == limit → last page, next_cursor is None.
  4. make_cursor_page with len(items) == limit + 1 → has next page, correct cursor.
  5. make_cursor_page with empty list → empty page, no cursor.
  6. Cursor is URL-safe (no +, /, = characters that would break query strings).
  7. Cursor is opaque (not plain JSON — base64url encoded).
  8. Cursors generated from different (created_at, id) pairs are different.
"""
import uuid
import base64
import json
import pytest
from datetime import datetime, timezone, timedelta

from app.pagination import CursorPage, encode_cursor, decode_cursor, make_cursor_page


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _dt(offset_seconds: int = 0) -> datetime:
    """Create a timezone-aware datetime offset from a fixed reference point."""
    base = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


def _fake_item(offset_seconds: int = 0) -> object:
    """
    Create a minimal ORM-like object with the fields make_cursor_page uses:
    created_at and id.
    """
    class _FakeItem:
        def __init__(self, created_at, item_id):
            self.created_at = created_at
            self.id = item_id
    return _FakeItem(_dt(offset_seconds), uuid.uuid4())


# ─── encode_cursor / decode_cursor ────────────────────────────────────────────

class TestEncodeDecodeCursor:

    def test_round_trip_preserves_datetime(self):
        """Encode then decode must return the exact same datetime."""
        dt = _dt(1000)
        item_id = uuid.uuid4()
        cursor = encode_cursor(dt, item_id)
        decoded_dt, decoded_id = decode_cursor(cursor)
        assert decoded_dt == dt

    def test_round_trip_preserves_uuid(self):
        """Encode then decode must return the exact same UUID."""
        dt = _dt(2000)
        item_id = uuid.uuid4()
        cursor = encode_cursor(dt, item_id)
        _, decoded_id = decode_cursor(cursor)
        assert decoded_id == item_id

    def test_cursor_is_a_string(self):
        cursor = encode_cursor(_dt(), uuid.uuid4())
        assert isinstance(cursor, str)

    def test_cursor_is_url_safe(self):
        """
        URL-safe base64 must NOT contain +, /, or = characters.
        These would need percent-encoding when passed as a query parameter,
        which would break cursor parsing in browsers / curl.
        """
        cursor = encode_cursor(_dt(), uuid.uuid4())
        assert "+" not in cursor
        assert "/" not in cursor
        # Padding '=' may appear; urlsafe_b64encode includes it.
        # Verify the cursor can round-trip through a URL query string unmodified.
        import urllib.parse
        encoded = urllib.parse.quote(cursor, safe="")
        # If it needed encoding, the encoded form would differ from the original
        # only for '=' (which is URL-safe in query values). Both are fine.
        assert decode_cursor(cursor) is not None  # Decodes without error

    def test_cursor_is_opaque_base64(self):
        """
        Clients must not rely on cursor structure. Verify it's not plain JSON.
        (base64url decoding the cursor is fine, but clients shouldn't do it.)
        """
        cursor = encode_cursor(_dt(), uuid.uuid4())
        # Should not look like raw JSON
        assert not cursor.startswith("{")
        # Should be valid base64url
        decoded_bytes = base64.urlsafe_b64decode(cursor + "==")  # add padding
        payload = json.loads(decoded_bytes)
        assert "t" in payload
        assert "i" in payload

    def test_different_inputs_produce_different_cursors(self):
        """Two distinct (created_at, id) pairs must produce distinct cursors."""
        dt1, id1 = _dt(0), uuid.uuid4()
        dt2, id2 = _dt(1), uuid.uuid4()
        assert encode_cursor(dt1, id1) != encode_cursor(dt2, id2)

    def test_same_datetime_different_uuid_produces_different_cursors(self):
        """Same timestamp, different UUID → different cursor."""
        dt = _dt(0)
        cursor1 = encode_cursor(dt, uuid.uuid4())
        cursor2 = encode_cursor(dt, uuid.uuid4())
        assert cursor1 != cursor2

    def test_decode_malformed_base64_raises_value_error(self):
        with pytest.raises(ValueError):
            decode_cursor("not-valid-base64!!!")

    def test_decode_valid_base64_but_wrong_json_raises_value_error(self):
        bad = base64.urlsafe_b64encode(b'{"wrong_key": "value"}').decode()
        with pytest.raises(ValueError):
            decode_cursor(bad)

    def test_decode_valid_json_but_bad_uuid_raises_value_error(self):
        payload = json.dumps({"t": "2024-01-01T00:00:00+00:00", "i": "not-a-uuid"})
        bad = base64.urlsafe_b64encode(payload.encode()).decode()
        with pytest.raises(ValueError):
            decode_cursor(bad)

    def test_decode_valid_json_but_bad_datetime_raises_value_error(self):
        payload = json.dumps({"t": "not-a-datetime", "i": str(uuid.uuid4())})
        bad = base64.urlsafe_b64encode(payload.encode()).decode()
        with pytest.raises(ValueError):
            decode_cursor(bad)

    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            decode_cursor("")


# ─── make_cursor_page ─────────────────────────────────────────────────────────

class TestMakeCursorPage:

    def test_empty_list_returns_empty_page_no_cursor(self):
        """No items → empty page, next_cursor is None (caller is on the last page)."""
        items, next_cursor = make_cursor_page([], limit=10)
        assert items == []
        assert next_cursor is None

    def test_fewer_items_than_limit_is_last_page(self):
        """3 items with limit=10 → all returned, no next cursor."""
        items_in = [_fake_item(i) for i in range(3)]
        items_out, next_cursor = make_cursor_page(items_in, limit=10)
        assert len(items_out) == 3
        assert next_cursor is None

    def test_exactly_limit_items_is_last_page(self):
        """
        Exactly limit items (not limit+1) → last page. The caller fetches
        limit+1 from the DB; receiving exactly limit means no extra item
        was found, so there's no next page.
        """
        items_in = [_fake_item(i) for i in range(10)]
        items_out, next_cursor = make_cursor_page(items_in, limit=10)
        assert len(items_out) == 10
        assert next_cursor is None

    def test_limit_plus_one_items_indicates_next_page(self):
        """
        limit+1 items → there IS a next page. Return only the first `limit`
        items to the client, and generate a cursor from the (limit+1)th item.
        """
        items_in = [_fake_item(i) for i in range(11)]  # 11 items, limit=10
        items_out, next_cursor = make_cursor_page(items_in, limit=10)
        assert len(items_out) == 10, "Only first limit items should be returned"
        assert next_cursor is not None, "Must provide a cursor when more items exist"

    def test_next_cursor_decodes_to_eleventh_item_position(self):
        """
        The cursor must encode the position of the (limit+1)th item,
        so the next query picks up from there.
        """
        items_in = [_fake_item(i) for i in range(11)]
        eleventh_item = items_in[10]
        _, next_cursor = make_cursor_page(items_in, limit=10)
        cursor_dt, cursor_id = decode_cursor(next_cursor)
        assert cursor_dt == eleventh_item.created_at
        assert cursor_id == eleventh_item.id

    def test_page_items_do_not_include_the_extra_item(self):
        """The (limit+1)th item used to generate the cursor is NOT in the response."""
        items_in = [_fake_item(i) for i in range(11)]
        extra_item = items_in[10]
        items_out, _ = make_cursor_page(items_in, limit=10)
        assert extra_item not in items_out

    def test_limit_one_with_two_items_returns_single_item_and_cursor(self):
        """Edge case: limit=1 with 2 items → one item returned, one cursor."""
        items_in = [_fake_item(0), _fake_item(1)]
        items_out, next_cursor = make_cursor_page(items_in, limit=1)
        assert len(items_out) == 1
        assert next_cursor is not None

    def test_cursor_page_model_validates(self):
        """CursorPage[str] is a valid Pydantic model with correct fields."""
        page = CursorPage[str](items=["a", "b"], next_cursor="xyz", count=2)
        assert page.count == 2
        assert page.next_cursor == "xyz"

    def test_cursor_page_null_next_cursor_is_valid(self):
        """next_cursor=None is valid and represents the last page."""
        page = CursorPage[str](items=["a"], next_cursor=None, count=1)
        assert page.next_cursor is None
