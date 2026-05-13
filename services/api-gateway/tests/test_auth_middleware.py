"""
tests/test_auth_middleware.py

Tests for the JWT authentication middleware.

KEY BEHAVIOURS TESTED:
  1. Valid JWT → request proceeds, user_id/email/roles set on request.state.
  2. Missing Authorization header → 401 with WWW-Authenticate: Bearer.
  3. Expired token → 401 "Token expired".
  4. Tampered/invalid token → 401 "Invalid token".
  5. JWT 'sub' claim not a valid UUID → 401 "Invalid token claims".
     This is the fix for the type-confusion attack on downstream services.
  6. Exempt paths bypass auth entirely (health, metrics).
  7. Case-insensitive "Bearer" prefix is accepted (RFC 7230 normalisation).
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

# conftest sets env vars before this
from app.middleware.auth import JWTAuthMiddleware

# Use the same secret set in conftest.py
_SECRET = os.environ["JWT_SECRET"]
_ALGORITHM = "HS256"


def _make_token(
    sub: str = str(uuid.uuid4()),
    exp_delta: timedelta = timedelta(hours=1),
    secret: str = _SECRET,
    algorithm: str = _ALGORITHM,
    **extra_claims,
) -> str:
    payload = {
        "sub": sub,
        "email": "test@example.com",
        "roles": ["user"],
        "exp": datetime.now(timezone.utc) + exp_delta,
        "iat": datetime.now(timezone.utc),
        **extra_claims,
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def _make_app():
    async def protected(request: Request):
        return JSONResponse({
            "user_id": getattr(request.state, "user_id", None),
            "user_email": getattr(request.state, "user_email", None),
        })

    app = Starlette(routes=[
        Route("/api/v1/tasks", protected),
        Route("/health", protected),
        Route("/metrics", protected),
    ])
    app.add_middleware(
        JWTAuthMiddleware,
        exempt_paths=["/health", "/metrics"],
    )
    return app


class TestJWTAuthMiddleware:

    def test_valid_token_passes_through(self):
        client = TestClient(_make_app(), raise_server_exceptions=True)
        token = _make_token()
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] is not None
        assert data["user_email"] == "test@example.com"

    def test_missing_auth_header_returns_401(self):
        client = TestClient(_make_app())
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"

    def test_expired_token_returns_401(self):
        client = TestClient(_make_app())
        token = _make_token(exp_delta=timedelta(seconds=-1))
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_tampered_token_returns_401(self):
        client = TestClient(_make_app())
        token = _make_token() + "tampered"
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_wrong_secret_returns_401(self):
        client = TestClient(_make_app())
        token = _make_token(secret="completely-different-secret-that-is-long-enough")
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_non_uuid_sub_returns_401(self):
        """
        FIX #7: A JWT with sub='../../admin' or sub='*' must be rejected.
        Without this check, type-confusion bugs downstream could be exploited.
        """
        client = TestClient(_make_app())
        token = _make_token(sub="not-a-uuid-at-all")
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "claims" in resp.json()["detail"].lower()

    def test_sub_path_traversal_attempt_rejected(self):
        """A crafted sub that looks like a path traversal."""
        client = TestClient(_make_app())
        token = _make_token(sub="../../etc/passwd")
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401

    def test_exempt_path_skips_auth(self):
        """Health and metrics endpoints must work with no token at all."""
        client = TestClient(_make_app())
        for path in ["/health", "/metrics"]:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} should be exempt from auth"

    def test_case_insensitive_bearer_scheme(self):
        """RFC 7230: header field names are case-insensitive. 'bearer' == 'Bearer'."""
        client = TestClient(_make_app())
        token = _make_token()
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"bearer {token}"})
        assert resp.status_code == 200

    def test_user_id_is_canonical_uuid_string(self):
        """Sub claim should be normalised to lowercase UUID string."""
        user_id = uuid.uuid4()
        client = TestClient(_make_app())
        token = _make_token(sub=str(user_id).upper())  # uppercase UUID
        resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        # Should be normalised to lowercase canonical form
        assert resp.json()["user_id"] == str(user_id).lower()
