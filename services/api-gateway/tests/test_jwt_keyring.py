"""
tests/test_jwt_keyring.py

Tests for the JWT keyring + kid-based rotation. Verifies both the parser and
the end-to-end behaviour: a token signed with key A is rejected once A is
removed from the keyring, but accepted while A is still listed alongside B.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.jwt_keyring import (
    KeyringError,
    parse_keyring,
    select_signing_key,
    select_verification_key,
)


def test_empty_secrets_falls_back_to_single_key():
    ring = parse_keyring("", "legacy-secret-32-chars-or-more!", fallback_kid="default")
    assert ring == {"default": "legacy-secret-32-chars-or-more!"}


def test_single_pair_parses():
    ring = parse_keyring("k1=abc", "ignored", fallback_kid="default")
    assert ring == {"k1": "abc"}


def test_two_pairs_parse():
    ring = parse_keyring("k1=abc,k2=xyz", "ignored")
    assert ring == {"k1": "abc", "k2": "xyz"}


def test_duplicate_kid_raises():
    with pytest.raises(KeyringError, match="Duplicate"):
        parse_keyring("k1=abc,k1=xyz", "")


def test_malformed_entry_no_equals_raises():
    with pytest.raises(KeyringError, match="kid=secret"):
        parse_keyring("just-a-string", "")


def test_empty_kid_raises():
    with pytest.raises(KeyringError, match="empty kid"):
        parse_keyring("=abc", "")


def test_select_signing_key_returns_current():
    ring = {"k1": "abc", "k2": "xyz"}
    kid, secret = select_signing_key(ring, "k2")
    assert kid == "k2"
    assert secret == "xyz"


def test_select_signing_key_unknown_kid_raises():
    ring = {"k1": "abc"}
    with pytest.raises(KeyringError, match="not present"):
        select_signing_key(ring, "k999")


def test_select_verification_known_kid():
    ring = {"k1": "abc", "k2": "xyz"}
    assert select_verification_key(ring, "k1") == "abc"
    assert select_verification_key(ring, "k2") == "xyz"


def test_select_verification_unknown_kid_returns_none():
    ring = {"k1": "abc"}
    assert select_verification_key(ring, "k999") is None


def test_select_verification_no_kid_falls_back_to_default():
    """Old tokens (pre-rotation) have no kid; they should still verify against
    the 'default' key when present."""
    ring = {"default": "legacy"}
    assert select_verification_key(ring, None) == "legacy"


def test_select_verification_no_kid_no_default_returns_none():
    ring = {"k1": "abc"}
    assert select_verification_key(ring, None) is None


# ─── End-to-end: middleware accepts tokens signed under any keyring entry ──

@pytest.fixture
def jwt_secret_setup(monkeypatch):
    """Reload the gateway settings + middleware with a keyring."""
    # Two keys in the ring. New tokens signed with k2; k1 still accepted.
    monkeypatch.setenv("JWT_SECRET", "0123456789abcdef0123456789abcdef")  # legacy fallback
    monkeypatch.setenv("JWT_SECRETS", "k1=keyone-thirty-two-chars-aaaaaaa,k2=keytwo-thirty-two-chars-bbbbbb")
    monkeypatch.setenv("JWT_CURRENT_KID", "k2")
    # Force config + middleware reload so the new env applies
    import importlib
    import app.config as config_module
    importlib.reload(config_module)
    import app.middleware.auth as auth_module
    importlib.reload(auth_module)
    return config_module, auth_module


def _make_token(secret: str, kid: str | None = None, sub: str | None = None) -> str:
    payload = {
        "sub": sub or str(uuid.uuid4()),
        "email": "test@example.com",
        "roles": ["user"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "iat": datetime.now(timezone.utc),
    }
    headers = {"kid": kid} if kid else {}
    return jwt.encode(payload, secret, algorithm="HS256", headers=headers)


def _make_app(auth_module):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def protected(request):
        return JSONResponse({"user_id": getattr(request.state, "user_id", None)})

    app = Starlette(routes=[Route("/p", protected)])
    app.add_middleware(auth_module.JWTAuthMiddleware, exempt_paths=[])
    return app


def test_middleware_accepts_token_signed_with_current_kid(jwt_secret_setup):
    _, auth_module = jwt_secret_setup
    from starlette.testclient import TestClient
    token = _make_token("keytwo-thirty-two-chars-bbbbbb", kid="k2")
    r = TestClient(_make_app(auth_module)).get("/p", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_middleware_still_accepts_old_kid_during_rotation(jwt_secret_setup):
    """During rotation, tokens signed with the old kid must still verify."""
    _, auth_module = jwt_secret_setup
    from starlette.testclient import TestClient
    token = _make_token("keyone-thirty-two-chars-aaaaaaa", kid="k1")
    r = TestClient(_make_app(auth_module)).get("/p", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_middleware_rejects_unknown_kid(jwt_secret_setup):
    """An attacker who forges a token claiming a non-existent kid is rejected."""
    _, auth_module = jwt_secret_setup
    from starlette.testclient import TestClient
    token = _make_token("attacker-secret-pretending-to-be-real-32", kid="k999")
    r = TestClient(_make_app(auth_module)).get("/p", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_middleware_rejects_signature_mismatch_on_known_kid(jwt_secret_setup):
    """A token that claims k1 but is signed with a different secret is rejected."""
    _, auth_module = jwt_secret_setup
    from starlette.testclient import TestClient
    token = _make_token("attacker-secret-pretending-to-be-real-32", kid="k1")
    r = TestClient(_make_app(auth_module)).get("/p", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
