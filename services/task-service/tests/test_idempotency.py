"""
tests/test_idempotency.py (task-service)

Unit tests for the Redis-backed idempotency store.

THE BUG THESE TESTS GUARD AGAINST:
  POST /api/v1/tasks/ is not idempotent without this feature. A client
  retry after a network timeout creates a duplicate task. These tests verify
  the cache get/set contract that prevents that duplication.

KEY BEHAVIOURS TESTED:
  1. get_cached_response returns None on a cache miss.
  2. get_cached_response returns (status_code, body) on a cache hit.
  3. cache_response stores with the correct TTL (24 hours).
  4. Keys are scoped to (caller_id, idempotency_key) — different users
     with the same key have independent cache entries.
  5. Keys are sanitised: whitespace is stripped, length is capped.
  6. GRACEFUL DEGRADATION: all functions are no-ops when Redis is unavailable.
  7. Redis exceptions are caught and logged — they never propagate to callers.
  8. The 24-hour TTL constant is correct.
"""
import json
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.idempotency as idem


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_mock_redis(get_return=None):
    """Return a mock aioredis client."""
    mock = AsyncMock()
    mock.get = AsyncMock(return_value=get_return)
    mock.setex = AsyncMock(return_value=True)
    return mock


def _cached_payload(status_code: int, body: dict) -> str:
    return json.dumps({"status_code": status_code, "body": body})


# ─── get_cached_response ──────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestGetCachedResponse:

    async def test_returns_none_on_cache_miss(self):
        """No Redis entry → cache miss → return None."""
        mock_redis = _make_mock_redis(get_return=None)
        with patch.object(idem, "_get_redis", return_value=mock_redis):
            result = await idem.get_cached_response(uuid.uuid4(), "key-abc")
        assert result is None

    async def test_returns_status_and_body_on_cache_hit(self):
        """Redis has the entry → return (status_code, body)."""
        body = {"id": "task-1", "title": "Test task"}
        mock_redis = _make_mock_redis(get_return=_cached_payload(201, body))
        with patch.object(idem, "_get_redis", return_value=mock_redis):
            result = await idem.get_cached_response(uuid.uuid4(), "key-abc")
        assert result is not None
        status_code, returned_body = result
        assert status_code == 201
        assert returned_body == body

    async def test_returns_none_when_redis_unavailable(self):
        """GRACEFUL DEGRADATION: Redis down → miss, don't crash."""
        with patch.object(idem, "_get_redis", return_value=None):
            result = await idem.get_cached_response(uuid.uuid4(), "key-abc")
        assert result is None

    async def test_returns_none_on_redis_get_exception(self):
        """Redis operational error → treat as miss, don't propagate."""
        mock_redis = _make_mock_redis()
        mock_redis.get = AsyncMock(side_effect=Exception("Connection refused"))
        with patch.object(idem, "_get_redis", return_value=mock_redis):
            result = await idem.get_cached_response(uuid.uuid4(), "key-abc")
        assert result is None

    async def test_uses_caller_scoped_key(self):
        """
        Key must include caller_id so user A's 'key-1' and user B's 'key-1'
        never collide. Without caller scoping, user A could observe user B's
        task creation response.
        """
        caller_a = uuid.uuid4()
        caller_b = uuid.uuid4()
        mock_redis = _make_mock_redis(get_return=None)

        with patch.object(idem, "_get_redis", return_value=mock_redis):
            await idem.get_cached_response(caller_a, "same-key")
            await idem.get_cached_response(caller_b, "same-key")

        calls = [str(c.args[0]) for c in mock_redis.get.call_args_list]
        # Both calls should use different Redis keys
        assert calls[0] != calls[1], (
            "Different callers with the same idempotency keyifferent Redis keys"
        )


# ─── cache_response ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestCacheResponse:

    async def test_stores_with_24h_ttl(self):
        """Response must be cached with IDEMPOTENCY_TTL_SECONDS TTL."""
        mock_redis = _make_mock_redis()
        caller_id = uuid.uuid4()

        with patch.object(idem, "_get_redis", return_value=mock_redis):
            await idem.cache_response(caller_id, "key-xyz", 201, {"id": "t-1"})

        mock_redis.setex.assert_awaited_once()
        call_args = mock_redis.setex.call_args
        ttl = call_args.args[1]
        assert ttl == idem.IDEMPOTENCY_TTL_SECONDS
        assert idem.IDEMPOTENCY_TTL_SECONDS == 86400, (
            "TTL must be 24 hours (86400 seconds) — long enough for client retries, "
            "short enough to avoid indefinite accumulation."
        )

    async def test_stored_value_includes_status_and_body(self):
        """Stored JSON must round-trip status_code and body correctly."""
        mock_redis = _make_mock_redis()
        caller_id = uuid.uuid4()
        body = {"id": "t-2", "title": "My task", "status": "todo"}

        with patch.object(idem, "_get_redis", return_value=mock_redis):
            await idem.cache_response(caller_id, "key-xyz", 201, body)

        raw_value = mock_redis.setex.call_args.args[2]
        stored = json.loads(raw_value)
        assert stored["status_code"] == 201
        assert stored["body"] == body

    async def test_noop_when_redis_unavailable(self):
        """GRACEFUL DEGRADATION: Redis down → don't crash, just skip caching."""
        with patch.object(idem, "_get_redis", return_value=None):
            # Must not raise
            await idem.cache_response(uuid.uuid4(), "key-xyz", 201, {"id": "t-3"})

    async def test_noop_on_redis_setex_exception(self):
        """Redis write error → swallow, log, don't propagate to caller."""
        mock_redis = _make_mock_redis()
        mock_redis.setex = AsyncMock(side_effect=Exception("OOM"))

        with patch.object(idem, "_get_redis", return_value=mock_redis):
            # Must not raise
            await idem.cache_response(uuid.uuid4(), "key-xyz", 201, {})


# ─── Key construction / sanitisation ─────────────────────────────────────────

@pytest.mark.asyncio
class TestKeyConstruction:

    async def test_key_includes_prefix(self):
        """Redis key must start with the idempotency: prefix for namespacing."""
        mock_redis = _make_mock_redis()
        caller_id = uuid.uuid4()

        with patch.object(idem, "_get_redis", return_value=mock_redis):
            await idem.get_cached_response(caller_id, "my-key")

        redis_key = mock_redis.get.call_args.args[0]
        assert redis_key.startswith("idempotency:"), (
            f"Redis key must start with 'idempotency:' for namespacing, got: {redis_key!r}"
        )

    async def test_key_includes_caller_id(self):
        """Redis key must embed the caller_id for per-user isolation."""
        mock_redis = _make_mock_redis()
        caller_id = uuid.uuid4()

        with patch.object(idem, "_get_redis", return_value=mock_redis):
            await idem.get_cached_response(caller_id, "my-key")

        redis_key = mock_redis.get.call_args.args[0]
        assert str(caller_id) in redis_key

    async def test_whitespace_stripped_from_idempotency_key(self):
        """Leading/trailing whitespace in the header value must be stripped."""
        mock_redis = _make_mock_redis()
        caller_id = uuid.uuid4()

        with patch.object(idem, "_get_redis", return_value=mock_redis):
            await idem.get_cached_response(caller_id, "  my-key  ")

        redis_key = mock_redis.get.call_args.args[0]
        # The key in Redis should not contain leading/trailing spaces
        assert "  " not in redis_key

    async def test_long_idempotency_key_is_capped(self):
        """Pathologically long idempotency keys must be truncated to 128 chars."""
        mock_redis = _make_mock_redis()
        caller_id = uuid.uuid4()
        very_long_key = "x" * 1000

        with patch.object(idem, "_get_redis", return_value=mock_redis):
            await idem.get_cached_response(caller_id, very_long_key)

        redis_key = mock_redis.get.call_args.args[0]
        # The key part contributed by the idempotency_key should be <= 128 chars
        # Key format: idempotency:{caller_id}:{safe_key}
        prefix_len = len(f"idempotency:{caller_id}:")
        key_part = redis_key[prefix_len:]
        assert len(key_part) <= 128


# ─── Round-trip integration ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_then_retrieve_round_trips():
    """
    Store a response then retrieve it. This is the happy path that prevents
    duplicate task creation on client retry.
    """
    caller_id = uuid.uuid4()
    body = {"id": "task-99", "title": "Prevent duplicates", "status": "todo"}
    stored_raw: dict[str, str] = {}

    # Simulate Redis with a real in-memory dict
    mock_redis = AsyncMock()

    async def setex(key, ttl, value):
        stored_raw[key] = value

    async def get(key):
        return stored_raw.get(key)

    mock_redis.setex = setex
    mock_redis.get = get

    with patch.object(idem, "_get_redis", return_value=mock_redis):
        # First: cache the response
        await idem.cache_response(caller_id, "idempotency-key-1", 201, body)
        # Second: retrieve it (simulates the retry)
        result = await idem.get_cached_response(caller_id, "idempotency-key-1")

    assert result is not None
    status_code, retrieved_body = result
    assert status_code == 201
    assert retrieved_body == body
