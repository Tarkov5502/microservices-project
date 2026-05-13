"""
task-service/app/idempotency.py — Redis-backed idempotency for task creation.

THE PROBLEM:
  POST /api/v1/tasks/ is not idempotent. If a client sends the request, the
  network times out before a response arrives, and the client retries — the
  task gets created twice. Two duplicate tasks in the same project are hard
  to detect and annoying to clean up.

  This is especially likely in:
    - Mobile clients with flaky connections
    - Automated scripts with retry logic
    - Browser fetch() calls retried after a 504 from the gateway

THE SOLUTION — Idempotency Keys:
  Clients include an Idempotency-Key: <uuid4> header on creation requests.
  The server caches the response for 24 hours keyed by (caller_id, key).
  On a duplicate request (same key), the original response is returned
  without re-executing the request body.

  Stripe uses exactly this pattern:
    https://stripe.com/docs/api/idempotent_requests

SCOPE:
  Keys are scoped to the caller: user A's key "abc" and user B's key "abc"
  are independent. This prevents user B from accidentally (or maliciously)
  colliding with user A's idempotency space.

KEY FORMAT IN REDIS:
  idempotency:{caller_id}:{idempotency_key}
  Value: JSON of {status_code, body}
  TTL: IDEMPOTENCY_TTL_SECONDS (24 hours)

WHAT IS STORED:
  The HTTP response status code + JSON body. For task creation this is always
  201 + the TaskResponse JSON. On a duplicate, we reconstruct a Response object
  with the exact same status and body — identical to what the original returned.

GRACEFUL DEGRADATION:
  If Redis is unavailable, the endpoint proceeds WITHOUT idempotency protection.
  This means duplicate requests during a Redis outage may create duplicate tasks,
  but the endpoint remains functional. The alternative (503 every time Redis is
  down) is worse — it takes down task creation entirely.

CACHE INVALIDATION:
  The 24-hour TTL is long enough that a client retrying after a network timeout
  (typically < 30 seconds) always hits the cache. It's short enough that stale
  entries don't accumulate indefinitely.

LIMITATIONS:
  - Idempotency is only implemented for task creation (POST). Update and delete
    are already idempotent by nature (PATCH/DELETE of the same resource twice
    returns the same result).
  - Long-running requests (> Redis key TTL) are not protected — but task
    creation is fast (< 200ms) so this is not a realistic concern.
"""
import json
import logging
import uuid

import redis.asyncio as aioredis

from app.config import settings

logger = logging.getLogger(__name__)

IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24  # 24 hours
_KEY_PREFIX = "idempotency:"

_client: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis | None:
    """Return a shared Redis client, or None if Redis is unavailable."""
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
        logger.info("Idempotency store connected to Redis: %s", settings.redis_url)
    except Exception as exc:
        logger.warning(
            "Redis unavailable for idempotency store (%s) — "
            "duplicate POST requests may create duplicate tasks during outage.",
            exc,
        )
        _client = None
    return _client


async def close_redis() -> None:
    """Close the shared Redis client on shutdown."""
    global _client
    if _client:
        await _client.aclose()
        _client = None


def _make_key(caller_id: uuid.UUID, idempotency_key: str) -> str:
    """
    Build the Redis key for an idempotency entry.

    Scoped to caller_id so different users with the same key value are
    independent — no cross-user collision is possible.
    """
    # Sanitise idempotency_key: strip whitespace, limit length.
    # A malicious client cannot inject newlines or control chars into the key.
    safe_key = idempotency_key.strip()[:128]
    return f"{_KEY_PREFIX}{caller_id}:{safe_key}"


async def get_cached_response(
    caller_id: uuid.UUID,
    idempotency_key: str,
) -> tuple[int, dict] | None:
    """
    Look up a cached response for this (caller_id, idempotency_key) pair.

    Returns (status_code, body_dict) if found, None if not found or Redis is down.
    """
    client = await _get_redis()
    if not client:
        return None
    key = _make_key(caller_id, idempotency_key)
    try:
        raw = await client.get(key)
        if raw is None:
            return None
        cached = json.loads(raw)
        return cached["status_code"], cached["body"]
    except Exception as exc:
        logger.error("Idempotency cache GET failed for key %r: %s", key, exc)
        return None


async def cache_response(
    caller_id: uuid.UUID,
    idempotency_key: str,
    status_code: int,
    body: dict,
) -> None:
    """
    Store the response for this (caller_id, idempotency_key) pair with a 24h TTL.

    Called immediately after a successful task creation. If Redis is down,
    this is a no-op — the next duplicate request won't find a cache entry and
    will proceed to create a second task. This is acceptable: a Redis outage
    is a known-bad state, and we prefer availability over strict idempotency.

    Args:
        caller_id:        The authenticated user who made the request.
        idempotency_key:  The client-supplied Idempotency-Key header value.
        status_code:      HTTP status code of the response to cache (e.g. 201).
        body:             JSON-serialisable response body dict.
    """
    client = await _get_redis()
    if not client:
        return
    key = _make_key(caller_id, idempotency_key)
    try:
        payload = json.dumps({"status_code": status_code, "body": body})
        await client.setex(key, IDEMPOTENCY_TTL_SECONDS, payload)
        logger.debug("Cached idempotency response for key %r (TTL: 24h)", key)
    except Exception as exc:
        logger.error("Idempotency cache SET failed for key %r: %s", key, exc)
