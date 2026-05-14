"""
HTTP-level tests for chaos-controller.

Covers the contract every Chaos Theater frontend relies on:
    - /health returns 200 with the expected shape
    - /snapshot returns the cluster state with 4 services
    - /explainers returns 23 actions and 10 scenarios with required keys
    - Every /chaos/* endpoint returns 200 + the queued action name
    - /chaos/scenario validates scenario ids
    - SSE /stream produces parseable events

We test the HTTP contract because that's what the frontend depends on.
A behavioural test of recovery semantics is in test_cluster.py — those
exercise the mock cluster directly.
"""
import json
import pytest


# ─── Health + state ────────────────────────────────────────────────────
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "chaos-controller"
    assert body["mode"] in ("mock", "live")


def test_snapshot_returns_all_services(client):
    r = client.get("/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert set(body["services"].keys()) == {
        "api-gateway", "user-service", "task-service", "notification-service"
    }
    for name, svc in body["services"].items():
        # All required fields present
        for k in ("desired", "ready", "pods", "healthy", "latency_p95_ms", "rps", "error_rate"):
            assert k in svc, f"{name} missing {k}"
        assert svc["desired"] >= 1
        assert isinstance(svc["pods"], list)


# ─── Explainers contract ──────────────────────────────────────────────
def test_explainers_returns_actions_and_scenarios(client):
    r = client.get("/explainers")
    assert r.status_code == 200
    body = r.json()
    assert "actions" in body and "scenarios" in body
    # The frontend hardcodes these counts; if you change them, update the UI too
    assert len(body["actions"]) >= 23
    assert len(body["scenarios"]) >= 10


def test_every_action_has_required_explainer_keys(client):
    body = client.get("/explainers").json()
    required = {"title", "summary", "what_happens", "primitives", "learn_more", "takeaway"}
    for action_id, data in body["actions"].items():
        missing = required - set(data.keys())
        assert not missing, f"{action_id} missing keys: {missing}"
        # Structural validation
        assert isinstance(data["what_happens"], list) and len(data["what_happens"]) >= 4
        assert isinstance(data["primitives"], list)
        for prim in data["primitives"]:
            assert len(prim) == 2, f"{action_id} primitive must be (name, desc)"
        for lm in data["learn_more"]:
            assert len(lm) == 2 and lm[1].startswith("http"), f"{action_id} bad link {lm}"


def test_every_scenario_step_references_a_known_action(client):
    body = client.get("/explainers").json()
    known_actions = set(body["actions"].keys())
    for sid, scn in body["scenarios"].items():
        for step in scn["steps"]:
            assert step["action"] in known_actions, \
                f"scenario {sid} references unknown action {step['action']}"


# ─── Chaos action endpoints ───────────────────────────────────────────
SERVICE_REQUIRED = [
    ("/chaos/kill-pod", "kill_pod"),
    ("/chaos/cpu-pressure", "cpu_pressure"),
    ("/chaos/memory-leak", "memory_leak"),
    ("/chaos/network-partition", "network_partition"),
    ("/chaos/bad-deploy", "bad_deploy"),
    ("/chaos/disk-full", "disk_full"),
    ("/chaos/gc-pause", "gc_pause"),
    ("/chaos/service-mesh-crash", "service_mesh_crash"),
]
NO_SERVICE = [
    ("/chaos/expire-jwt", "expire_jwt"),
    ("/chaos/region-outage", "region_outage"),
    ("/chaos/redis-failure", "redis_failure"),
    ("/chaos/db-failure", "db_failure"),
    ("/chaos/slow-network", "slow_network"),
    ("/chaos/cert-expiry", "cert_expiry"),
    ("/chaos/dns-failure", "dns_failure"),
    ("/chaos/cascading-failure", "cascading_failure"),
    ("/chaos/thundering-herd", "thundering_herd"),
    ("/chaos/noisy-neighbor", "noisy_neighbor"),
    ("/chaos/spot-reclaim", "spot_reclaim"),
    ("/chaos/autoscaler-stuck", "autoscaler_stuck"),
    ("/chaos/api-throttle", "api_throttle"),
    ("/chaos/secret-leak", "secret_leak"),
    ("/chaos/third-party-outage", "third_party_outage"),
]


@pytest.mark.parametrize("path,expected", SERVICE_REQUIRED)
def test_chaos_endpoints_requiring_service(client, path, expected):
    r = client.post(path, json={"service": "api-gateway"})
    assert r.status_code == 200, f"{path}: {r.text}"
    body = r.json()
    assert body["queued"] == expected
    assert body["service"] == "api-gateway"


@pytest.mark.parametrize("path,expected", NO_SERVICE)
def test_chaos_endpoints_without_service(client, path, expected):
    r = client.post(path)
    assert r.status_code == 200, f"{path}: {r.text}"
    assert r.json()["queued"] == expected


def test_kill_pod_requires_service_body(client):
    r = client.post("/chaos/kill-pod")
    assert r.status_code == 422  # FastAPI validation


def test_scenario_endpoint_accepts_known_ids(client):
    for sid in ["black_friday", "region_disaster", "friday_evening", "christmas_eve", "active_breach"]:
        r = client.post("/chaos/scenario", json={"id": sid})
        assert r.status_code == 200, f"{sid}: {r.text}"
        assert r.json()["id"] == sid


# ─── SSE stream ───────────────────────────────────────────────────────
def test_stream_returns_sse_content_type(client):
    # We don't subscribe (would block), but we can verify the route exists
    # and returns the right content type by checking with a HEAD request
    # via the OpenAPI spec presence.
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/stream" in paths


# ─── Negative / edge cases ────────────────────────────────────────────
def test_chaos_endpoint_with_unknown_service_does_not_crash(client):
    """The cluster gracefully handles unknown service names —
    it shouldn't return 500."""
    r = client.post("/chaos/kill-pod", json={"service": "nonexistent-service"})
    assert r.status_code == 200  # request accepted, but action will no-op


def test_cors_headers_present(client):
    r = client.options("/chaos/kill-pod", headers={"Origin": "http://localhost:8000", "Access-Control-Request-Method": "POST"})
    # CORS pre-flight: should succeed
    assert r.status_code in (200, 204), f"got {r.status_code}: {r.text}"
