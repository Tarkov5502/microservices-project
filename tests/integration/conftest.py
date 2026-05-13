"""
tests/integration/conftest.py

Shared fixtures for the cross-service integration tests. These tests run
against a LIVE docker-compose stack — they're not unit tests. The CI pipeline
brings the stack up before running them; locally, run:

    docker compose up -d --wait    # boot everything, wait for health checks
    pytest tests/integration -v
    docker compose down -v          # clean up

If the gateway is unreachable, every test is SKIPPED rather than failing.
That keeps the suite useful even when someone runs `pytest` without first
starting the stack — you get a clear "skipped, stack not running" instead
of a misleading "test failure".

ENV OVERRIDES:
  GATEWAY_URL  — full URL to the API gateway (default http://localhost:8000)
  STARTUP_WAIT — max seconds to wait for the gateway to become reachable (60)
"""
import os
import time
import uuid

import httpx
import pytest


GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
STARTUP_WAIT = int(os.environ.get("STARTUP_WAIT", "60"))


def _gateway_reachable() -> bool:
    """Probe the gateway's readiness endpoint with a short timeout."""
    try:
        r = httpx.get(f"{GATEWAY_URL}/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def _wait_for_gateway() -> bool:
    """Block up to STARTUP_WAIT seconds for the gateway to come up."""
    deadline = time.monotonic() + STARTUP_WAIT
    while time.monotonic() < deadline:
        if _gateway_reachable():
            return True
        time.sleep(1)
    return False


@pytest.fixture(scope="session", autouse=True)
def _require_stack():
    """Skip every test in this directory if the stack isn't running."""
    if not _gateway_reachable():
        if not _wait_for_gateway():
            pytest.skip(
                f"Integration tests require a running stack at {GATEWAY_URL}. "
                "Run `docker compose up -d --wait` first, then re-run pytest.",
                allow_module_level=True,
            )


@pytest.fixture
def client():
    """A long-lived httpx client pointed at the gateway."""
    with httpx.Client(base_url=GATEWAY_URL, timeout=10.0) as c:
        yield c


@pytest.fixture
def unique_user_factory():
    """
    Factory that generates a unique (email, username, password, full_name)
    tuple per call. Tests use this to avoid colliding when run in parallel
    or after a previous run that didn't clean up.

    PASSWORD COMPLEXITY: matches the user-service schema rules
    (≥ 8 chars, at least one uppercase, at least one digit).
    """
    def _make():
        token = uuid.uuid4().hex[:10]
        return {
            "email":     f"itest-{token}@example.com",
            "username":  f"itest_{token}",
            "password":  f"Itest{token[:3]}1!",
            "full_name": "Integration Test User",
        }
    return _make


@pytest.fixture
def registered_user(client, unique_user_factory):
    """
    Register a fresh user via the API and return (creds, profile).

    creds:   the original dict suitable for POST /auth/login
    profile: the response body from /auth/register (id, email, etc.)
    """
    creds = unique_user_factory()
    r = client.post("/api/v1/auth/register", json=creds)
    assert r.status_code == 201, r.text
    return creds, r.json()


@pytest.fixture
def logged_in_user(client, registered_user):
    """
    Register + log in. Returns (token_response, creds) so tests can use the
    access token directly.
    """
    creds, _profile = registered_user
    r = client.post("/api/v1/auth/login", json={
        "email": creds["email"],
        "password": creds["password"],
    })
    assert r.status_code == 200, r.text
    return r.json(), creds


@pytest.fixture
def auth_headers(logged_in_user):
    """Bearer-token Authorization header for an authenticated user."""
    token_response, _ = logged_in_user
    return {"Authorization": f"Bearer {token_response['access_token']}"}
