"""
tests/test_email_flows.py — Email verification + password reset flows.

We test the building blocks (token storage + consumption) with mocked Redis,
and the route schemas with Pydantic. The full HTTP-level flow is covered in
the integration test suite (tests/integration/) where a real Postgres + Redis
are available via docker-compose.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.redis_client as rc
from app.routes.auth import (
    ForgotPasswordRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    VerifyEmailRequest,
    _new_url_safe_token,
)


# ─── Tokens ──────────────────────────────────────────────────────────────────


def test_new_url_safe_token_is_long_and_unique():
    a = _new_url_safe_token()
    b = _new_url_safe_token()
    assert len(a) >= 32
    assert a != b


# ─── Schemas ─────────────────────────────────────────────────────────────────


class TestResetPasswordRequest:

    def test_complexity_enforced_on_new_password(self):
        with pytest.raises(Exception, match="uppercase"):
            ResetPasswordRequest(token="a" * 32, new_password="lowercase1")

    def test_complexity_requires_digit(self):
        with pytest.raises(Exception, match="digit"):
            ResetPasswordRequest(token="a" * 32, new_password="NoDigitsHere")

    def test_token_min_length(self):
        with pytest.raises(Exception):
            ResetPasswordRequest(token="short", new_password="GoodPass1")

    def test_valid_payload_accepts(self):
        r = ResetPasswordRequest(token="a" * 32, new_password="GoodPass1")
        assert r.new_password == "GoodPass1"


def test_forgot_password_request_validates_email():
    with pytest.raises(Exception):
        ForgotPasswordRequest(email="not-an-email")


def test_resend_verification_validates_email():
    with pytest.raises(Exception):
        ResendVerificationRequest(email="garbage")


# ─── Redis-backed token storage ──────────────────────────────────────────────


def _mock_redis():
    m = AsyncMock()
    m.setex = AsyncMock(return_value=True)
    m.getdel = AsyncMock(return_value=None)
    return m


@pytest.mark.asyncio
async def test_store_email_verification_token_uses_setex_with_ttl():
    redis = _mock_redis()
    with patch.object(rc, "get_redis", return_value=redis):
        ok = await rc.store_email_verification_token("tkn123", "user-uuid", 12345)
    assert ok is True
    # Check key + TTL
    redis.setex.assert_called_once()
    call = redis.setex.call_args
    assert call.args[0] == "verify:tkn123"
    assert call.args[1] == 12345
    assert call.args[2] == "user-uuid"


@pytest.mark.asyncio
async def test_consume_email_verification_token_uses_getdel():
    """Single-use semantics: consumption must DELETE the token."""
    redis = _mock_redis()
    redis.getdel = AsyncMock(return_value="user-uuid")
    with patch.object(rc, "get_redis", return_value=redis):
        result = await rc.consume_email_verification_token("tkn123")
    assert result == "user-uuid"
    redis.getdel.assert_called_once_with("verify:tkn123")


@pytest.mark.asyncio
async def test_consume_unknown_verify_token_returns_none():
    redis = _mock_redis()
    redis.getdel = AsyncMock(return_value=None)
    with patch.object(rc, "get_redis", return_value=redis):
        assert await rc.consume_email_verification_token("nope") is None


@pytest.mark.asyncio
async def test_password_reset_token_uses_different_prefix():
    """Verify and reset tokens MUST NOT share a namespace — a leaked
    verification token must not let you reset a password."""
    redis = _mock_redis()
    with patch.object(rc, "get_redis", return_value=redis):
        await rc.store_password_reset_token("tkn", "u", 10)
    assert redis.setex.call_args.args[0] == "reset:tkn"
    assert not redis.setex.call_args.args[0].startswith("verify:")


@pytest.mark.asyncio
async def test_token_store_returns_false_when_redis_down():
    """Graceful degradation: store returns False, caller surfaces it."""
    with patch.object(rc, "get_redis", return_value=None):
        assert await rc.store_email_verification_token("t", "u", 10) is False
        assert await rc.store_password_reset_token("t", "u", 10) is False


@pytest.mark.asyncio
async def test_token_consume_returns_none_when_redis_down():
    with patch.object(rc, "get_redis", return_value=None):
        assert await rc.consume_email_verification_token("t") is None
        assert await rc.consume_password_reset_token("t") is None


# ─── Email sender abstraction ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_sender_writes_to_logger(caplog):
    """Dev sender emits the body via the application logger."""
    import logging
    from app.email import _LogEmailSender

    sender = _LogEmailSender()
    with caplog.at_level(logging.WARNING, logger="app.email"):
        await sender.send("dev@example.com", "Test subject", "Click https://x.com/y?z=tkn")

    text = caplog.text
    assert "dev@example.com" in text
    assert "Test subject" in text
    assert "Click https://x.com/y?z=tkn" in text
