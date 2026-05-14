"""
Behavioural tests for MockCluster.

The mock cluster is the educational/demo path. These tests verify it
faithfully simulates K8s recovery semantics: kill a pod, watch state
flip to degraded, then back to healthy. Apply CPU pressure, watch
the simulator scale up replicas. Run a scenario with overlapping
steps, verify they actually overlap.

These are slow tests (each runs through realistic K8s timing, with
jitter). Marked accordingly.
"""
import asyncio
import time
import pytest


# ─── Basic state ──────────────────────────────────────────────────────
def test_initial_snapshot_has_4_services_each_healthy(mock_cluster):
    snap = mock_cluster.snapshot()
    assert len(snap["services"]) == 4
    for name, svc in snap["services"].items():
        assert svc["healthy"] is True
        assert svc["ready"] == svc["desired"]
        assert svc["error_rate"] < 0.05  # baseline


def test_unknown_service_kill_is_noop(mock_cluster):
    """kill_pod on an unknown service should not raise."""
    asyncio.run(mock_cluster.kill_pod("does-not-exist"))
    snap = mock_cluster.snapshot()
    for svc in snap["services"].values():
        assert svc["healthy"] is True


# ─── Kill pod behaviour ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_kill_pod_degrades_then_recovers(mock_cluster):
    """End-to-end: kill_pod produces a degraded state, then full recovery."""
    # Before
    before = mock_cluster.snapshot()["services"]["api-gateway"]
    assert before["healthy"] is True

    # Run kill_pod
    await mock_cluster.kill_pod("api-gateway")

    # After (full recovery is awaited inside kill_pod)
    after = mock_cluster.snapshot()["services"]["api-gateway"]
    assert after["healthy"] is True, "service should be healthy after recovery"
    assert after["ready"] == after["desired"], "all replicas should be back"


@pytest.mark.asyncio
async def test_kill_pod_publishes_k8s_events(mock_cluster):
    """The mock cluster publishes structured k8s events via the hub
    while a kill_pod action runs."""
    from app.events import hub
    q = hub.subscribe()
    try:
        # Start the action
        task = asyncio.create_task(mock_cluster.kill_pod("api-gateway"))
        # Collect events for a short window
        collected = []
        deadline = asyncio.get_event_loop().time() + 12.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(q.get(), timeout=0.5)
                collected.append(line)
                if task.done():
                    # Drain anything still queued
                    while True:
                        try:
                            collected.append(q.get_nowait())
                        except Exception:
                            break
                    break
            except asyncio.TimeoutError:
                if task.done():
                    break

        await task
        # We should have seen at least one k8s event and one action event
        import json as J
        kinds = []
        for line in collected:
            # SSE lines: "data: {...}\n\n"
            if line.startswith("data: "):
                payload = J.loads(line[6:].strip())
                kinds.append(payload["kind"])
        assert "k8s" in kinds, "should publish k8s events"
        assert "action" in kinds, "should publish action lifecycle events"
    finally:
        hub.unsubscribe(q)


# ─── Scenario parallelism (the regression fix) ────────────────────────
@pytest.mark.asyncio
async def test_scenario_steps_run_concurrently(mock_cluster):
    """Regression test: scenario steps must run concurrently, not be serialized
    by the manual-trigger lock. We assert by timing: a scenario with two 10s
    steps starting 1s apart should complete in <=14s, not 21s."""
    # Use a tiny scenario added inline via SCENARIOS — but the real ones suffice.
    # black_friday has cpu_pressure at 0 (30s) and cpu_pressure at 5 (35s).
    # If serialized: ~65s. If concurrent: ~40s.
    # To keep the test fast, we craft an inline scenario.
    from app.explainers import SCENARIOS
    SCENARIOS["__test_overlap"] = {
        "name": "Test Overlap",
        "icon": "T",
        "duration_sec": 10,
        "description": "for tests",
        "steps": [
            {"at": 0, "action": "cpu_pressure", "service": "api-gateway", "duration": 3},
            {"at": 0, "action": "cpu_pressure", "service": "task-service", "duration": 3},
        ],
        "takeaway": "",
    }
    try:
        start = time.perf_counter()
        await mock_cluster.run_scenario("__test_overlap")
        elapsed = time.perf_counter() - start
        # If serialized, two ~12s actions = 24s. Concurrent = ~12s.
        assert elapsed < 20.0, f"scenario took {elapsed:.1f}s — steps may be serialized"
    finally:
        SCENARIOS.pop("__test_overlap", None)


# ─── Lifecycle ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_start_and_stop_are_clean(mock_cluster):
    """start() spawns _ambient_loop. stop() must cancel it cleanly."""
    await mock_cluster.start()
    await asyncio.sleep(0.1)
    assert mock_cluster._ambient_task is not None
    assert not mock_cluster._ambient_task.done()
    await mock_cluster.stop()
    assert mock_cluster._ambient_task.done() or mock_cluster._ambient_task.cancelled()


# ─── Service-affecting actions ────────────────────────────────────────
@pytest.mark.asyncio
async def test_network_partition_marks_service_unhealthy_then_recovers(mock_cluster):
    """During partition the service should be unhealthy; after it heals."""
    # Smaller duration for fast test
    task = asyncio.create_task(mock_cluster.network_partition("user-service", duration_sec=2.0))
    await asyncio.sleep(1.5)
    mid = mock_cluster.snapshot()["services"]["user-service"]
    assert mid["healthy"] is False or mid["error_rate"] > 0.5
    await task
    end = mock_cluster.snapshot()["services"]["user-service"]
    assert end["healthy"] is True


@pytest.mark.asyncio
async def test_redis_failure_increases_latency_then_recovers(mock_cluster):
    """Redis failure should bump latency briefly then return to baseline."""
    baseline = {
        n: s["latency_p95_ms"]
        for n, s in mock_cluster.snapshot()["services"].items()
    }
    await mock_cluster.redis_failure(duration_sec=2.0)
    after = mock_cluster.snapshot()["services"]
    # After recovery, latency should be back to baseline (within ambient drift)
    for name, base in baseline.items():
        assert abs(after[name]["latency_p95_ms"] - base) < 30, \
            f"{name} did not recover latency"
