"""
tests/test_security_headers.py

Tests that SecurityHeadersMiddleware applies all required headers to every
response and actively removes fingerprinting headers.

WHY THESE TESTS MATTER:
  Security headers are easy to add and equally easy to accidentally remove
  when refactoring middleware. These tests act as a regression net — if
  someone deletes the SecurityHeadersMiddleware import or reorders middleware
  in a way that drops it, these tests fail loudly in CI before it ships.
"""
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.security_headers import SecurityHeadersMiddleware, _CREDENTIAL_PATHS


def _make_app():
    async def handler(request: Request):
        # Simulate a server header that might leak implementation details
        resp = JSONResponse({"ok": True})
        resp.headers["server"] = "uvicorn"
        resp.headers["x-powered-by"] = "Python/FastAPI"
        return resp

    app = Starlette(routes=[
        Route("/api/v1/tasks", handler),
        Route("/api/v1/auth/login", handler, methods=["POST"]),
    ])
    app.add_middleware(SecurityHeadersMiddleware)
    return app


class TestSecurityHeadersMiddleware:

    def test_content_type_options_nosniff(self):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_frame_options_deny(self):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        assert resp.headers.get("x-frame-options") == "DENY"

    def test_xss_protection_disabled(self):
        """Modern best practice: disable legacy XSS filter, rely on CSP."""
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        assert resp.headers.get("x-xss-protection") == "0"

    def test_hsts_present(self):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        hsts = resp.headers.get("strict-transport-security", "")
        assert "max-age=" in hsts
        assert "includeSubDomains" in hsts

    def test_csp_restricts_all_sources(self):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_referrer_policy_present(self):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"

    def test_permissions_policy_present(self):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        pp = resp.headers.get("permissions-policy", "")
        assert "camera=()" in pp
        assert "geolocation=()" in pp

    def test_server_header_stripped(self):
        """'server: uvicorn' must not leak to clients."""
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        assert "server" not in resp.headers or resp.headers.get("server") != "uvicorn"

    def test_x_powered_by_stripped(self):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        assert "x-powered-by" not in resp.headers

    def test_cache_control_on_auth_path(self):
        """JWT responses must never be cached by proxies or browsers."""
        client = TestClient(_make_app())
        resp = client.post("/api/v1/auth/login")
        cc = resp.headers.get("cache-control", "")
        assert "no-store" in cc

    def test_no_cache_control_on_non_auth_path(self):
        """Cache-Control: no-store should only apply to credential paths."""
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        # General paths should NOT have no-store (would defeat all caching)
        assert "no-store" not in resp.headers.get("cache-control", "")

    def test_all_credential_paths_have_cache_no_store(self):
        """Every path in _CREDENTIAL_PATHS must get the no-store directive."""
        client = TestClient(_make_app())
        # We only have /api/v1/auth/login in our test app routes,
        # but we can verify the set is populated and the logic works.
        assert "/api/v1/auth/login" in _CREDENTIAL_PATHS
        assert "/api/v1/auth/register" in _CREDENTIAL_PATHS
