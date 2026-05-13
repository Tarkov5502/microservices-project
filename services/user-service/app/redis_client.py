"""
user-service/app/redis_client.py — Shared async Redis client.

PURPOSE:
  - Refresh token storage: opaque UUID4 token → user_id (TTL: 30 days)
  - Per-account login failure tracking for brute-force lockout

WHY OPAQUE REFRESH TOKENS OVER LONG-LIVED JWTs?
  A long-lived JWT can't be revoked before expiry. If it leaks, the attacker
  has access until it expires — possibly days or weeks.

  An opaque refresh token is stored server-side. Logout deletes it from Redis
  immediately. The token is useless after that, even if the attacker still has it.

  The short-lived access JWT (60 min) is still stateless — we don't track those.
  If an access JWT leaks, worst case is 60 minutes of exposure, after which the
  attacker can't refresh without the (now deleted) refresh token.

TOKEN ROTATION:
  Each use of a refresh token issues a new one and invalidates the old one.
  This means:
  - If an attacker steals a refresh token and uses it, the legitimate user's
    next refresh attempt will fail (token already used/rotated). They must log in.
  - The security window is at most one refresh cycle.

PER-ACCOUNT BRUTE FORCE LOCKOUT:
  The API gateway rate-limits by IP address. An attacker with multiple IPs
  (botnet / proxy rotation) can bypass IP limits and still target a single
  account indefinitely. Per-account tracking closes this gap:

    Key:   login_fail:{email}
    Value: integer failure count
    TTL:   LOCKOUT_WINDOW_SECONDS (15 minutes), set on FIRST failure only.

  The window is FIXED from the first failure: 10 failures in 15 minutes
  locks the account for the remainder of that window. This is intentionally
  simpler than a sliding window — for lockout purposes, a fixed window is
  correct and avoids the sliding-window INCR+EXPIRE reset edge case.

  SECURITY NOTE — Enumeration resistance:
    The auth route always returns 401 "Invalid email or password" regardless
    of whether the account exists or is locked. An attacker cannot distinguish
    a locked account from a wrong password. The legitimate user experiences
    a frozen account and must wait for the lockout to expire (or contact
    support). This is a deliberate UX trade-off for security.

GRACEFUL DEGRADATION:
  If Redis is unavailable, refresh tokens and lockout tracking are disabled.
  Login still works, users can't refresh until Redis recovers, and brute
  force protection falls back to the gateway IP rate limiter alone.
"""
import logging
import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None

REFRESH_TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days in seconds
_REFRESH_KEY_PREFIX = "refresh:"

# Brute-force lockout constants
LOCKOUT_THRESHOLD = 10        # Max failed attempts before lockout
LOCKOUT_WINDOW_SECONDS = 900  # 15-minute fixed window
_LOGIN_FAIL_PREFIX = "login_fail:"


async def get_redis() -> aioredis.Redis | None:
    """Return the shared Redis client, or None if Redis is unavailable."""
    global _client
    if _client is not None:
        return _client
    try:
        client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=1,
        )
        await client.ping()
        _client = client
        logger.info("Redis connected: %s", settings.redis_url)
    except Exception as exc:
        logger.warning("Redis unavailable: %s — token refresh disabled", exc)
        _client = None
    return _client


async def close_redis() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None
        logger.info("Redis connection closed")


async def store_refresh_token(token: str, user_id: str) -> bool:
    """Store refresh_token → user_id with TTL. Returns False if Redis unavailable."""
    client = await get_redis()
    if not client:
        return False
    await client.setex(f"{_REFRESH_KEY_PREFIX}{token}", REFRESH_TOKEN_TTL, user_id)
    return True


async def consume_refresh_token(token: str) -> str | None:
    """
    Atomically retrieve and DELETE a refresh token.
    Returns the user_id if found, None if not found or Redis unavailable.

    Deletion on read = single-use token rotation. An attacker who intercepts
    the response gets the new token; the old one is already gone.
    """
    client = await get_redis()
    if not client:
        return None
    key = f"{_REFRESH_KEY_PREFIX}{token}"
    # GETDEL: atomic get + delete — prevents race conditions between two
    # concurrent refresh requests with the same token
    user_id: str | None = await client.getdel(key)
    return user_id


async def revoke_refresh_token(token: str) -> None:
    """Delete a refresh token (called on logout)."""
    client = await get_redis()
    if not client:
        return
    await client.delete(f"{_REFRESH_KEY_PREFIX}{token}")


# ─── Per-Account Brute Force Lockout ─────────────────────────────────────────

async def is_account_locked(email: str) -> bool:
    """
    Check whether an account has exceeded the login failure threshold.

    Returns True if the failure count is at or above LOCKOUT_THRESHOLD.
    Returns False if Redis is unavailable (fail open — don't block all logins
    because Redis is down).
    """
    client = await get_redis()
    if not client:
        return False
    try:
        raw = await client.get(f"{_LOGIN_FAIL_PREFIX}{email}")
        if raw is None:
            return False
        return int(raw) >= LOCKOUT_THRESHOLD
    except Exception as exc:
        logger.error("Lockout check failed for %s: %s", email, exc)
        return False  # Fail open


async def record_login_failure(email: str) -> int:
    """
    Increment the failure counter for an account and set a TTL on first failure.

    TTL is set with NX (only if not already set) so the window is anchored to
    the FIRST failure in the sequence, not extended by each subsequent attempt.
    This prevents an attacker from perpetually resetting the lockout window
    by timing one attempt every LOCKOUT_WINDOW_SECONDS - 1 seconds.

    Returns the current failure count after increment.
    Returns 0 if Redis is unavailable.
    """
    client = await get_redis()
    if not client:
        return 0
    key = f"{_LOGIN_FAIL_PREFIX}{email}"
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            # nx=True: only set the expiry if the key is new (first failure).
            # Keeps the window anchored to the first failure, not each one.
            pipe.expire(key, LOCKOUT_WINDOW_SECONDS, nx=True)
            results = await pipe.execute()
        count: int = results[0]
        logger.info("Login failure #%d for account: %s", count, email)
        return count
    except Exception as exc:
        logger.error("record_login_failure failed for %s: %s", email, exc)
        return 0


async def reset_login_failures(email: str) -> None:
    """
    Clear the failure counter for an account after a successful login.

    Called on successful authentication so a legitimate user who previously
    failed doesn't stay locked out after they remember their password.
    """
    client = await get_redis()
    if not client:
        return
    try:
        await client.delete(f"{_LOGIN_FAIL_PREFIX}{email}")
    except Exception as exc:
        logger.error("reset_login_failures failed for %s: %s", email, exc)


# ─── Email verification + password reset tokens ─────────────────────────────
#
# Two single-use token spaces with different prefixes and lifetimes:
#
#   verify:{token}   → user_id  (TTL = email_verification_token_ttl_seconds)
#   reset:{token}    → user_id  (TTL = password_reset_token_ttl_seconds)
#
# Both are consumed atomically via GETDEL so a single token can never be
# used twice — important for password reset, where an attacker who replays a
# reset link must NOT get a working session.
#
# If Redis is unavailable, store_* returns False (caller must surface that
# back to the user as "email service unavailable"); consume_* returns None
# (which the caller treats as "invalid or expired token").

_VERIFY_KEY_PREFIX = "verify:"
_RESET_KEY_PREFIX  = "reset:"


async def store_email_verification_token(token: str, user_id: str, ttl_seconds: int) -> bool:
    client = await get_redis()
    if not client:
        return False
    await client.setex(f"{_VERIFY_KEY_PREFIX}{token}", ttl_seconds, user_id)
    return True


async def consume_email_verification_token(token: str) -> str | None:
    """
    Single-use: GETDEL. Returns the user_id this token belongs to, or None
    if the token is unknown / expired / Redis is down.
    """
    client = await get_redis()
    if not client:
        return None
    user_id: str | None = await client.getdel(f"{_VERIFY_KEY_PREFIX}{token}")
    return user_id


async def store_password_reset_token(token: str, user_id: str, ttl_seconds: int) -> bool:
    client = await get_redis()
    if not client:
        return False
    await client.setex(f"{_RESET_KEY_PREFIX}{token}", ttl_seconds, user_id)
    return True


async def consume_password_reset_token(token: str) -> str | None:
    """Same single-use semantics as email verification."""
    client = await get_redis()
    if not client:
        return None
    user_id: str | None = await client.getdel(f"{_RESET_KEY_PREFIX}{token}")
    return user_id
