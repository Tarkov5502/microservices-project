"""
In-memory sliding window rate limiter middleware.

Fix #1 — Client IP extraction:
  The original code used `request.client.host` which resolves to the NGINX
  Ingress pod IP in Kubernetes. Every user shared the same rate limit bucket!
  Now we read X-Real-IP (set by NGINX to the actual client IP) first, then
  fall back to the direct connection host. This is safe because our
  NetworkPolicy only allows NGINX to reach the gateway — no untrusted pod
  can spoof X-Real-IP by going direct.

Fix #2 — Auth endpoint stricter limits:
  Login and registration endpoints accept credentials. The general 100 req/min
  limit is far too permissive for brute-forcing passwords. Auth paths get a
  separate, configurable stricter limit (default: 10/min).

For production, swap the in-memory store for a Redis-backed counter
so limits are shared across multiple gateway replicas.

Memory safety: the _buckets dict is bounded by MAX_TRACKED_IPS via an LRU
eviction strategy. Without a bound, every unique IP that ever hit the server
would accumulate an entry and the process would leak memory indefinitely.
"""
import re
import time
import logging
from collections import deque
from typing import Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Maximum number of unique client IPs tracked simultaneously.
# When exceeded, the oldest-seen IP entry is evicted (LRU policy).
MAX_TRACKED_IPS = 10_000

# Auth paths that need a tighter per-minute limit to slow brute-force.
_AUTH_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/register",
})

# Paths that should NEVER be rate-limited.
# Kubernetes probes them every few seconds — blocking them means evictions.
_EXEMPT_PATHS = frozenset({"/health", "/health/ready", "/metrics"})

# Allowlist for valid IP address characters. Log injection via crafted
# X-Real-IP values (e.g. containing newlines or log-format delimiters) is
# prevented by validating the header value before using it as a dict key.
_IP_RE = re.compile(r"^[\w:.\[\]%-]{1,64}$")


def _extract_client_ip(request: Request) -> str:
    """
    Resolve the real client IP from NGINX-forwarded headers.

    Priority:
      1. X-Real-IP  — NGINX sets this to the actual downstream client IP.
      2. Direct connection (request.client.host) — used in local dev.

    We intentionally do NOT parse X-Forwarded-For here because:
      a) NGINX already handles XFF and sets X-Real-IP correctly.
      b) XFF is a comma-separated list; parsing it is error-prone.
      c) Any pod that bypasses NGINX can't set X-Real-IP (NetworkPolicy).
    """
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip and _IP_RE.match(real_ip):
        return real_ip

    fallback = request.client.host if request.client else "unknown"
    if not _IP_RE.match(fallback):
        logger.warning("Suspicious client IP value: %r — using 'unknown'", fallback)
        return "unknown"
    return fallback


class RateLimiterMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        max_requests: int = 100,
        window_seconds: int = 60,
        auth_max_requests: int = 10,
    ):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.auth_max_requests = auth_max_requests
        # Separate bucket store per (ip, scope) key so auth limits don't
        # consume from the general pool or vice-versa.
        self._buckets: dict[str, deque] = {}

    def _get_bucket(self, key: str) -> deque:
        """Return the timestamp deque for a key, applying LRU eviction."""
        if key in self._buckets:
            bucket = self._buckets.pop(key)
            self._buckets[key] = bucket
            return bucket
        if len(self._buckets) >= MAX_TRACKED_IPS:
            oldest = next(iter(self._buckets))
            del self._buckets[oldest]
        bucket: deque = deque()
        self._buckets[key] = bucket
        return bucket

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        client_ip = _extract_client_ip(request)
        is_auth = request.url.path in _AUTH_PATHS
        scope = "auth" if is_auth else "general"
        limit = self.auth_max_requests if is_auth else self.max_requests
        key = f"{scope}:{client_ip}"

        now = time.monotonic()
        bucket = self._get_bucket(key)

        # Evict timestamps outside the sliding window
        while bucket and bucket[0] < now - self.window_seconds:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = int(self.window_seconds - (now - bucket[0]))
            # Log at WARNING with sanitised IP (already validated by regex)
            logger.warning("Rate limit exceeded for %s on %s", client_ip, scope)
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(limit - len(bucket))
        return response
