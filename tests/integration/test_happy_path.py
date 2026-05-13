"""
tests/integration/test_happy_path.py

End-to-end happy path that exercises the gateway, user-service, task-service,
and the auth + project + task flows in a single pytest run. This is the test
that would have caught:

  - The login deadlock (auth routes not exempt from JWT middleware)
  - The project-ownership BOLA fix
  - JWT signature drift between gateway and user-service
  - Any future regression where the request leaves the gateway with the wrong
    X-User-* headers

Each test owns its own fresh user so they can run in parallel without
collisions. Database state is intentionally not cleaned up between runs —
we use UUIDs in emails and the deletion test exercises the GDPR endpoint.
"""
import time
import uuid

import pytest


# ─── Auth ────────────────────────────────────────────────────────────────────


def test_register_login_logout_cycle(client, unique_user_factory):
    """Full lifecycle: create account, log in, log out, refresh fails."""
    creds = unique_user_factory()

    # Register
    r = client.post("/api/v1/auth/register", json=creds)
    assert r.status_code == 201, r.text
    profile = r.json()
    assert profile["email"] == creds["email"]
    assert "hashed_password" not in profile  # password never echoed back

    # Login
    r = client.post("/api/v1/auth/login", json={
        "email": creds["email"], "password": creds["password"],
    })
    assert r.status_code == 200
    tokens = r.json()
    assert tokens["access_token"]
    assert tokens["token_type"] == "bearer"
    assert tokens["refresh_token"]  # opaque UUID

    # Use the access token to read /me
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = client.get("/api/v1/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["email"] == creds["email"]

    # Logout revokes the refresh token
    r = client.post("/api/v1/auth/logout", json={
        "refresh_token": tokens["refresh_token"],
    })
    assert r.status_code == 204

    # Refresh with the now-revoked token should fail
    r = client.post("/api/v1/auth/refresh", json={
        "refresh_token": tokens["refresh_token"],
    })
    assert r.status_code == 401


def test_unauthenticated_request_returns_401(client):
    r = client.get("/api/v1/users/me")
    assert r.status_code == 401


def test_wrong_password_returns_401(client, registered_user):
    creds, _ = registered_user
    r = client.post("/api/v1/auth/login", json={
        "email": creds["email"], "password": "WrongPassword99",
    })
    assert r.status_code == 401


def test_duplicate_registration_returns_409(client, registered_user):
    creds, _ = registered_user
    r = client.post("/api/v1/auth/register", json=creds)
    assert r.status_code == 409


def test_password_must_meet_complexity_rules(client, unique_user_factory):
    bad = unique_user_factory()
    bad["password"] = "tooweak"  # too short, no uppercase, no digit
    r = client.post("/api/v1/auth/register", json=bad)
    assert r.status_code == 422


# ─── Token refresh + rotation ────────────────────────────────────────────────


def test_refresh_returns_new_tokens_and_invalidates_old(client, logged_in_user):
    tokens, _ = logged_in_user
    old_refresh = tokens["refresh_token"]

    r = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert r.status_code == 200
    new = r.json()
    assert new["access_token"] != tokens["access_token"]
    assert new["refresh_token"] != old_refresh

    # The old refresh token must not work twice
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert r.status_code == 401


# ─── Projects + tasks ────────────────────────────────────────────────────────


def test_full_project_and_task_lifecycle(client, auth_headers):
    """Create project → create task in it → list → update → delete."""
    # Create project
    r = client.post("/api/v1/projects/", json={
        "name": "Integration Project",
        "description": "Created by the integration test suite",
    }, headers=auth_headers)
    assert r.status_code == 201, r.text
    project = r.json()
    project_id = project["id"]

    # Create task in that project
    r = client.post("/api/v1/tasks/", json={
        "title": "Integration task",
        "project_id": project_id,
        "priority": "high",
    }, headers=auth_headers)
    assert r.status_code == 201, r.text
    task = r.json()
    task_id = task["id"]
    assert task["status"] == "todo"  # status is server-controlled

    # List tasks — should include the new one
    r = client.get("/api/v1/tasks/", headers=auth_headers)
    assert r.status_code == 200
    listed = [t["id"] for t in r.json()["items"]]
    assert task_id in listed

    # Patch task status
    r = client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"status": "in_progress"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"

    # Delete task
    r = client.delete(f"/api/v1/tasks/{task_id}", headers=auth_headers)
    assert r.status_code == 204

    # Fetching after delete: 404
    r = client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)
    assert r.status_code == 404


def test_cannot_create_task_in_someone_elses_project(
    client, auth_headers, registered_user, unique_user_factory,
):
    """
    BOLA regression test: user A creates a project; user B (different account)
    tries to create a task in A's project. Should 404 (not 403 — we don't
    leak existence).
    """
    # User A (auth_headers) creates a project
    r = client.post("/api/v1/projects/", json={"name": "A's secret project"}, headers=auth_headers)
    assert r.status_code == 201
    a_project_id = r.json()["id"]

    # User B logs in with their own credentials
    b_creds = unique_user_factory()
    client.post("/api/v1/auth/register", json=b_creds)
    rb = client.post("/api/v1/auth/login", json={
        "email": b_creds["email"], "password": b_creds["password"],
    })
    b_token = rb.json()["access_token"]
    b_headers = {"Authorization": f"Bearer {b_token}"}

    # B tries to create a task in A's project
    r = client.post("/api/v1/tasks/", json={
        "title": "Sneaky task",
        "project_id": a_project_id,
        "priority": "low",
    }, headers=b_headers)
    assert r.status_code == 404, (
        f"BOLA hole: user B was able to create a task in user A's project "
        f"(status={r.status_code})"
    )


def test_idempotency_key_returns_cached_response(client, auth_headers):
    """
    Same Idempotency-Key on a retry returns the original 201 + a header
    flagging the replay; no second row is created.
    """
    # Set up a project first
    r = client.post("/api/v1/projects/", json={"name": "Idempotency proj"}, headers=auth_headers)
    project_id = r.json()["id"]

    idem_key = str(uuid.uuid4())
    payload = {"title": "Idempotent task", "project_id": project_id, "priority": "low"}
    headers = {**auth_headers, "Idempotency-Key": idem_key}

    r1 = client.post("/api/v1/tasks/", json=payload, headers=headers)
    assert r1.status_code == 201
    task_id_first = r1.json()["id"]

    r2 = client.post("/api/v1/tasks/", json=payload, headers=headers)
    assert r2.status_code == 201
    assert r2.json()["id"] == task_id_first
    assert r2.headers.get("Idempotency-Key-Replay") == "true"


# ─── Security headers + rate limit headers ───────────────────────────────────


def test_security_headers_present_on_every_response(client):
    r = client.get("/health")
    # Gateway always wraps responses in the SecurityHeadersMiddleware set
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in r.headers


def test_rate_limit_headers_present(client, auth_headers):
    r = client.get("/api/v1/users/me", headers=auth_headers)
    # The gateway sets RateLimit headers on every non-exempt response.
    assert "X-RateLimit-Limit" in r.headers
    assert "X-RateLimit-Remaining" in r.headers
    assert "X-RateLimit-Reset" in r.headers


def test_oversize_body_rejected_at_gateway(client):
    """Bodies above the 1 MiB cap are rejected with 413 before reaching the backend."""
    # 2 MiB of 'A'. We POST to a path the gateway will route — even if the
    # route would 401 us, the body limit fires first because BodySize is
    # outermost in the middleware stack.
    big = b"A" * (2 * 1024 * 1024)
    r = client.post(
        "/api/v1/tasks/", content=big,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413


# ─── GDPR account deletion ───────────────────────────────────────────────────


def test_hard_delete_account_removes_user(client, unique_user_factory):
    """
    After DELETE /api/v1/users/me/permanent, the same credentials no longer
    authenticate (the row is gone). The email is therefore available for re-
    registration.
    """
    creds = unique_user_factory()
    r = client.post("/api/v1/auth/register", json=creds)
    assert r.status_code == 201

    r = client.post("/api/v1/auth/login", json={
        "email": creds["email"], "password": creds["password"],
    })
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    r = client.delete("/api/v1/users/me/permanent", headers=headers)
    assert r.status_code == 204

    # Login with the same creds should now fail
    r = client.post("/api/v1/auth/login", json={
        "email": creds["email"], "password": creds["password"],
    })
    assert r.status_code == 401

    # Email is available again — re-register succeeds
    r = client.post("/api/v1/auth/register", json=creds)
    assert r.status_code == 201
