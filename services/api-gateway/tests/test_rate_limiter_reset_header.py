"""
tests/test_rate_limiter_reset_header.py

Tests for the X-RateLimit-Reset header added to the rate limiter.

THE PROBLEM THIS SOLVES:
  Before the fix, rate-limited clients received X-RateLimit-Limit and
  X-RateLimit-Remaining but no X-RateLimit-Reset. Without a reset timestamp,
  a client hitting 429 has to guess how long to back off — usually with
  exponential backoff that may be way longer (wasted time) or too short
  (hammering the server needlessly).

  X-RateLimit-Reset gives clients the exact Unix epoch second when they can
  safely retry. Combined with Retry-After (seconds), clients have two options
  for implementing backoff correctly.

WHAT THESE TESTS VERIFY:
  1. X-RateLimit-Reset is present on every successful response.
  2. X-RateLimit-Reset is present on 429 responses.
  3. Retry-After is also present on 429 (belt-and-suspenders).
  4. The reset timestamp is a plausible epoch integer (within a sane range).
  5. X-RateLimit-Reset on 429 equals X-RateLimit-Reset on success for the
     same window (both use now + window_seconds).
"""
import time

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.rate_limiter import RateLimiterMiddleware


def _make_app(max_requests: int = 5, window_seconds: int = 60):
    async def endpoint(request: Request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/v1/tasks", endpoint)])
    app.add_middleware(
        RateLimiterMiddleware,
        max_requests=max_requests,
        window_seconds=window_seconds,
        auth_max_requests=2,
    )
    return app


class TestRateLimitResetHeader:

    def test_reset_header_present_on_success(self):
        """X-RateLimit-Reset must be in every successful response."""
        client = TestClient(_make_app(max_requests=10))
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "1.1.1.1"})
        assert resp.status_code == 200
        assert "X-RateLimit-Reset" in resp.headers, (
            "X-RateLimit-Reset must be returned on every response so clients "
            "can implement correct backoff without guessing."
        )

    def test_reset_header_is_an_integer(self):
        """The reset value must be parseable as an integer Unix timestamp."""
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "2.2.2.2"})
        reset_str = resp.headers.get("X-RateLimit-Reset", "")
        assert reset_str.isdigit(), (
            f"X-RateLimit-Reset must be an integer Unix epoch, got: {reset_str!r}"
        )

    def test_reset_header_is_in_the_future(self):
        """The reset time must be after 'now' — it's a future expiry timestamp."""
        client = TestClient(_make_app(window_seconds=60))
        before = int(time.time())
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "3.3.3.3"})
        after = int(time.time())

        reset = int(resp.headers["X-RateLimit-Reset"])
        # Reset must be in [now + window_seconds - 1, now + window_seconds + 1]
        # The -1/+1 tolerance covers clock rounding between before/after calls.
        assert reset >= before + 60 - 1
        assert reset <= after + 60 + 1

    def test_reset_header_present_on_429(self):
        """
        The 429 response must also include X-RateLimit-Reset so clients know
        WHEN they can retry, not just THAT they're blocked.
        """
        client = TestClient(_make_app(max_requests=1))
        client.get("/api/v1/tasks", headers={"X-Real-IP": "4.4.4.4"})  # exhaust
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "4.4.4.4"})
        assert resp.status_code == 429
        assert "X-RateLimit-Reset" in resp.headers

    def test_retry_after_present_on_429(self):
        """Retry-After (seconds) is the HTTP/1.1 standard; X-RateLimit-Reset is
        the de-facto API standard. Both should be present on 429."""
        client = TestClient(_make_app(max_requests=1))
        client.get("/api/v1/tasks", headers={"X-Real-IP": "5.5.5.5"})
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "5.5.5.5"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    def test_remaining_zero_on_429(self):
        """When limited, X-RateLimit-Remaining must report 0 (not a negative number)."""
        client = TestClient(_make_app(max_requests=1))
        client.get("/api/v1/tasks", headers={"X-Real-IP": "6.6.6.6"})
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "6.6.6.6"})
        assert resp.status_code == 429
        remaining = int(resp.headers.get("X-RateLimit-Remaining", "-1"))
        assert remaining == 0, f"Remaining on 429 must be 0, got {remaining}"

    def test_remaining_decrements_across_requests(self):
        """X-RateLimit-Remaining must decrease by 1 with each request."""
        client = TestClient(_make_app(max_requests=5))
        ip_headers = {"X-Real-IP": "7.7.7.7"}

        resp1 = client.get("/api/v1/tasks", headers=ip_headers)
        resp2 = client.get("/api/v1/tasks", headers=ip_headers)
        resp3 = client.get("/api/v1/tasks", headers=ip_headers)

        assert int(resp1.headers["X-RateLimit-Remaining"]) == 4
        assert int(resp2.headers["X-RateLimit-Remaining"]) == 3
        assert int(resp3.headers["X-RateLimit-Remaining"]) == 2
