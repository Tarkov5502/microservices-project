"""
user-service/app/redis_client.py — Shared async Redis client.

PURPOSE:
  - Refresh token storage: opaque UUID4 token → user_id (TTL: 30 days)

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

GRACEFUL DEGRADATION:
  If Redis is unavailable, refresh tokens are disabled — login still works,
  users just can't refresh and must re-authenticate when their JWT expires.
  The client sees a 503 on the /refresh endpoint rather than a silent hang.
"""
import logging
import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

_client: aioredis.Redis | None = None

REFRESH_TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days in seconds
_REFRESH_KEY_PREFIX = "refresh:"


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
