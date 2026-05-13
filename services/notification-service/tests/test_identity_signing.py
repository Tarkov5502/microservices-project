"""
tests/test_identity_signing.py

Validates the HMAC-signed identity envelope used between the api-gateway and
the backend services.
"""
import time

import pytest

from app.identity_signing import (
    IDENTITY_SIG_HEADER,
    IDENTITY_TS_HEADER,
    IdentityVerifierMiddleware,
    sign_identity,
    verify_identity,
)


SECRET = "0123456789abcdef" * 2  # 32 chars


def test_signed_envelope_round_trip():
    """A freshly-signed envelope verifies."""
    sig, ts = sign_identity(SECRET, "alice", "alice@x.io", "user,admin")
    assert verify_identity(SECRET, "alice", "alice@x.io", "user,admin", ts, sig)


def test_tampered_user_id_fails_verification():
    """Flipping any signed field invalidates the signature."""
    sig, ts = sign_identity(SECRET, "alice", "alice@x.io", "user")
    assert not verify_identity(SECRET, "mallory", "alice@x.io", "user", ts, sig)


def test_tampered_email_fails():
    sig, ts = sign_identity(SECRET, "alice", "alice@x.io", "user")
    assert not verify_identity(SECRET, "alice", "evil@x.io", "user", ts, sig)


def test_tampered_roles_fails():
    sig, ts = sign_identity(SECRET, "alice", "alice@x.io", "user")
    assert not verify_identity(SECRET, "alice", "alice@x.io", "user,admin", ts, sig)


def test_wrong_secret_fails():
    """An attacker without the secret can't forge a valid signature."""
    sig, ts = sign_identity(SECRET, "alice", "alice@x.io", "user")
    assert not verify_identity("a-different-secret-of-sufficient-length", "alice", "alice@x.io", "user", ts, sig)


def test_old_signature_fails_freshness_check():
    """A signature from beyond the freshness window is rejected."""
    sig, ts = sign_identity(SECRET, "alice", "alice@x.io", "user", issued_at=1000)
    # 'now' is way past the freshness window
    assert not verify_identity(SECRET, "alice", "alice@x.io", "user", ts, sig, now=1000 + 9999)


def test_future_signature_within_skew_passes():
    """Up to 5 s of forward clock skew is tolerated."""
    future = int(time.time()) + 3
    sig, ts = sign_identity(SECRET, "alice", "alice@x.io", "user", issued_at=future)
    assert verify_identity(SECRET, "alice", "alice@x.io", "user", ts, sig)


def test_far_future_signature_fails():
    """Beyond the 5 s skew, future-dated signatures are suspicious and fail."""
    future = int(time.time()) + 600
    sig, ts = sign_identity(SECRET, "alice", "alice@x.io", "user", issued_at=future)
    assert not verify_identity(SECRET, "alice", "alice@x.io", "user", ts, sig)


def test_malformed_timestamp_fails():
    sig, _ = sign_identity(SECRET, "alice", "alice@x.io", "user")
    assert not verify_identity(SECRET, "alice", "alice@x.io", "user", "not-a-number", sig)


def test_malformed_signature_fails():
    """Garbage in the signature header is rejected, not throwing."""
    _, ts = sign_identity(SECRET, "alice", "alice@x.io", "user")
    assert not verify_identity(SECRET, "alice", "alice@x.io", "user", ts, "!@#$ not base64 !@#$")


def test_secret_too_short_rejected_at_middleware_construction():
    """Backends must refuse to start with a weak HMAC secret."""
    from fastapi import FastAPI
    with pytest.raises(ValueError, match="≥ 32"):
        IdentityVerifierMiddleware(FastAPI(), secret="short")


# ─── End-to-end middleware test via Starlette ────────────────────────────────

def _build_app(secret: str):
    from fastapi import FastAPI, Request
    app = FastAPI()
    app.add_middleware(
        IdentityVerifierMiddleware,
        secret=secret,
        exempt_paths=["/health"],
    )

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/whoami")
    async def whoami(request: Request):
        return {"user_id": request.headers.get("X-User-Id", "")}

    return app


def test_middleware_blocks_unsigned_identity():
    """A request with X-User-Id but no signature is rejected."""
    from fastapi.testclient import TestClient
    client = TestClient(_build_app(SECRET))
    r = client.get("/whoami", headers={"X-User-Id": "550e8400-e29b-41d4-a716-446655440000"})
    assert r.status_code == 401
    assert "signature" in r.json()["detail"].lower()


def test_middleware_allows_anonymous_request():
    """No identity headers at all → pass through (route can still 401)."""
    from fastapi.testclient import TestClient
    client = TestClient(_build_app(SECRET))
    r = client.get("/whoami")
    assert r.status_code == 200


def test_middleware_accepts_valid_signature():
    """A correctly-signed envelope reaches the handler."""
    from fastapi.testclient import TestClient
    sig, ts = sign_identity(SECRET, "550e8400-e29b-41d4-a716-446655440000", "a@b.com", "user")
    client = TestClient(_build_app(SECRET))
    r = client.get("/whoami", headers={
        "X-User-Id":         "550e8400-e29b-41d4-a716-446655440000",
        "X-User-Email":      "a@b.com",
        "X-User-Roles":      "user",
        IDENTITY_SIG_HEADER: sig,
        IDENTITY_TS_HEADER:  ts,
    })
    assert r.status_code == 200
    assert r.json()["user_id"] == "550e8400-e29b-41d4-a716-446655440000"


def test_middleware_rejects_non_uuid_user_id_even_with_valid_signature():
    """Belt-and-braces: signed but malformed user_id is still rejected."""
    from fastapi.testclient import TestClient
    sig, ts = sign_identity(SECRET, "not-a-uuid", "x@x.com", "user")
    client = TestClient(_build_app(SECRET))
    r = client.get("/whoami", headers={
        "X-User-Id":         "not-a-uuid",
        "X-User-Email":      "x@x.com",
        "X-User-Roles":      "user",
        IDENTITY_SIG_HEADER: sig,
        IDENTITY_TS_HEADER:  ts,
    })
    assert r.status_code == 401


def test_middleware_exempts_health_path():
    """Health probes are exempt — kubelet doesn't have the HMAC secret."""
    from fastapi.testclient import TestClient
    client = TestClient(_build_app(SECRET))
    r = client.get("/health")
    assert r.status_code == 200
