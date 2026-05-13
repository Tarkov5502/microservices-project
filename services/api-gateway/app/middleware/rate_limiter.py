"""
In-memory sliding window rate limiter middleware.

For production, swap the in-memory store for a Redis-backed counter
so limits are shared across multiple gateway replicas.

Memory safety: the _buckets dict is bounded by MAX_TRACKED_IPS via an LRU
eviction strategy. Without a bound, every unique IP that ever hit the server
would accumulate an entry and the process would leak memory indefinitely.
"""
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


class RateLimiterMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int = 100, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # Insertion-ordered dict: use as an LRU cache by moving accessed keys
        # to the end and evicting from the front when over MAX_TRACKED_IPS.
        self._buckets: dict[str, deque] = {}

    def _get_bucket(self, ip: str) -> deque:
        """Return the request-timestamp deque for an IP, applying LRU eviction."""
        if ip in self._buckets:
            # Move to end (mark as recently used)
            bucket = self._buckets.pop(ip)
            self._buckets[ip] = bucket
            return bucket
        # New IP — evict oldest entry if at capacity
        if len(self._buckets) >= MAX_TRACKED_IPS:
            oldest_ip = next(iter(self._buckets))
            del self._buckets[oldest_ip]
        bucket: deque = deque()
        self._buckets[ip] = bucket
        return bucket

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = self._get_bucket(client_ip)

        # Evict timestamps outside the sliding window
        while bucket and bucket[0] < now - self.window_seconds:
            bucket.popleft()

        if len(bucket) >= self.max_requests:
            retry_after = int(self.window_seconds - (now - bucket[0]))
            logger.warning("Rate limit exceeded for %s", client_ip)
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded. Try again later."},
                headers={"Retry-After": str(retry_after)},
            )

        bucket.append(now)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(self.max_requests - len(bucket))
        return response
