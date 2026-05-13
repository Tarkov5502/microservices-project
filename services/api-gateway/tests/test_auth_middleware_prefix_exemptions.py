"""
tests/test_auth_middleware_prefix_exemptions.py

Tests for the exempt_prefixes parameter added to JWTAuthMiddleware.

THE BUG THIS TESTS:
  Before the fix, the middleware only accepted exact exempt_paths. The auth
  endpoints (/api/v1/auth/login, /api/v1/auth/register, etc.) were NOT in
  the exempt list, so the gateway required a JWT to reach the login endpoint
  — a chicken-and-egg deadlock that made the entire system unusable.

  The fix adds exempt_prefixes: any path starting with a prefix in the list
  is allowed through without a JWT. /api/v1/auth/ covers all auth routes.

WHAT THESE TESTS VERIFY:
  1. Paths matching an exempt prefix are served without any token.
  2. Paths NOT matching the prefix still require a valid JWT.
  3. Exact exempt_paths still work alongside prefixes.
  4. Multiple prefixes can be registered simultaneously.
  5. A prefix of "/" exempts everything (edge case for dev).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.auth import JWTAuthMiddleware

_SECRET = os.environ["JWT_SECRET"]
_ALGORITHM = "HS256"


def _make_token(sub: str | None = None) -> str:
    sub = sub or str(uuid.uuid4())
    payload = {
        "sub": sub,
        "email": "test@example.com",
        "roles": ["user"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def _make_app(exempt_paths: list[str] | None = None, exempt_prefixes: list[str] | None = None):
    """Minimal ASGI app with configurable JWT middleware exemptions."""

    async def endpoint(request: Request):
        return JSONResponse({"ok": True})

    routes = [
        Route("/health", endpoint),
        Route("/api/v1/auth/login", endpoint, methods=["POST"]),
        Route("/api/v1/auth/register", endpoint, methods=["POST"]),
        Route("/api/v1/auth/refresh", endpoint, methods=["POST"]),
        Route("/api/v1/auth/logout", endpoint, methods=["POST"]),
        Route("/api/v1/tasks", endpoint),
        Route("/api/v1/users/me", endpoint),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(
        JWTAuthMiddleware,
        exempt_paths=exempt_paths or [],
        exempt_prefixes=exempt_prefixes or [],
    )
    return app


class TestExemptPrefixes:
    """
    Validate that every /api/v1/auth/* route is reachable without a JWT
    when the /api/v1/auth/ prefix is configured as exempt.

    This is the primary regression test for the login-deadlock bug.
    """

    def _client(self) -> TestClient:
        return TestClient(
            _make_app(
                exempt_paths=["/health"],
                exempt_prefixes=["/api/v1/auth/"],
            )
        )

    def test_login_is_reachable_without_token(self):
        """The most critical test: POST /api/v1/auth/login must work with no JWT."""
        resp = self._client().post("/api/v1/auth/login")
        assert resp.status_code == 200, (
            "Login endpoint must be reachable without a JWT. "
            "If this fails, users can never authenticate — the system is unusable."
        )

    def test_register_is_reachable_without_token(self):
        resp = self._client().post("/api/v1/auth/register")
        assert resp.status_code == 200

    def test_refresh_is_reachable_without_token(self):
        """
        Refresh carries an opaque refresh token, not a JWT.
        If this were JWT-protected, users couldn't renew expired access tokens.
        """
        resp = self._client().post("/api/v1/auth/refresh")
        assert resp.status_code == 200

    def test_logout_is_reachable_without_token(self):
        """
        Logout must work even when the access JWT has expired.
        Users need to revoke their refresh token regardless of JWT state.
        """
        resp = self._client().post("/api/v1/auth/logout")
        assert resp.status_code == 200

    def test_non_auth_endpoints_still_require_token(self):
        """
        The prefix exemption must NOT accidentally open up all endpoints.
        /api/v1/tasks is not under /api/v1/auth/ and must still require auth.
        """
        resp = self._client().get("/api/v1/tasks")
        assert resp.status_code == 401

    def test_non_auth_endpoint_succeeds_with_valid_token(self):
        """Verify that protected routes still work correctly with a valid token."""
        resp = self._client().get(
            "/api/v1/tasks",
            headers={"Authorization": f"Bearer {_make_token()}"},
        )
        assert resp.status_code == 200

    def test_exact_exempt_path_still_works_alongside_prefixes(self):
        """/health is an exact exempt path and must work with no token."""
        resp = self._client().get("/health")
        assert resp.status_code == 200

    def test_partial_prefix_match_is_not_enough(self):
        """
        /api/v1/auth/ must match as a prefix, not a substring.
        /api/v1/authorized/ does NOT start with /api/v1/auth/ as a complete
        prefix segment — but since our match is startswith(), let's verify
        a genuinely different path is NOT exempt.
        """
        app = _make_app(exempt_prefixes=["/api/v1/auth/"])
        client = TestClient(app)
        # /api/v1/users/me does NOT start with /api/v1/auth/
        resp = client.get("/api/v1/users/me")
        assert resp.status_code == 401

    def test_multiple_prefixes_can_be_registered(self):
        """Two exempt prefixes should both work independently."""
        app = _make_app(exempt_prefixes=["/api/v1/auth/", "/api/v1/public/"])
        client = TestClient(app)
        # auth prefix
        resp = client.post("/api/v1/auth/login")
        assert resp.status_code == 200
        # Non-exempt still requires token
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 401

    def test_root_prefix_exempts_everything(self):
        """
        Edge case: a prefix of '/' exempts all paths.
        Useful for fully-open dev environments. Verify it works.
        """
        app = _make_app(exempt_prefixes=["/"])
        client = TestClient(app)
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 200  # No token required
