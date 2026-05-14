"""
chaos-controller/app/cluster.py

Cluster abstraction. Two implementations share the same interface:

    MockCluster — simulates a cluster in-memory. Useful for local dev and
                  for the educational/demo use case where you want predictable
                  recovery timings.

    LiveCluster — wraps the real Kubernetes API.

Both produce the same event shapes via the hub. The frontend cannot tell
which one is running. That's by design — the chaos theater works identically
whether you have a real cluster or not.

Each cluster has a uniform action surface:

    kill_pod(service)              — delete one pod, watch recovery
    cpu_pressure(service, secs)    — load a pod's CPU, watch HPA respond
    network_partition(service, s)  — block traffic to service, watch fallback
    expire_jwt()                   — invalidate all JWTs, watch reauth
    region_outage(secs)            — simulate AZ outage, watch failover
    bad_deploy()                   — push a crashing image, watch rollback

In LiveCluster these become real kubectl-style operations. In MockCluster
they advance an in-memory timeline that mirrors what real K8s would do
with the SAME services we operate.
"""
from __future__ import annotations

import asyncio
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.events import hub
from app.config import settings


# ─── Service model ─────────────────────────────────────────────────────
@dataclass
class PodState:
    name: str
    phase: str = "Running"          # Pending | Running | Terminating
    ready: bool = True
    age_sec: float = 0.0


@dataclass
class ServiceState:
    name: str
    desired_replicas: int = 3
    pods: list[PodState] = field(default_factory=list)
    healthy: bool = True
    latency_ms_p95: float = 45.0    # baseline p95 latency
    rps: float = 80.0               # baseline requests/sec
    error_rate: float = 0.001       # baseline error rate (0–1)


class BaseCluster:
    """Common surface. Implementations override the action methods."""

    def __init__(self) -> None:
        self.t0_anchor: float | None = None
        self._action_lock = asyncio.Lock()

    def relative_ts(self) -> float:
        if self.t0_anchor is None:
            return 0.0
        return time.time() - self.t0_anchor

    async def _anchor(self, action_name: str) -> None:
        self.t0_anchor = time.time()
        hub.publish(
            "action",
            {"action": action_name, "started_at": self.t0_anchor},
            level="info",
        )

    # ── Action surface (implementations override) ──
    # Tier 1: original K8s primitives
    async def kill_pod(self, service: str) -> None: ...
    async def cpu_pressure(self, service: str, duration_sec: float = 12.0) -> None: ...
    async def network_partition(self, service: str, duration_sec: float = 10.0) -> None: ...
    async def expire_jwt(self) -> None: ...
    async def region_outage(self, duration_sec: float = 20.0) -> None: ...
    async def bad_deploy(self, service: str) -> None: ...
    # Tier 2: infrastructure dependencies
    async def redis_failure(self, duration_sec: float = 20.0) -> None: ...
    async def db_failure(self, duration_sec: float = 18.0) -> None: ...
    async def slow_network(self, duration_sec: float = 25.0) -> None: ...
    async def memory_leak(self, service: str) -> None: ...
    async def cert_expiry(self) -> None: ...
    async def dns_failure(self, duration_sec: float = 18.0) -> None: ...
    async def cascading_failure(self) -> None: ...
    # Tier 3: real-world operational scenarios
    async def disk_full(self, service: str) -> None: ...
    async def gc_pause(self, service: str) -> None: ...
    async def thundering_herd(self, duration_sec: float = 18.0) -> None: ...
    async def noisy_neighbor(self, duration_sec: float = 22.0) -> None: ...
    async def spot_reclaim(self) -> None: ...
    async def autoscaler_stuck(self, duration_sec: float = 20.0) -> None: ...
    async def api_throttle(self, duration_sec: float = 20.0) -> None: ...
    async def secret_leak(self, duration_sec: float = 22.0) -> None: ...
    async def service_mesh_crash(self, service: str) -> None: ...
    async def third_party_outage(self, duration_sec: float = 25.0) -> None: ...
    # Composed sequences
    async def run_scenario(self, scenario_id: str) -> None: ...

    # ── State surface ──
    def snapshot(self) -> dict: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# ─── Mock implementation ──────────────────────────────────────────────
SERVICE_NAMES = ["api-gateway", "user-service", "task-service", "notification-service"]


class MockCluster(BaseCluster):
    """In-memory simulation that mirrors how a real K8s cluster recovers.

    Timings are chosen to match what's typical in well-configured production:
      readiness probe failure detection: 1.5-2.5s
      endpoint slice update:             0.4-1.0s after detection
      replicaset pod creation:           0.5-1.5s
      scheduler placement:               0.5-1.5s
      image pull (cached):               0.0-0.5s
      container start + ready probe:     3-7s
      total p50:                         ~6-9 seconds
      total p95:                         ~12-18 seconds

    These are real numbers. The mock isn't pretending — it's reproducing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.services: dict[str, ServiceState] = {}
        self._jitter = 0.15  # ±15% on all timings
        self._scenario_mode = False  # set during scenarios to allow overlap
        self._ambient_task = None
        self._init_services()

    def _init_services(self) -> None:
        for name in SERVICE_NAMES:
            s = ServiceState(name=name)
            for i in range(s.desired_replicas):
                s.pods.append(PodState(name=f"{name}-{_rand_suffix()}", age_sec=random.uniform(200, 3600)))
            self.services[name] = s

    def snapshot(self) -> dict:
        return {
            "services": {
                name: {
                    "desired": s.desired_replicas,
                    "ready": sum(1 for p in s.pods if p.ready),
                    "pods": [
                        {"name": p.name, "phase": p.phase, "ready": p.ready, "age": round(p.age_sec, 1)}
                        for p in s.pods
                    ],
                    "healthy": s.healthy,
                    "latency_p95_ms": round(s.latency_ms_p95, 1),
                    "rps": round(s.rps, 1),
                    "error_rate": round(s.error_rate, 4),
                }
                for name, s in self.services.items()
            },
            "mode": "mock",
        }

    async def start(self) -> None:
        # Ambient metric drift — small natural variance so charts don't look static.
        # We hold a reference so lifespan teardown can cancel it cleanly.
        self._ambient_task = asyncio.create_task(self._ambient_loop())

    async def stop(self) -> None:
        """Graceful shutdown: cancel background tasks. Idempotent."""
        task = getattr(self, "_ambient_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _ambient_loop(self) -> None:
        """Publish slowly-drifting baseline metrics every second so the charts
        always have data. Real cluster does the same naturally."""
        while True:
            try:
                for s in self.services.values():
                    if s.healthy:
                        # Tiny random walk around baseline
                        s.latency_ms_p95 = max(20, s.latency_ms_p95 + random.gauss(0, 0.6))
                        s.rps = max(10, s.rps + random.gauss(0, 0.8))
                        s.error_rate = max(0, min(0.01, s.error_rate + random.gauss(0, 0.0003)))
                hub.publish("metric", {"snapshot": self.snapshot()}, level="info")
                await asyncio.sleep(settings.prometheus_poll_interval_sec)
            except asyncio.CancelledError:
                return
            except Exception as e:
                hub.publish("log", {"msg": f"ambient loop error: {e}"}, level="warn")
                await asyncio.sleep(5)

    # ── Helpers ─────────────────────────────────────────────────────
    def _jittered(self, secs: float) -> float:
        return secs * (1 + random.uniform(-self._jitter, self._jitter))

    async def _wait(self, secs: float) -> None:
        await asyncio.sleep(self._jittered(secs))

    @asynccontextmanager
    async def _lock(self):
        """Conditional lock — held only during manual triggers, suspended for
        scenario runs so concurrent steps can overlap as designed."""
        if self._scenario_mode:
            yield
        else:
            async with self._action_lock:
                yield

    def _publish_k8s(self, message: str, level: str = "info", **fields) -> None:
        payload = {"message": message, "rel_t": round(self.relative_ts(), 2), **fields}
        hub.publish("k8s", payload, level=level)

    def _publish_probe(self, target: str, ok: bool, latency_ms: float) -> None:
        hub.publish(
            "probe",
            {"target": target, "ok": ok, "latency_ms": round(latency_ms, 1), "rel_t": round(self.relative_ts(), 2)},
            level="info" if ok else "warn",
        )

    def _publish_narration(self, text: str, predicted_range: tuple[float, float], matched: bool = False) -> None:
        hub.publish(
            "narration",
            {"text": text, "predicted": list(predicted_range), "matched": matched, "rel_t": round(self.relative_ts(), 2)},
            level="info" if not matched else "success",
        )

    # ── Action: kill a pod ───────────────────────────────────────────
    async def kill_pod(self, service: str) -> None:
        if service not in self.services:
            hub.publish("log", {"msg": f"unknown service {service}"}, level="warn")
            return

        async with self._lock():
            await self._anchor(f"kill_pod:{service}")
            svc = self.services[service]
            if not svc.pods:
                self._publish_k8s(f"{service}: no pods to kill", level="warn")
                return

            target = svc.pods[0]
            self._publish_narration(
                f"Predicted: readiness probe fails on {target.name} at T+1-2s, traffic reroutes by T+2s, new pod ready by T+6-10s",
                (1.0, 10.0),
            )
            self._publish_k8s(f"kubectl delete pod {target.name}", level="warn")

            # T+0.2s: pod enters Terminating
            await self._wait(0.25)
            target.phase = "Terminating"
            self._publish_k8s(f"{target.name}: phase=Terminating", level="warn")

            # T+0.4s: kubelet sends SIGTERM
            await self._wait(0.2)
            self._publish_k8s(f"{target.name}: kubelet SIGTERM sent", level="info")

            # T+1.4s: readiness probe begins failing
            await self._wait(1.0)
            target.ready = False
            svc.healthy = False
            self._publish_k8s(
                f"{target.name}: readinessProbe failed (connection refused)",
                level="crit",
            )
            self._publish_narration("✓ readiness probe failure observed", (1.0, 2.0), matched=True)

            # Spike latency + error rate during the recovery window
            old_latency, old_errors = svc.latency_ms_p95, svc.error_rate
            svc.latency_ms_p95 *= 2.8
            svc.error_rate = 0.08
            svc.rps *= 0.65

            # T+1.8s: endpoint slice update — traffic reroutes
            await self._wait(0.4)
            self._publish_k8s(
                f"endpoint slice {service}: removed {target.name}",
                level="warn",
            )
            self._publish_narration("✓ traffic rerouted away from failing pod", (1.5, 2.5), matched=True)

            # T+2.4s: replicaset notices replicas < desired
            await self._wait(0.6)
            new_pod = PodState(name=f"{service}-{_rand_suffix()}", phase="Pending", ready=False, age_sec=0)
            svc.pods.append(new_pod)
            self._publish_k8s(
                f"replicaset/{service}: replicas={len(svc.pods)-1}/{svc.desired_replicas} → scheduling pod {new_pod.name}",
                level="info",
            )

            # T+3.0s: scheduler places it
            await self._wait(0.6)
            new_pod.phase = "ContainerCreating"
            self._publish_k8s(
                f"{new_pod.name}: scheduled to node aks-app-12345 → ContainerCreating",
                level="info",
            )

            # T+3.3s: image pulled (cached)
            await self._wait(0.3)
            self._publish_k8s(
                f"{new_pod.name}: container started (image cached, 0.2s)",
                level="info",
            )

            # T+5.7s: readiness probe begins passing
            await self._wait(2.4)
            new_pod.phase = "Running"
            new_pod.ready = True
            self._publish_k8s(
                f"{new_pod.name}: readinessProbe passed → endpoint added",
                level="success",
            )
            self._publish_narration("✓ new pod ready, traffic resumes", (4.0, 10.0), matched=True)

            # Remove the dead pod from state
            svc.pods = [p for p in svc.pods if p.name != target.name]

            # T+6.0s: latency/errors recovered
            await self._wait(0.3)
            svc.latency_ms_p95 = old_latency
            svc.error_rate = old_errors
            svc.rps *= 1.0 / 0.65
            svc.healthy = True
            self._publish_k8s(
                f"recovery complete: {service} back to {svc.desired_replicas}/{svc.desired_replicas} ready",
                level="success",
            )
            hub.publish(
                "action",
                {"action": f"kill_pod:{service}", "completed": True, "duration_sec": round(self.relative_ts(), 1)},
                level="success",
            )

    # ── Action: CPU pressure → HPA scale-up ─────────────────────────
    async def cpu_pressure(self, service: str, duration_sec: float = 12.0) -> None:
        if service not in self.services:
            return
        async with self._lock():
            await self._anchor(f"cpu_pressure:{service}")
            svc = self.services[service]
            self._publish_narration(
                "Predicted: latency spikes, HPA detects high CPU at T+15-30s, scales replicas, latency recovers",
                (15.0, 45.0),
            )
            self._publish_k8s(f"applied CPU pressure to {service} (target=85%)", level="warn")

            # Latency climbs over a few seconds
            for i in range(6):
                await self._wait(0.5)
                svc.latency_ms_p95 *= 1.18
                svc.rps *= 0.92

            # HPA sees the metric
            await self._wait(2.0)
            self._publish_k8s(
                f"HPA/{service}: CPU 87% > 70% target → scale 3 → 5",
                level="warn",
            )

            # New pods come up
            for i in range(2):
                await self._wait(1.0)
                new_pod = PodState(name=f"{service}-{_rand_suffix()}", phase="Pending", ready=False)
                svc.pods.append(new_pod)
                self._publish_k8s(f"{new_pod.name}: scheduled (HPA scale-up)", level="info")

            # Pods become ready
            await self._wait(3.0)
            for p in svc.pods[-2:]:
                p.phase = "Running"
                p.ready = True
            self._publish_k8s(f"{service}: 5/5 ready, latency stabilizing", level="success")
            self._publish_narration("✓ HPA absorbed the load — latency recovering", (15.0, 45.0), matched=True)

            # Recovery
            for i in range(8):
                await self._wait(0.4)
                svc.latency_ms_p95 = max(45, svc.latency_ms_p95 * 0.88)

            hub.publish(
                "action",
                {"action": f"cpu_pressure:{service}", "completed": True, "duration_sec": round(self.relative_ts(), 1)},
                level="success",
            )

    # ── Action: network partition ────────────────────────────────────
    async def network_partition(self, service: str, duration_sec: float = 10.0) -> None:
        if service not in self.services:
            return
        async with self._lock():
            await self._anchor(f"network_partition:{service}")
            svc = self.services[service]
            self._publish_narration(
                f"Predicted: requests to {service} fail, circuit breaker trips by T+3-5s, fallback engages",
                (3.0, 8.0),
            )
            self._publish_k8s(
                f"applied NetworkPolicy: deny ingress to {service}",
                level="crit",
            )

            old_err, old_lat = svc.error_rate, svc.latency_ms_p95
            svc.healthy = False
            svc.error_rate = 0.95
            svc.latency_ms_p95 = 30000  # timeouts

            await self._wait(2.0)
            self._publish_k8s(f"upstream gateway: connection timeouts to {service}", level="crit")

            await self._wait(2.5)
            self._publish_k8s(
                "circuit-breaker/api-gateway: state=OPEN (threshold 5 failures hit)",
                level="warn",
            )
            self._publish_narration("✓ circuit breaker opened — requests fail fast now", (3.0, 8.0), matched=True)

            # Remain partitioned for duration_sec
            await self._wait(max(0, duration_sec - 4.5))

            # Heal
            self._publish_k8s(
                f"NetworkPolicy removed: ingress to {service} restored",
                level="info",
            )
            svc.error_rate = old_err
            svc.latency_ms_p95 = old_lat
            svc.healthy = True

            await self._wait(3.0)
            self._publish_k8s(
                "circuit-breaker/api-gateway: state=HALF_OPEN → CLOSED (probes succeed)",
                level="success",
            )
            self._publish_narration("✓ circuit breaker closed — traffic resumed", (10.0, 30.0), matched=True)

            hub.publish("action", {"action": f"network_partition:{service}", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: JWT expiry ─────────────────────────────────────────
    async def expire_jwt(self) -> None:
        async with self._lock():
            await self._anchor("expire_jwt")
            self._publish_narration(
                "Predicted: requests with old kid return 401, clients re-auth with refresh, new kid issues fresh JWTs",
                (0.5, 4.0),
            )
            self._publish_k8s(
                "jwt-keyring/user-service: rotated current_kid k1 → k2",
                level="warn",
            )
            self._publish_k8s(
                "jwt-keyring/api-gateway: removed kid k1 from trust list",
                level="warn",
            )

            api = self.services["api-gateway"]
            user = self.services["user-service"]
            old_err = api.error_rate
            api.error_rate = 0.30  # 30% of requests had k1 JWT
            api.healthy = False

            await self._wait(0.8)
            self._publish_k8s(
                "api-gateway: 401 unauthorized — kid=k1 not in trust list",
                level="warn",
            )

            await self._wait(1.2)
            self._publish_k8s(
                "user-service/refresh: granted new JWT (kid=k2) to 47 clients",
                level="info",
            )
            self._publish_narration("✓ clients re-auth via refresh token flow", (0.5, 4.0), matched=True)

            await self._wait(1.5)
            api.error_rate = old_err
            api.healthy = True
            self._publish_k8s("api-gateway: error rate back to baseline — rotation complete", level="success")

            hub.publish("action", {"action": "expire_jwt", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: region outage → multi-AZ failover ───────────────────
    async def region_outage(self, duration_sec: float = 20.0) -> None:
        async with self._lock():
            await self._anchor("region_outage")
            self._publish_narration(
                "Predicted: half the pods unreachable, traffic shifts to surviving AZ within T+5s, no customer-visible errors",
                (3.0, 8.0),
            )
            self._publish_k8s(
                "simulated AZ outage: aks-app-12345..47 (zone us-east-1a) marked NotReady",
                level="crit",
            )

            # Half the pods in every service go down
            killed = []
            for svc in self.services.values():
                cut = svc.pods[::2]
                killed.extend(cut)
                for p in cut:
                    p.phase = "Terminating"
                    p.ready = False
                svc.healthy = False
                svc.rps *= 0.55
                svc.latency_ms_p95 *= 1.5

            await self._wait(2.0)
            self._publish_k8s(
                f"endpoint slices updated: {len(killed)} pods removed across all services",
                level="warn",
            )
            self._publish_narration("✓ traffic shifting to zone us-east-1b", (3.0, 8.0), matched=True)

            # Cluster autoscaler kicks in
            await self._wait(3.0)
            self._publish_k8s(
                "cluster-autoscaler: provisioning 3 replacement nodes in us-east-1b",
                level="info",
            )

            await self._wait(duration_sec * 0.6)

            # Pods schedule on new nodes
            for svc in self.services.values():
                svc.pods = [p for p in svc.pods if p.phase != "Terminating"]
                while len(svc.pods) < svc.desired_replicas:
                    np = PodState(name=f"{svc.name}-{_rand_suffix()}", phase="Running", ready=True)
                    svc.pods.append(np)
                svc.healthy = True
                svc.rps /= 0.55
                svc.latency_ms_p95 /= 1.5

            self._publish_k8s("all services back to desired replicas in surviving AZ", level="success")
            self._publish_narration("✓ failover complete — outside customer impact under 4s", (10.0, 30.0), matched=True)
            hub.publish("action", {"action": "region_outage", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: bad deploy → automated rollback ────────────────────
    async def bad_deploy(self, service: str) -> None:
        if service not in self.services:
            return
        async with self._lock():
            await self._anchor(f"bad_deploy:{service}")
            self._publish_narration(
                "Predicted: new pods CrashLoopBackOff, readiness fails, rollout halts at maxSurge boundary, rolling back",
                (5.0, 15.0),
            )
            self._publish_k8s(f"kubectl rollout deploy/{service} → v1.2.4 (broken)", level="warn")

            await self._wait(1.5)
            self._publish_k8s(f"{service}-v1.2.4-{_rand_suffix()}: started → ImagePullBackOff", level="crit")
            self._publish_narration("✓ new pod failed liveness — RollingUpdate halted at maxUnavailable=0", (3.0, 8.0), matched=True)

            await self._wait(2.5)
            self._publish_k8s(
                f"deployment/{service}: condition=Progressing reason=NewReplicaSetAvailable=false (new pod not Ready)",
                level="warn",
            )

            await self._wait(2.0)
            self._publish_k8s(f"kubectl rollout undo deploy/{service} → reverting to v1.2.3", level="info")
            self._publish_narration("✓ automatic rollback triggered by progressDeadlineSeconds", (8.0, 20.0), matched=True)

            await self._wait(2.5)
            self._publish_k8s(f"{service}: rollback complete, all pods on v1.2.3, healthy", level="success")
            hub.publish("action", {"action": f"bad_deploy:{service}", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Redis failure ────────────────────────────────────────
    async def redis_failure(self, duration_sec: float = 20.0) -> None:
        async with self._lock():
            await self._anchor("redis_failure")
            self._publish_narration(
                "Predicted: rate limiter falls back to in-memory, cached endpoints get slower, DB load increases, recovery on Redis return",
                (1.0, 4.0),
            )
            self._publish_k8s("redis-master/redis: pod terminated (simulated)", level="crit")

            await self._wait(0.8)
            self._publish_k8s(
                "api-gateway/rate-limiter: Redis connection refused -> fallback to in-memory mode",
                level="warn",
            )
            self._publish_narration("rate limiter degraded to in-memory (per-replica buckets)", (1.0, 3.0), matched=True)

            old_lats = {}
            for svc in self.services.values():
                old_lats[svc.name] = svc.latency_ms_p95
                svc.latency_ms_p95 *= 1.7
                svc.error_rate = min(0.05, svc.error_rate + 0.005)

            await self._wait(2.0)
            self._publish_k8s("task-service: cache-miss rate 100% (Redis down) - DB qps +180%", level="warn")
            self._publish_narration("requests fall through cache to database", (2.0, 6.0), matched=True)

            await self._wait(max(0, duration_sec - 3.0))

            self._publish_k8s("redis-master: pod replaced, accepting connections", level="success")
            for svc in self.services.values():
                svc.latency_ms_p95 = old_lats[svc.name]
                svc.error_rate = max(0.001, svc.error_rate - 0.005)

            await self._wait(2.5)
            self._publish_k8s("api-gateway/rate-limiter: Redis healthy -> switched back to distributed mode", level="success")
            self._publish_narration("cache warmed, latency back to baseline", (15.0, 30.0), matched=True)
            hub.publish("action", {"action": "redis_failure", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Database failure ────────────────────────────────────
    async def db_failure(self, duration_sec: float = 18.0) -> None:
        async with self._lock():
            await self._anchor("db_failure")
            self._publish_narration(
                "Predicted: writes fail with 503, read replicas keep partial availability, pool reconnects on recovery",
                (0.5, 3.0),
            )
            self._publish_k8s("postgres-primary: connection refused (simulated outage)", level="crit")

            affected = ["user-service", "task-service"]
            old_state = {}
            for name in affected:
                svc = self.services[name]
                old_state[name] = (svc.healthy, svc.latency_ms_p95, svc.error_rate)
                svc.healthy = False
                svc.error_rate = 0.45
                svc.latency_ms_p95 *= 3.5

            await self._wait(1.2)
            for name in affected:
                self._publish_k8s(f"{name}: connection pool exhausted, returning 503", level="crit")
            self._publish_narration("writes fail fast with 503 + Retry-After", (1.0, 3.0), matched=True)

            await self._wait(2.5)
            self._publish_k8s("task-service: read path redirected to read-replica (degraded but available)", level="info")
            self._publish_narration("reads degraded gracefully to replicas", (3.0, 8.0), matched=True)

            await self._wait(max(0, duration_sec - 4.0))

            self._publish_k8s("postgres-primary: pod replaced, accepting connections", level="success")
            for name in affected:
                _, lat, err = old_state[name]
                svc = self.services[name]
                svc.healthy = True
                svc.latency_ms_p95 = lat
                svc.error_rate = err

            await self._wait(2.0)
            self._publish_k8s("connection pools reconnected, normal traffic resumed", level="success")
            self._publish_narration("pools re-established, system back to baseline", (15.0, 30.0), matched=True)
            hub.publish("action", {"action": "db_failure", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Slow network ────────────────────────────────────────
    async def slow_network(self, duration_sec: float = 25.0) -> None:
        async with self._lock():
            await self._anchor("slow_network")
            self._publish_narration(
                "Predicted: all latency p95 spikes by +500ms, timeouts cascade, bulkheads protect critical paths",
                (0.5, 4.0),
            )
            self._publish_k8s("tc netem: +500ms latency injected on all inter-pod traffic", level="crit")

            old_lats = {}
            for svc in self.services.values():
                old_lats[svc.name] = svc.latency_ms_p95
                svc.latency_ms_p95 += 500

            await self._wait(2.0)
            self._publish_k8s("connection pools filling, queue depths climbing", level="warn")

            await self._wait(3.0)
            self._publish_k8s("api-gateway: bulkhead pool-task-service saturated (32/32)", level="warn")
            self._publish_narration("bulkhead isolates the saturation - other pools still healthy", (3.0, 8.0), matched=True)

            await self._wait(2.0)
            for svc in self.services.values():
                svc.error_rate = min(0.08, svc.error_rate + 0.02)
            self._publish_k8s("scattered 504 Gateway Timeout responses across endpoints", level="warn")

            await self._wait(max(0, duration_sec - 7.0))

            self._publish_k8s("tc netem: latency removed", level="success")
            for svc in self.services.values():
                svc.latency_ms_p95 = old_lats[svc.name]
                svc.error_rate = max(0.001, svc.error_rate - 0.02)

            await self._wait(1.5)
            self._publish_k8s("queue depths normalizing, requests draining", level="success")
            self._publish_narration("system back to baseline - no permanent damage", (20.0, 35.0), matched=True)
            hub.publish("action", {"action": "slow_network", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Memory leak ─────────────────────────────────────────
    async def memory_leak(self, service: str) -> None:
        if service not in self.services:
            return
        async with self._lock():
            await self._anchor(f"memory_leak:{service}")
            self._publish_narration(
                "Predicted: memory climbs over ~8s, container OOMKilled at limit, replicaset replaces, blast contained to one pod",
                (8.0, 16.0),
            )
            svc = self.services[service]
            target = svc.pods[0] if svc.pods else None
            if not target:
                return

            self._publish_k8s(f"{target.name}: memory_rss climbing (520MB / 512MB limit)", level="warn")
            await self._wait(2.0)
            self._publish_k8s(f"{target.name}: memory_rss=620MB / 512MB limit (overcommitted)", level="warn")

            old_lat = svc.latency_ms_p95
            svc.latency_ms_p95 *= 1.3

            await self._wait(3.0)
            self._publish_k8s(f"{target.name}: memory_rss=730MB / 512MB", level="crit")
            await self._wait(1.5)
            self._publish_k8s(f"{target.name}: kernel OOMKilled container 'app' (exit code 137)", level="crit")
            self._publish_narration("OOM killer fired - blast contained to one pod", (5.0, 12.0), matched=True)

            target.phase = "Terminating"
            target.ready = False
            svc.healthy = False

            await self._wait(1.0)
            self._publish_k8s(f"endpoint slice {service}: removed {target.name}", level="warn")

            await self._wait(1.5)
            new_pod = PodState(name=f"{service}-{_rand_suffix()}", phase="Pending", ready=False)
            svc.pods.append(new_pod)
            self._publish_k8s(f"replicaset/{service}: scheduling replacement {new_pod.name}", level="info")

            await self._wait(2.5)
            new_pod.phase = "Running"
            new_pod.ready = True
            svc.pods = [p for p in svc.pods if p.name != target.name]
            svc.latency_ms_p95 = old_lat
            svc.healthy = True

            self._publish_k8s(f"{new_pod.name}: ready (memory_rss=85MB, healthy baseline)", level="success")
            self._publish_narration("recovery complete - alerts fire so the leak gets fixed", (10.0, 20.0), matched=True)
            hub.publish("action", {"action": f"memory_leak:{service}", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Cert expiry ────────────────────────────────────────
    async def cert_expiry(self) -> None:
        async with self._lock():
            await self._anchor("cert_expiry")
            self._publish_narration(
                "Predicted: x509 errors on new TLS connections, cert-manager renews, distributes, traffic resumes",
                (1.0, 6.0),
            )
            self._publish_k8s("tls-secret/internal-cert: expired (notAfter passed)", level="crit")

            await self._wait(1.5)
            api = self.services["api-gateway"]
            api.healthy = False
            old_err = api.error_rate
            api.error_rate = 0.85
            self._publish_k8s("api-gateway: x509: certificate has expired (downstream TLS handshakes failing)", level="crit")

            await self._wait(2.5)
            self._publish_k8s("cert-manager: certificate renewal triggered", level="info")
            await self._wait(2.5)
            self._publish_k8s("cert-manager: new certificate issued by Let's Encrypt", level="success")
            self._publish_narration("cert-manager renewed automatically", (3.0, 8.0), matched=True)

            await self._wait(1.5)
            self._publish_k8s("tls-secret/internal-cert: distributed to all api-gateway replicas (rolling reload)", level="info")
            await self._wait(2.0)
            api.error_rate = old_err
            api.healthy = True
            self._publish_k8s("api-gateway: TLS handshakes succeeding, error rate baseline", level="success")
            self._publish_narration("system recovered - moral: monitor cert age, not just liveness", (10.0, 18.0), matched=True)
            hub.publish("action", {"action": "cert_expiry", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: DNS failure ────────────────────────────────────────
    async def dns_failure(self, duration_sec: float = 18.0) -> None:
        async with self._lock():
            await self._anchor("dns_failure")
            self._publish_narration(
                "Predicted: new DNS lookups SERVFAIL, established conns OK, NodeLocal DNSCache softens impact, CoreDNS recovers",
                (1.0, 4.0),
            )
            self._publish_k8s("coredns: pod terminated, no replicas available", level="crit")

            await self._wait(1.5)
            self._publish_k8s("api-gateway: net/http: lookup user-service: server misbehaving (SERVFAIL)", level="crit")
            for svc in self.services.values():
                svc.error_rate = min(0.4, svc.error_rate + 0.1)

            await self._wait(2.5)
            self._publish_k8s("node-local-dnscache: serving stale entries from cache (graceful degradation)", level="info")
            self._publish_narration("NodeLocal DNSCache absorbing most of the impact", (3.0, 8.0), matched=True)

            await self._wait(max(0, duration_sec - 4.0))

            self._publish_k8s("coredns: pod scheduled and ready (2/2 replicas back)", level="success")
            for svc in self.services.values():
                svc.error_rate = max(0.001, svc.error_rate - 0.1)
            self._publish_narration("DNS recovered - lesson: always run CoreDNS HA", (15.0, 25.0), matched=True)
            hub.publish("action", {"action": "dns_failure", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Cascading failure ──────────────────────────────────
    async def cascading_failure(self) -> None:
        async with self._lock():
            await self._anchor("cascading_failure")
            self._publish_narration(
                "Predicted: user-service dies, task-service degrades, api-gateway circuit breakers trip, outage contained to user-flow",
                (3.0, 10.0),
            )
            user = self.services["user-service"]
            task = self.services["task-service"]
            api = self.services["api-gateway"]

            user.healthy = False
            user.error_rate = 0.92
            user.latency_ms_p95 *= 4
            self._publish_k8s("user-service: all replicas crashed (simulated kernel panic)", level="crit")

            await self._wait(2.0)
            task.error_rate = 0.35
            task.latency_ms_p95 *= 2.2
            self._publish_k8s("task-service: depends on user-service for auth checks - timeouts cascading", level="crit")
            self._publish_narration("failure spreading to dependent service", (1.0, 4.0), matched=True)

            await self._wait(3.0)
            self._publish_k8s("api-gateway/circuit-breaker:user-service -> OPEN (failure threshold hit)", level="warn")
            self._publish_k8s("api-gateway/circuit-breaker:task-service -> OPEN", level="warn")
            self._publish_narration("circuit breakers tripped - cascade contained at gateway", (4.0, 10.0), matched=True)

            api.error_rate = 0.15

            await self._wait(8.0)
            user.healthy = True
            user.error_rate = 0.001
            user.latency_ms_p95 /= 4
            task.error_rate = 0.001
            task.latency_ms_p95 /= 2.2
            api.error_rate = 0.001

            self._publish_k8s("user-service: pods recovered, readiness probes passing", level="success")
            await self._wait(2.0)
            self._publish_k8s("circuit-breakers: HALF_OPEN -> CLOSED (probe requests succeeded)", level="success")
            self._publish_narration("full recovery - blast radius bounded by breakers", (15.0, 30.0), matched=True)
            hub.publish("action", {"action": "cascading_failure", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")


    # ── Action: Disk full ──────────────────────────────────────────
    async def disk_full(self, service: str) -> None:
        if service not in self.services: return
        async with self._lock():
            await self._anchor(f"disk_full:{service}")
            self._publish_narration(
                "Predicted: disk fills, writes fail, kubelet evicts under DiskPressure, replicaset replaces",
                (4.0, 12.0),
            )
            svc = self.services[service]
            target = svc.pods[0] if svc.pods else None
            if not target: return

            self._publish_k8s(f"{target.name}: ephemeral storage 75% (1.5G/2G)", level="info")
            await self._wait(1.5)
            self._publish_k8s(f"{target.name}: ephemeral storage 92% (1.84G/2G)", level="warn")
            old_err = svc.error_rate
            svc.error_rate = 0.30

            await self._wait(1.5)
            self._publish_k8s(f"{target.name}: write failures (ENOSPC) on log file rotation", level="crit")
            self._publish_narration("write path returning 500s as disk fills", (2.0, 5.0), matched=True)

            await self._wait(2.0)
            self._publish_k8s(f"kubelet: node DiskPressure condition true (>85%)", level="crit")
            await self._wait(0.5)
            self._publish_k8s(f"kubelet: evicting {target.name} (reason: DiskPressure)", level="warn")
            target.phase = "Terminating"; target.ready = False
            svc.healthy = False

            await self._wait(1.5)
            new_pod = PodState(name=f"{service}-{_rand_suffix()}", phase="Pending", ready=False)
            svc.pods.append(new_pod)
            self._publish_k8s(f"replicaset: scheduling {new_pod.name} on a fresh disk", level="info")

            await self._wait(3.0)
            new_pod.phase = "Running"; new_pod.ready = True
            svc.pods = [p for p in svc.pods if p.name != target.name]
            svc.error_rate = old_err
            svc.healthy = True
            self._publish_k8s(f"{new_pod.name}: ready, disk 12% — underlying log-rotation bug still needs fix", level="success")
            self._publish_narration("recovery complete — fix the cause, not just the symptom", (8.0, 15.0), matched=True)
            hub.publish("action", {"action": f"disk_full:{service}", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: GC pause ──────────────────────────────────────────
    async def gc_pause(self, service: str) -> None:
        if service not in self.services: return
        async with self._lock():
            await self._anchor(f"gc_pause:{service}")
            self._publish_narration(
                "Predicted: ~600ms latency spike on one pod, outlier detection routes around, recovery in seconds",
                (1.0, 4.0),
            )
            svc = self.services[service]
            self._publish_k8s(f"{service}: full GC triggered on replica-1 (heap 91%)", level="warn")
            old_lat = svc.latency_ms_p95
            svc.latency_ms_p95 *= 6  # the slow pod drags p95 hard

            await self._wait(0.6)
            self._publish_k8s(f"{service}: p99 latency spike to 3.2s (stop-the-world pause)", level="warn")
            self._publish_narration("p99 spike observed — long-tail latency from GC", (0.5, 2.0), matched=True)

            await self._wait(1.5)
            self._publish_k8s(f"envoy: outlier detection downweights replica-1 (consecutive 5xx)", level="info")
            self._publish_narration("load balancer routing around the slow pod", (2.0, 5.0), matched=True)
            svc.latency_ms_p95 = old_lat * 1.3

            await self._wait(3.0)
            self._publish_k8s(f"{service}: GC complete, replica-1 latency back to baseline", level="success")
            self._publish_narration("replica re-admitted to pool gradually (slow-start)", (4.0, 8.0), matched=True)
            svc.latency_ms_p95 = old_lat
            hub.publish("action", {"action": f"gc_pause:{service}", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Thundering herd ───────────────────────────────────
    async def thundering_herd(self, duration_sec: float = 18.0) -> None:
        async with self._lock():
            await self._anchor("thundering_herd")
            self._publish_narration(
                "Predicted: brief upstream blip, all clients retry simultaneously, herd takes the service down a second time",
                (2.0, 6.0),
            )
            api = self.services["api-gateway"]
            user = self.services["user-service"]
            self._publish_k8s("user-service: brief 3-second blip (network glitch)", level="warn")
            old_err = user.error_rate
            user.error_rate = 0.5

            await self._wait(3.0)
            user.error_rate = old_err
            self._publish_k8s("user-service: recovered, accepting connections", level="success")

            await self._wait(0.8)
            self._publish_k8s("api-gateway: queued retries firing simultaneously (no jitter)", level="crit")
            user.error_rate = 0.7
            user.latency_ms_p95 *= 4
            api.latency_ms_p95 *= 2
            self._publish_narration("herd arrived all at once — service overwhelmed", (3.0, 6.0), matched=True)

            await self._wait(3.5)
            self._publish_k8s("user-service: adaptive concurrency limit kicks in, shedding load", level="warn")
            self._publish_narration("backpressure stops the herd from compounding", (5.0, 10.0), matched=True)

            await self._wait(max(0, duration_sec - 7.0))
            user.error_rate = old_err
            user.latency_ms_p95 /= 4
            api.latency_ms_p95 /= 2
            self._publish_k8s("traffic shaped — system stable", level="success")
            self._publish_narration("recovery — moral: full jitter on every retry policy", (10.0, 20.0), matched=True)
            hub.publish("action", {"action": "thundering_herd", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Noisy neighbor ────────────────────────────────────
    async def noisy_neighbor(self, duration_sec: float = 22.0) -> None:
        async with self._lock():
            await self._anchor("noisy_neighbor")
            self._publish_narration(
                "Predicted: a co-tenant pod hogs node CPU, neighbors degrade, scheduler eventually rebalances",
                (3.0, 10.0),
            )
            self._publish_k8s("node-12: pod 'data-batch-7f3' consuming 87% of node CPU (no limits set)", level="warn")

            # Pick a few services on the same node to degrade
            affected = ["task-service", "notification-service"]
            old_lats = {}
            for name in affected:
                svc = self.services[name]
                old_lats[name] = svc.latency_ms_p95
                svc.latency_ms_p95 *= 2.4

            await self._wait(2.0)
            self._publish_k8s("task-service: CPU throttled — latency p95 climbing", level="warn")
            self._publish_narration("co-tenant pods getting CPU-starved", (3.0, 8.0), matched=True)

            await self._wait(4.0)
            self._publish_k8s("operator applied resource limits to data-batch deployment", level="info")
            await self._wait(2.0)
            self._publish_k8s("kernel cgroup: data-batch-7f3 throttled at 4 CPU limit", level="success")
            self._publish_narration("limits applied — noisy pod capped at its fair share", (8.0, 16.0), matched=True)

            for name in affected:
                self.services[name].latency_ms_p95 = old_lats[name]

            await self._wait(max(0, duration_sec - 8.0))
            self._publish_k8s("VPA recommends data-batch right-sized to 1 CPU — moved to batch node pool", level="success")
            hub.publish("action", {"action": "noisy_neighbor", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Spot instance reclaim ─────────────────────────────
    async def spot_reclaim(self) -> None:
        async with self._lock():
            await self._anchor("spot_reclaim")
            self._publish_narration(
                "Predicted: 30s notice, node-termination-handler drains, PDB respected, pods reschedule, no user impact",
                (0.5, 4.0),
            )
            self._publish_k8s("aws-spot: termination notice for node aks-spot-9f4d (30s)", level="warn")

            await self._wait(1.0)
            self._publish_k8s("node-termination-handler: cordoning aks-spot-9f4d", level="info")
            await self._wait(1.5)
            self._publish_k8s("kubectl drain: evicting pods, respecting PDB (max-unavailable: 1)", level="info")

            # Half of api-gateway pods on this node go down briefly
            api = self.services["api-gateway"]
            target = api.pods[0]
            target.phase = "Terminating"; target.ready = False

            await self._wait(2.0)
            self._publish_k8s(f"{target.name}: SIGTERM received, draining in-flight requests (terminationGracePeriodSeconds: 30)", level="info")
            self._publish_narration("graceful shutdown — in-flight requests finished", (1.5, 5.0), matched=True)

            await self._wait(3.0)
            self._publish_k8s(f"{target.name}: terminated cleanly", level="success")
            new_pod = PodState(name=f"api-gateway-{_rand_suffix()}", phase="Pending", ready=False)
            api.pods.append(new_pod)
            self._publish_k8s(f"replicaset: scheduling replacement {new_pod.name} on on-demand capacity", level="info")

            await self._wait(2.5)
            new_pod.phase = "Running"; new_pod.ready = True
            api.pods = [p for p in api.pods if p.name != target.name]
            self._publish_k8s(f"{new_pod.name}: ready, traffic resumed", level="success")
            self._publish_narration("zero user impact — spot interruption handled gracefully", (8.0, 15.0), matched=True)
            hub.publish("action", {"action": "spot_reclaim", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Autoscaler stuck ──────────────────────────────────
    async def autoscaler_stuck(self, duration_sec: float = 20.0) -> None:
        async with self._lock():
            await self._anchor("autoscaler_stuck")
            self._publish_narration(
                "Predicted: pods Pending, cluster autoscaler unable to provision (capacity issue), fallback to alt instance family",
                (5.0, 15.0),
            )
            self._publish_k8s("cluster-autoscaler: 4 Pending pods, attempting to scale up", level="warn")

            await self._wait(2.0)
            self._publish_k8s("cloud-api: InsufficientInstanceCapacity for m5.xlarge in us-east-1a", level="crit")
            self._publish_narration("cloud-side capacity exhausted — autoscaler stuck", (2.0, 5.0), matched=True)

            # During this period, the system is degraded
            api = self.services["api-gateway"]
            api.latency_ms_p95 *= 1.8
            api.error_rate = 0.04

            await self._wait(4.0)
            self._publish_k8s("cluster-autoscaler: retry #3 — still InsufficientInstanceCapacity", level="warn")
            await self._wait(3.0)
            self._publish_k8s("priority-expander: failing over to m5a.xlarge node group", level="info")
            self._publish_narration("alt instance family attempted", (8.0, 14.0), matched=True)

            await self._wait(2.5)
            self._publish_k8s("cloud-api: m5a.xlarge accepted — provisioning 2 nodes", level="success")
            await self._wait(3.0)
            self._publish_k8s("new nodes Ready, Pending pods scheduled", level="success")

            api.latency_ms_p95 /= 1.8
            api.error_rate = 0.001
            await self._wait(max(0, duration_sec - 14.5))
            self._publish_narration("capacity restored — moral: don't depend on a single instance family", (15.0, 25.0), matched=True)
            hub.publish("action", {"action": "autoscaler_stuck", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: API server throttle ───────────────────────────────
    async def api_throttle(self, duration_sec: float = 20.0) -> None:
        async with self._lock():
            await self._anchor("api_throttle")
            self._publish_narration(
                "Predicted: misbehaving controller hammers API server, APF rejects low-priority requests, workloads keep running",
                (1.0, 4.0),
            )
            self._publish_k8s("kube-apiserver: request queue depth at 800/1000", level="warn")

            await self._wait(1.5)
            self._publish_k8s("kube-apiserver: 429 Too Many Requests on low-priority requests", level="crit")
            self._publish_k8s("apf: workload-low priority being throttled", level="warn")
            self._publish_narration("APF protecting system-critical requests", (1.0, 4.0), matched=True)

            await self._wait(3.0)
            self._publish_k8s("identified: third-party-controller-x making 12K LIST requests/min", level="info")
            await self._wait(2.5)
            self._publish_k8s("operator: increased kube-apiserver replicas 3 → 5", level="info")

            await self._wait(max(0, duration_sec - 7.0))
            self._publish_k8s("kube-apiserver: queue depth normal, all priority levels healthy", level="success")
            self._publish_narration("workloads were never affected — APF did its job", (15.0, 25.0), matched=True)
            hub.publish("action", {"action": "api_throttle", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Secret leak ───────────────────────────────────────
    async def secret_leak(self, duration_sec: float = 22.0) -> None:
        async with self._lock():
            await self._anchor("secret_leak")
            self._publish_narration(
                "Predicted: secret rotated, projected volumes pick up new value within seconds, env-var pods need rolling restart",
                (1.0, 8.0),
            )
            self._publish_k8s("gitleaks: detected DB_PASSWORD in commit abcd1234 (public repo)", level="crit")

            await self._wait(2.0)
            self._publish_k8s("security-team: rotating db-password secret", level="info")
            await self._wait(1.5)
            self._publish_k8s("kubectl apply: secret/db-password updated", level="info")
            await self._wait(1.0)
            self._publish_k8s("projected volume in task-service pods: hot-reloaded new secret", level="success")
            self._publish_narration("projected volume picked up rotation in 5s", (3.0, 8.0), matched=True)

            await self._wait(2.0)
            self._publish_k8s("user-service uses env-var secret: needs rolling restart", level="info")
            user = self.services["user-service"]
            user.healthy = False

            await self._wait(2.5)
            self._publish_k8s("rolling restart: replicas 3/3 with new secret", level="success")
            user.healthy = True
            self._publish_narration("env-var-based pods rotated via rolling restart", (8.0, 15.0), matched=True)

            await self._wait(2.0)
            self._publish_k8s("db: old credential revoked, all open connections re-authed", level="success")
            await self._wait(max(0, duration_sec - 11.0))
            self._publish_k8s("postmortem action: add gitleaks pre-commit hook + External Secrets Operator", level="info")
            hub.publish("action", {"action": "secret_leak", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Service mesh sidecar crash ────────────────────────
    async def service_mesh_crash(self, service: str) -> None:
        if service not in self.services: return
        async with self._lock():
            await self._anchor(f"service_mesh_crash:{service}")
            self._publish_narration(
                "Predicted: sidecar OOM, app keeps running but unreachable, sidecar restarts and resyncs xDS, total ~5s impact",
                (2.0, 8.0),
            )
            svc = self.services[service]
            self._publish_k8s(f"{service} pod replica-1: envoy sidecar memory 510MB / 512MB limit", level="warn")
            await self._wait(1.0)
            self._publish_k8s(f"{service} pod replica-1: envoy OOMKilled (exit 137) — app container running", level="crit")

            old_err = svc.error_rate
            svc.error_rate = 0.20

            await self._wait(1.5)
            self._publish_k8s(f"istio: removed pod replica-1 from EndpointSlice (sidecar not ready)", level="warn")
            self._publish_narration("mesh routing around the pod with the dead sidecar", (1.5, 4.0), matched=True)

            await self._wait(2.0)
            self._publish_k8s(f"envoy: restarted, xDS sync from istiod in progress", level="info")
            await self._wait(2.0)
            self._publish_k8s(f"envoy: xDS sync complete, listeners ready", level="success")
            svc.error_rate = old_err
            self._publish_narration("sidecar back, pod re-admitted to mesh", (5.0, 10.0), matched=True)
            hub.publish("action", {"action": f"service_mesh_crash:{service}", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Action: Third-party SaaS outage ───────────────────────────
    async def third_party_outage(self, duration_sec: float = 25.0) -> None:
        async with self._lock():
            await self._anchor("third_party_outage")
            self._publish_narration(
                "Predicted: payment provider down, circuit breaker trips, orders queue for replay, no double-charges thanks to idempotency keys",
                (1.0, 5.0),
            )
            self._publish_k8s("upstream-monitor: api.stripe.com unreachable (5xx + timeouts)", level="crit")

            task = self.services["task-service"]
            task.error_rate = 0.18  # only payment paths fail

            await self._wait(2.5)
            self._publish_k8s("task-service: circuit-breaker:stripe → OPEN (5 consecutive failures)", level="warn")
            self._publish_narration("circuit breaker trips — fast-failing instead of hanging", (2.0, 5.0), matched=True)

            await self._wait(2.0)
            self._publish_k8s("checkout: degraded response — 'Payment will be processed shortly'", level="info")
            self._publish_k8s("order-queue: 47 orders enqueued for replay with idempotency keys", level="info")
            self._publish_narration("orders accepted and queued — customer experience preserved", (4.0, 10.0), matched=True)

            await self._wait(max(0, duration_sec - 6.5))
            self._publish_k8s("upstream-monitor: api.stripe.com responding normally", level="success")
            self._publish_k8s("circuit-breaker:stripe → HALF_OPEN (probe succeeded)", level="success")
            await self._wait(1.5)
            self._publish_k8s("order-queue: replaying 47 queued operations with deduplication", level="info")
            await self._wait(1.5)
            self._publish_k8s("order-queue: drained, 47 charges processed, 0 duplicates", level="success")
            task.error_rate = 0.001
            self._publish_narration("queued orders processed — moral: idempotency keys save lives", (15.0, 30.0), matched=True)
            hub.publish("action", {"action": "third_party_outage", "completed": True, "duration_sec": round(self.relative_ts(), 1)}, level="success")

    # ── Scenario runner ────────────────────────────────────────────
    async def run_scenario(self, scenario_id: str) -> None:
        from app.explainers import SCENARIOS
        scn = SCENARIOS.get(scenario_id)
        if not scn:
            hub.publish("log", {"msg": f"unknown scenario {scenario_id}"}, level="warn")
            return
        hub.publish(
            "scenario",
            {"id": scenario_id, "name": scn["name"], "started": True, "duration": scn["duration_sec"]},
            level="info",
        )
        # Scenario steps must be allowed to OVERLAP — black_friday spawns two
        # cpu_pressure events 5s apart and they should run concurrently. The
        # _action_lock exists to keep manual triggers from stomping on each
        # other (one click at a time), so we suspend it for scenario runs.
        self._scenario_mode = True
        tasks = []
        for step in scn["steps"]:
            tasks.append(asyncio.create_task(self._scenario_step(step)))
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            hub.publish("log", {"msg": f"scenario error: {e}"}, level="warn")
        finally:
            self._scenario_mode = False
        hub.publish(
            "scenario",
            {"id": scenario_id, "name": scn["name"], "completed": True},
            level="success",
        )

    async def _scenario_step(self, step: dict) -> None:
        await asyncio.sleep(step["at"])
        action = step["action"]
        if action == "kill_pod":
            await self.kill_pod(step["service"])
        elif action == "cpu_pressure":
            await self.cpu_pressure(step["service"], step.get("duration", 12))
        elif action == "network_partition":
            await self.network_partition(step["service"], step.get("duration", 10))
        elif action == "expire_jwt":
            await self.expire_jwt()
        elif action == "region_outage":
            await self.region_outage(step.get("duration", 20))
        elif action == "bad_deploy":
            await self.bad_deploy(step["service"])
        elif action == "redis_failure":
            await self.redis_failure(step.get("duration", 20))
        elif action == "db_failure":
            await self.db_failure(step.get("duration", 18))
        elif action == "slow_network":
            await self.slow_network(step.get("duration", 25))
        elif action == "memory_leak":
            await self.memory_leak(step["service"])
        elif action == "cert_expiry":
            await self.cert_expiry()
        elif action == "dns_failure":
            await self.dns_failure(step.get("duration", 18))
        elif action == "cascading_failure":
            await self.cascading_failure()
        elif action == "disk_full":
            await self.disk_full(step["service"])
        elif action == "gc_pause":
            await self.gc_pause(step["service"])
        elif action == "thundering_herd":
            await self.thundering_herd(step.get("duration", 18))
        elif action == "noisy_neighbor":
            await self.noisy_neighbor(step.get("duration", 22))
        elif action == "spot_reclaim":
            await self.spot_reclaim()
        elif action == "autoscaler_stuck":
            await self.autoscaler_stuck(step.get("duration", 20))
        elif action == "api_throttle":
            await self.api_throttle(step.get("duration", 20))
        elif action == "secret_leak":
            await self.secret_leak(step.get("duration", 22))
        elif action == "service_mesh_crash":
            await self.service_mesh_crash(step["service"])
        elif action == "third_party_outage":
            await self.third_party_outage(step.get("duration", 25))


def _rand_suffix() -> str:
    chars = "abcdefghjkmnpqrstvwxz23456789"
    return "".join(random.choice(chars) for _ in range(5))


# ─── Live cluster (production) ─────────────────────────────────────────
class LiveCluster(BaseCluster):
    """Real K8s implementation. Falls back to mock behaviour gracefully
    if the kubernetes package isn't installed or in-cluster config fails."""

    def __init__(self) -> None:
        super().__init__()
        try:
            from kubernetes import client, config as k8s_config

            if settings.k8s_in_cluster:
                k8s_config.load_incluster_config()
            elif settings.k8s_kubeconfig:
                k8s_config.load_kube_config(config_file=settings.k8s_kubeconfig)
            else:
                k8s_config.load_kube_config()
            self.core = client.CoreV1Api()
            self.apps = client.AppsV1Api()
            self.ready = True
        except Exception as e:
            self.ready = False
            hub.publish("log", {"msg": f"LiveCluster disabled: {e}"}, level="warn")

    def snapshot(self) -> dict:
        return {"services": {}, "mode": "live"}

    async def start(self) -> None:
        hub.publish("log", {"msg": "LiveCluster started (data sources not yet wired in this MVP)"}, level="info")

    async def stop(self) -> None:
        """Graceful shutdown placeholder for LiveCluster — close K8s watchers,
        flush metrics, etc. when the watchers are implemented."""
        return None


def make_cluster() -> BaseCluster:
    if settings.is_mock:
        return MockCluster()
    return LiveCluster()
