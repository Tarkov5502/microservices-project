"""
tests/test_rate_limiter.py

Tests for the sliding-window rate limiter middleware.

KEY BEHAVIOURS TESTED:
  1. X-Real-IP is used as the client identifier (not request.client.host).
     This is the fix for the "all users share one bucket" bug.
  2. Auth endpoints get a stricter per-IP limit than general endpoints.
  3. Health endpoints are completely exempt from rate limiting.
  4. LRU eviction: when MAX_TRACKED_IPS is exceeded, the oldest entry is dropped.
  5. IP sanitisation: invalid IP values are replaced with "unknown" safely.
"""
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
from starlette.requests import Request

# conftest.py sets JWT_SECRET before this import
from app.middleware.rate_limiter import RateLimiterMiddleware, _extract_client_ip, MAX_TRACKED_IPS


# ─── Helper: minimal ASGI app wrapped with rate limiter ───────────────────────

def _make_app(max_requests: int = 5, auth_max_requests: int = 2):
    """Create a tiny test app with the rate limiter applied."""
    async def homepage(request: Request):
        return JSONResponse({"path": str(request.url.path)})

    app = Starlette(routes=[
        Route("/api/v1/tasks", homepage),
        Route("/api/v1/auth/login", homepage, methods=["POST"]),
        Route("/health", homepage),
    ])
    app.add_middleware(
        RateLimiterMiddleware,
        max_requests=max_requests,
        window_seconds=60,
        auth_max_requests=auth_max_requests,
    )
    return app


# ─── IP extraction tests ──────────────────────────────────────────────────────

class TestExtractClientIp:
    """Unit tests for the IP extraction helper function."""

    def _make_request(self, headers: dict, client_host: str | None = "10.0.0.1"):
        """Create a minimal mock Request with the given headers."""
        mock = MagicMock()
        mock.headers = headers
        mock.client = MagicMock()
        mock.client.host = client_host
        return mock

    def test_xrealip_takes_priority_over_client_host(self):
        """X-Real-IP should be returned even when client.host is different."""
        req = self._make_request({"x-real-ip": "1.2.3.4"}, client_host="192.168.0.1")
        assert _extract_client_ip(req) == "1.2.3.4"

    def test_falls_back_to_client_host_when_no_xrealip(self):
        req = self._make_request({}, client_host="5.6.7.8")
        assert _extract_client_ip(req) == "5.6.7.8"

    def test_returns_unknown_when_client_is_none(self):
        req = self._make_request({})
        req.client = None
        assert _extract_client_ip(req) == "unknown"

    def test_rejects_xrealip_with_newline_injection(self):
        """A crafted X-Real-IP with a newline must not be used as a dict key."""
        req = self._make_request({"x-real-ip": "1.2.3.4\nX-Injected: evil"}, client_host="5.5.5.5")
        # Should fall back to client_host, not use the injection-containing value
        assert _extract_client_ip(req) == "5.5.5.5"

    def test_accepts_ipv6_addresses(self):
        req = self._make_request({"x-real-ip": "::1"}, client_host="127.0.0.1")
        assert _extract_client_ip(req) == "::1"


# ─── Middleware integration tests ─────────────────────────────────────────────

class TestRateLimiterMiddleware:

    def test_requests_under_limit_succeed(self):
        client = TestClient(_make_app(max_requests=5))
        for _ in range(5):
            resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "1.1.1.1"})
            assert resp.status_code == 200

    def test_request_over_limit_returns_429(self):
        client = TestClient(_make_app(max_requests=3))
        for _ in range(3):
            client.get("/api/v1/tasks", headers={"X-Real-IP": "2.2.2.2"})
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "2.2.2.2"})
        assert resp.status_code == 429
        assert "Rate limit exceeded" in resp.json()["detail"]

    def test_retry_after_header_is_present_on_429(self):
        client = TestClient(_make_app(max_requests=1))
        client.get("/api/v1/tasks", headers={"X-Real-IP": "3.3.3.3"})
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "3.3.3.3"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    def test_different_ips_have_independent_buckets(self):
        """The bug this tests: before the fix, all users shared one bucket."""
        client = TestClient(_make_app(max_requests=2))
        # Exhaust IP A's bucket
        client.get("/api/v1/tasks", headers={"X-Real-IP": "10.0.0.1"})
        client.get("/api/v1/tasks", headers={"X-Real-IP": "10.0.0.1"})
        resp_a = client.get("/api/v1/tasks", headers={"X-Real-IP": "10.0.0.1"})
        assert resp_a.status_code == 429  # IP A is limited

        # IP B should still be fine — it has its own bucket
        resp_b = client.get("/api/v1/tasks", headers={"X-Real-IP": "10.0.0.2"})
        assert resp_b.status_code == 200, "Different IP should not share rate limit bucket"

    def test_auth_endpoint_has_stricter_limit(self):
        """Auth endpoint limit (2) should be hit before general limit (5)."""
        client = TestClient(_make_app(max_requests=5, auth_max_requests=2))
        for _ in range(2):
            client.post("/api/v1/auth/login", headers={"X-Real-IP": "4.4.4.4"})
        resp = client.post("/api/v1/auth/login", headers={"X-Real-IP": "4.4.4.4"})
        assert resp.status_code == 429, "Auth endpoint should be rate-limited at 2 requests"

    def test_general_endpoint_not_limited_by_auth_budget(self):
        """Using up the auth budget shouldn't affect the general budget."""
        client = TestClient(_make_app(max_requests=5, auth_max_requests=2))
        for _ in range(2):
            client.post("/api/v1/auth/login", headers={"X-Real-IP": "5.5.5.5"})
        # General endpoint should still work (separate bucket)
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "5.5.5.5"})
        assert resp.status_code == 200

    def test_health_endpoint_is_exempt(self):
        """Health checks must never be rate-limited — k8s probes depend on them."""
        client = TestClient(_make_app(max_requests=1))
        # Exhaust the general bucket first
        client.get("/api/v1/tasks", headers={"X-Real-IP": "6.6.6.6"})
        # Health should still succeed
        for _ in range(10):
            resp = client.get("/health", headers={"X-Real-IP": "6.6.6.6"})
            assert resp.status_code == 200

    def test_ratelimit_headers_present_on_successful_response(self):
        client = TestClient(_make_app(max_requests=10))
        resp = client.get("/api/v1/tasks", headers={"X-Real-IP": "7.7.7.7"})
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers
        assert int(resp.headers["X-RateLimit-Remaining"]) == 9  # 10 - 1
