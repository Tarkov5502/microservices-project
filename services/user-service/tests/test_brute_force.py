"""
tests/test_brute_force.py (user-service)

Unit tests for the per-account brute force lockout functions in redis_client.py.

THE PROBLEM THESE TESTS GUARD AGAINST:
  The API gateway rate-limits by IP address. An attacker with multiple IPs
  (botnet, proxy rotation) can bypass the IP rate limit while hammering one
  specific account indefinitely. These tests verify the per-account counter
  that closes this gap.

KEY BEHAVIOURS TESTED:
  1. is_account_locked returns False when no failures recorded.
  2. is_account_locked returns True once failures reach LOCKOUT_THRESHOLD.
  3. record_login_failure increments the counter and returns the new count.
  4. reset_login_failures clears the counter (called on successful login).
  5. is_account_locked returns False after reset.
  6. GRACEFUL DEGRADATION: all functions return safe values when Redis is None.
  7. The pipeline for record_login_failure is atomic (INCR + EXPIRE in one roundtrip).
  8. TTL is set with NX=True on record (fixed window, not sliding).

APPROACH:
  We mock the Redis client to avoid needing a real Redis instance.
  All Redis operations are AsyncMocks. Tests verify the exact commands sent
  so we can confirm atomicity and the NX flag without a real Redis connection.
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import app.redis_client as rc


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_mock_redis():
    """Create a mock aioredis client with all methods as AsyncMocks."""
    mock = AsyncMock()
    mock.get = AsyncMock(return_value=None)
    mock.delete = AsyncMock(return_value=1)
    # Pipeline context manager
    pipeline = AsyncMock()
    pipeline.__aenter__ = AsyncMock(return_value=pipeline)
    pipeline.__aexit__ = AsyncMock(return_value=False)
    pipeline.incr = MagicMock()  # sync enqueue
    pipeline.expire = MagicMock()
    pipeline.execute = AsyncMock(return_value=[1, True])  # [incr result, expire result]
    mock.pipeline = MagicMock(return_value=pipeline)
    return mock, pipeline


# ─── is_account_locked ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestIsAccountLocked:

    async def test_returns_false_when_no_failures(self):
        """No Redis entry → account is not locked."""
        mock_redis, _ = _make_mock_redis()
        mock_redis.get = AsyncMock(return_value=None)

        with patch.object(rc, "get_redis", return_value=mock_redis), \
             patch.object(rc, "_client", mock_redis):
            result = await rc.is_account_locked("user@example.com")

        assert result is False

    async def test_returns_false_below_threshold(self):
        """Failure count below threshold → not locked."""
        mock_redis, _ = _make_mock_redis()
        mock_redis.get = AsyncMock(return_value=str(rc.LOCKOUT_THRESHOLD - 1))

        with patch.object(rc, "get_redis", return_value=mock_redis):
            result = await rc.is_account_locked("user@example.com")

        assert result is False

    async def test_returns_true_at_threshold(self):
        """Failure count == LOCKOUT_THRESHOLD → locked."""
        mock_redis, _ = _make_mock_redis()
        mock_redis.get = AsyncMock(return_value=str(rc.LOCKOUT_THRESHOLD))

        with patch.object(rc, "get_redis", return_value=mock_redis):
            result = await rc.is_account_locked("user@example.com")

        assert result is True

    async def test_returns_true_above_threshold(self):
        """Failure count > LOCKOUT_THRESHOLD → definitely locked."""
        mock_redis, _ = _make_mock_redis()
        mock_redis.get = AsyncMock(return_value=str(rc.LOCKOUT_THRESHOLD + 5))

        with patch.object(rc, "get_redis", return_value=mock_redis):
            result = await rc.is_account_locked("user@example.com")

        assert result is True

    async def test_returns_false_when_redis_unavailable(self):
        """
        GRACEFUL DEGRADATION: If Redis is down, fail open — don't lock every
        account in the system just because Redis is unhappy.
        """
        with patch.object(rc, "get_redis", return_value=None):
            result = await rc.is_account_locked("user@example.com")

        assert result is False

    async def test_returns_false_on_redis_exception(self):
        """Redis operational error → fail open, don't block logins."""
        mock_redis, _ = _make_mock_redis()
        mock_redis.get = AsyncMock(side_effect=Exception("Redis connection reset"))

        with patch.object(rc, "get_redis", return_value=mock_redis):
            result = await rc.is_account_locked("user@example.com")

        assert result is False

    async def test_uses_correct_redis_key(self):
        """Key must be login_fail:{email} so different accounts have separate counters."""
        mock_redis, _ = _make_mock_redis()
        mock_redis.get = AsyncMock(return_value=None)

        with patch.object(rc, "get_redis", return_value=mock_redis):
            await rc.is_account_locked("alice@example.com")

        mock_redis.get.assert_awaited_once_with("login_fail:alice@example.com")


# ─── record_login_failure ─────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRecordLoginFailure:

    async def test_returns_incremented_count(self):
        """Each call returns the new failure count."""
        mock_redis, pipe = _make_mock_redis()
        pipe.execute = AsyncMock(return_value=[3, True])  # 3rd failure

        with patch.object(rc, "get_redis", return_value=mock_redis):
            count = await rc.record_login_failure("user@example.com")

        assert count == 3

    async def test_returns_zero_when_redis_unavailable(self):
        """GRACEFUL DEGRADATION: Redis down → return 0, don't crash."""
        with patch.object(rc, "get_redis", return_value=None):
            count = await rc.record_login_failure("user@example.com")

        assert count == 0

    async def test_pipeline_uses_incr_and_expire(self):
        """
        ATOMICITY: Both INCR and EXPIRE must be in the same pipeline roundtrip.
        If INCR and EXPIRE were separate calls, a crash between them would
        leave a key with no TTL — the failure counter would never expire and
        the account would be permanently locked after one failure storm.
        """
        mock_redis, pipe = _make_mock_redis()

        with patch.object(rc, "get_redis", return_value=mock_redis):
            await rc.record_login_failure("user@example.com")

        # Verify pipeline was used (not individual get/set calls)
        mock_redis.pipeline.assert_called_once()
        pipe.incr.assert_called_once_with("login_fail:user@example.com")
        pipe.expire.assert_called_once()

    async def test_expire_uses_nx_true(self):
        """
        TTL is set with NX=True (only on first failure). This anchors the
        lockout window to the FIRST failure, not the most recent one.
        Without NX, an attacker could reset the window by timing attempts.
        """
        mock_redis, pipe = _make_mock_redis()

        with patch.object(rc, "get_redis", return_value=mock_redis):
            await rc.record_login_failure("user@example.com")

        # Get the actual call to expire
        expire_call = pipe.expire.call_args
        # Third positional/keyword arg should be nx=True
        assert expire_call.kwargs.get("nx") is True or \
               (len(expire_call.args) >= 3 and expire_call.args[2] is True), \
            "expire() must be called with nx=True to anchor the window to the first failure"

    async def test_expire_uses_lockout_window_seconds(self):
        """TTL must match the LOCKOUT_WINDOW_SECONDS constant."""
        mock_redis, pipe = _make_mock_redis()

        with patch.object(rc, "get_redis", return_value=mock_redis):
            await rc.record_login_failure("user@example.com")

        expire_call = pipe.expire.call_args
        ttl_arg = expire_call.args[1] if len(expire_call.args) > 1 else expire_call.kwargs.get("time")
        assert ttl_arg == rc.LOCKOUT_WINDOW_SECONDS

    async def test_returns_zero_on_redis_exception(self):
        """Operational errors → fail gracefully, return 0."""
        mock_redis, pipe = _make_mock_redis()
        pipe.execute = AsyncMock(side_effect=Exception("Connection refused"))

        with patch.object(rc, "get_redis", return_value=mock_redis):
            count = await rc.record_login_failure("user@example.com")

        assert count == 0


# ─── reset_login_failures ─────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestResetLoginFailures:

    async def test_deletes_the_failure_key(self):
        """Successful login clears the failure counter."""
        mock_redis, _ = _make_mock_redis()

        with patch.object(rc, "get_redis", return_value=mock_redis):
            await rc.reset_login_failures("user@example.com")

        mock_redis.delete.assert_awaited_once_with("login_fail:user@example.com")

    async def test_noop_when_redis_unavailable(self):
        """GRACEFUL DEGRADATION: Redis down → no error, just skip reset."""
        with patch.object(rc, "get_redis", return_value=None):
            # Must not raise
            await rc.reset_login_failures("user@example.com")

    async def test_noop_on_redis_exception(self):
        """Redis operational error → swallow, log, don't crash the login flow."""
        mock_redis, _ = _make_mock_redis()
        mock_redis.delete = AsyncMock(side_effect=Exception("Timeout"))

        with patch.object(rc, "get_redis", return_value=mock_redis):
            # Must not raise — a failed reset must not prevent login
            await rc.reset_login_failures("user@example.com")


# ─── Integration: lock → reset → unlock ───────────────────────────────────────

@pytest.mark.asyncio
class TestLockoutLifecycle:
    """
    Simulate the full lifecycle:
      fail N times → locked → successful login → not locked.
    """

    async def test_lock_then_reset_then_unlocked(self):
        """
        After reset_login_failures(), is_account_locked() must return False.
        This verifies the three functions work together correctly.
        """
        # Arrange: account is currently locked
        locked_redis, _ = _make_mock_redis()
        locked_redis.get = AsyncMock(return_value=str(rc.LOCKOUT_THRESHOLD))

        with patch.object(rc, "get_redis", return_value=locked_redis):
            is_locked_before = await rc.is_account_locked("user@example.com")

        assert is_locked_before is True

        # Act: successful login resets failures
        reset_redis, _ = _make_mock_redis()
        with patch.object(rc, "get_redis", return_value=reset_redis):
            await rc.reset_login_failures("user@example.com")

        reset_redis.delete.assert_awaited_once_with("login_fail:user@example.com")

        # Assert: after reset, account is unlocked
        unlocked_redis, _ = _make_mock_redis()
        unlocked_redis.get = AsyncMock(return_value=None)  # Key was deleted

        with patch.object(rc, "get_redis", return_value=unlocked_redis):
            is_locked_after = await rc.is_account_locked("user@example.com")

        assert is_locked_after is False
