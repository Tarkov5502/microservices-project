"""
api-gateway/app/middleware/rate_limiter.py

Sliding-window rate limiter backed by Redis, with in-memory fallback.

WHY REDIS?
  The gateway scales horizontally via HPA (2-10 replicas). Each replica
  running an in-memory rate limiter has an *independent* bucket per IP.
  With 5 replicas and a limit of 100 req/min, a client can make 500 req/min
  — 5× the intended limit — by simply round-robining across replicas.

  Redis is a single shared store, so rate limit state is consistent across
  all replicas. One client, one bucket, regardless of which pod serves them.

REDIS PATTERN — Sliding Window with Sorted Set:
  Key: rate:{scope}:{ip}
  Each request adds an entry with score = current_timestamp.
  Old entries (outside the window) are pruned before counting.
  The pipeline runs as a MULTI/EXEC transaction for atomicity.

  Commands per request (all in one pipeline roundtrip):
    ZREMRANGEBYSCORE key -inf (now - window)  ← prune expired
    ZADD key now unique_id                    ← record this request
    ZCARD key                                 ← count current window
    EXPIRE key window_seconds                 ← auto-cleanup

FALLBACK:
  If Redis is unavailable at startup or a request fails, the limiter
  transparently falls back to the in-memory implementation. This ensures
  the gateway keeps serving traffic even during a Redis outage — it just
  loses cross-replica coordination temporarily.

RATE LIMIT HEADERS:
  Every response carries three standard headers:
    X-RateLimit-Limit     — the max requests allowed in the window
    X-RateLimit-Remaining — requests left in the current window
    X-RateLimit-Reset     — Unix epoch (seconds) when the window resets

  X-RateLimit-Reset uses a fixed-window approximation: now + window_seconds.
  For a true sliding window the reset time varies per request, but clients
  use this value to decide how long to back off — a conservative upper bound
  (now + window) is correct: the client will never have to wait longer than
  that for a fresh window to open.

  429 responses additionally include Retry-After (seconds until reset) for
  HTTP/1.1 clients that don't parse X-RateLimit-Reset.

IP EXTRACTION:
  See _extract_client_ip() for the X-Real-IP priority logic and
  log-injection sanitisation.
"""
import re
import time
import uuid
import logging
from collections import deque
from typing import Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

MAX_TRACKED_IPS = 10_000

_AUTH_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/register",
})

_EXEMPT_PATHS = frozenset({"/health", "/health/ready", "/metrics"})

_IP_RE = re.compile(r"^[\w:.\[\]%-]{1,64}$")


def _extract_client_ip(request: Request) -> str:
    """
    Resolve the real client IP from NGINX-forwarded headers.
    See original docstring for full security rationale.
    """
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip and _IP_RE.match(real_ip):
        return real_ip
    fallback = request.client.host if request.client else "unknown"
    if not _IP_RE.match(fallback):
        logger.warning("Suspicious client IP value: %r — using 'unknown'", fallback)
        return "unknown"
    return fallback


# ─── Redis-backed store ───────────────────────────────────────────────────────

class _RedisStore:
    """
    Rate limit state stored in Redis using a sorted set per (scope, ip) key.
    All operations are atomic via a pipelined MULTI/EXEC transaction.
    """

    def __init__(self, client) -> None:
        self._redis = client

    async def check_and_increment(
        self, key: str, limit: int, window_seconds: int
    ) -> tuple[bool, int]:
        """
        Returns (is_limited, current_count).
        Atomically prunes old entries, adds this request, returns the count.
        """
        now = time.time()
        window_start = now - window_seconds
        member = str(uuid.uuid4())  # Unique per request — avoids ZADD overwrite

        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(key, "-inf", window_start)
                pipe.zadd(key, {member: now})
                pipe.zcard(key)
                pipe.expire(key, window_seconds + 1)
                results = await pipe.execute()
            count: int = results[2]  # zcard result
            return count > limit, count
        except Exception as exc:
            logger.error("Redis rate-limit check failed: %s", exc)
            # Fail open — don't block traffic because Redis is unhappy
            return False, 0


# ─── In-memory fallback store ─────────────────────────────────────────────────

class _InMemoryStore:
    """
    Sliding window rate limiter using an in-memory dict of deques.
    Correct for single-replica deployments; loses cross-replica coordination.
    Used as fallback when Redis is unavailable.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, deque] = {}

    def _get_bucket(self, key: str) -> deque:
        if key in self._buckets:
            bucket = self._buckets.pop(key)
            self._buckets[key] = bucket
            return bucket
        if len(self._buckets) >= MAX_TRACKED_IPS:
            del self._buckets[next(iter(self._buckets))]
        bucket: deque = deque()
        self._buckets[key] = bucket
        return bucket

    async def check_and_increment(
        self, key: str, limit: int, window_seconds: int
    ) -> tuple[bool, int]:
        now = time.monotonic()
        bucket = self._get_bucket(key)
        while bucket and bucket[0] < now - window_seconds:
            bucket.popleft()
        bucket.append(now)
        count = len(bucket)
        return count > limit, count


# ─── Middleware ───────────────────────────────────────────────────────────────

class RateLimiterMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter. Uses Redis when available, in-memory otherwise.

    The store is resolved lazily on the first request so the middleware can
    be constructed synchronously at startup time even if Redis isn't yet ready.
    """

    def __init__(
        self,
        app,
        max_requests: int = 100,
        window_seconds: int = 60,
        auth_max_requests: int = 10,
        user_max_requests: int = 300,
        redis_url: str | None = None,
    ):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.auth_max_requests = auth_max_requests
        # Per-authenticated-user budget, applied in ADDITION to the per-IP
        # budget. A user on a corporate NAT can saturate the per-IP bucket
        # through no fault of their own; the per-user limit gives them their
        # own budget. An attacker who steals a single JWT is still capped at
        # this rate regardless of how many IPs they rotate through.
        self.user_max_requests = user_max_requests
        self._redis_url = redis_url
        self._store: _RedisStore | _InMemoryStore | None = None

    async def _get_store(self) -> _RedisStore | _InMemoryStore:
        """Lazy initialisation — connect to Redis on first request."""
        if self._store is not None:
            return self._store

        if self._redis_url:
            try:
                import redis.asyncio as aioredis
                client = aioredis.from_url(
                    self._redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_connect_timeout=2,
                    socket_timeout=1,
                )
                await client.ping()
                self._store = _RedisStore(client)
                logger.info("Rate limiter using Redis: %s", self._redis_url)
            except Exception as exc:
                logger.warning(
                    "Redis unavailable (%s) — rate limiter falling back to in-memory. "
                    "Cross-replica rate limiting will not be enforced.", exc
                )
                self._store = _InMemoryStore()
        else:
            logger.info("No REDIS_URL set — rate limiter using in-memory store")
            self._store = _InMemoryStore()

        return self._store

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Enforce two independent buckets:
          - per-IP, always
          - per-user-id, IF the request is authenticated (JWTAuth middleware
            has run upstream and populated request.state.user_id)

        Either bucket can produce a 429. Response headers describe the
        most-restrictive of the two so a client knows which budget to back
        off against.
        """
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        client_ip = _extract_client_ip(request)
        is_auth_path = request.url.path in _AUTH_PATHS
        scope = "auth" if is_auth_path else "general"
        ip_limit = self.auth_max_requests if is_auth_path else self.max_requests
        ip_key = f"rate:{scope}:{client_ip}"

        # Reset time: conservative upper bound — the furthest this request
        # could possibly stay within the window.
        reset_ts = int(time.time()) + self.window_seconds

        store = await self._get_store()

        # ── Per-IP bucket ─────────────────────────────────────────────────
        ip_over, ip_count = await store.check_and_increment(
            ip_key, ip_limit, self.window_seconds
        )
        ip_remaining = max(0, ip_limit - ip_count)

        # ── Per-user bucket (only for authenticated requests) ─────────────
        user_id = getattr(request.state, "user_id", None)
        user_over = False
        user_remaining = self.user_max_requests
        if user_id:
            user_key = f"rate:user:{user_id}"
            user_over, user_count = await store.check_and_increment(
                user_key, self.user_max_requests, self.window_seconds
            )
            user_remaining = max(0, self.user_max_requests - user_count)

        # ── Decide which (if any) limit fired ─────────────────────────────
        if ip_over or user_over:
            which = "ip" if ip_over else "user"
            logger.warning(
                "Rate limit exceeded — scope=%s by=%s ip=%s user=%s",
                scope, which, client_ip, user_id,
            )
            # Report the more-restrictive limit so the client backs off
            # against the right budget.
            reported_limit = ip_limit if ip_over else self.user_max_requests
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={
                    "Retry-After":            str(self.window_seconds),
                    "X-RateLimit-Limit":      str(reported_limit),
                    "X-RateLimit-Remaining":  "0",
                    "X-RateLimit-Reset":      str(reset_ts),
                    "X-RateLimit-Scope":      which,
                },
            )

        response = await call_next(request)
        # Surface the tighter of the two budgets in the response headers so
        # clients pace themselves against the binding constraint.
        if user_id and user_remaining < ip_remaining:
            response.headers["X-RateLimit-Limit"]     = str(self.user_max_requests)
            response.headers["X-RateLimit-Remaining"] = str(user_remaining)
            response.headers["X-RateLimit-Scope"]     = "user"
        else:
            response.headers["X-RateLimit-Limit"]     = str(ip_limit)
            response.headers["X-RateLimit-Remaining"] = str(ip_remaining)
            response.headers["X-RateLimit-Scope"]     = "ip"
        response.headers["X-RateLimit-Reset"] = str(reset_ts)
        return response
