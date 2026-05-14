"""
chaos-controller/app/main.py

The Chaos Theater controller.

Exposes:
    GET  /health         — liveness probe
    GET  /snapshot       — current cluster state as JSON
    GET  /stream         — SSE event stream (the firehose to the browser)
    GET  /explainers     — educational metadata for the UI
    POST /chaos/<action> — trigger one of 23 chaos actions (see app/cluster.py)
    POST /chaos/scenario — run a composed scenario (10 pre-built, see app/explainers.py)

The frontend opens an EventSource against /stream and renders events as they
arrive. Action endpoints are async — they return immediately after kicking
off the chaos coroutine, which then publishes its progress to /stream.

The complexity lives in cluster.py (the simulator/live model), explainers.py
(educational metadata + composed scenarios), and events.py (the pub/sub).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.cluster import BaseCluster, make_cluster
from app.config import settings
from app.events import hub

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


cluster: BaseCluster | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cluster
    logger.info("chaos-controller starting up... mode=%s", settings.chaos_mode)
    cluster = make_cluster()
    await cluster.start()
    hub.publish(
        "log",
        {"msg": f"chaos-controller online (mode={settings.chaos_mode})", "snapshot": cluster.snapshot()},
        level="success",
    )
    try:
        yield
    finally:
        logger.info("chaos-controller shutting down — cancelling background tasks")
        if cluster is not None:
            try:
                await cluster.stop()
            except Exception as exc:
                logger.warning("error during cluster.stop(): %s", exc)


app = FastAPI(
    title="Chaos Controller",
    description="The backend for the Chaos Theater — triggers real failures and streams the recovery.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ─── Health / state endpoints ───────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "chaos-controller", "mode": settings.chaos_mode}


@app.get("/snapshot")
async def snapshot():
    if cluster is None:
        raise HTTPException(503, "cluster not ready")
    return cluster.snapshot()


# ─── SSE stream ─────────────────────────────────────────────────────────
@app.get("/stream")
async def stream():
    """Server-Sent Events stream. The browser opens this with EventSource
    and stays connected. Every chaos event (action, k8s, probe, metric,
    narration, log) is fanned out to every connected client."""
    q = hub.subscribe()

    async def event_generator():
        # Send a synthetic "connected" event first so the frontend can
        # display "connected" UI as soon as the stream opens.
        yield "data: " + '{"ts":0,"kind":"log","level":"success","payload":{"msg":"stream connected"}}' + "\n\n"
        # Send a fresh snapshot so the frontend has a baseline.
        if cluster is not None:
            import json
            yield "data: " + json.dumps({"ts": 0, "kind": "metric", "level": "info", "payload": {"snapshot": cluster.snapshot()}}) + "\n\n"
        try:
            async for line in hub.stream(q):
                yield line
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable buffering in nginx if proxied
        },
    )


# ─── Chaos action endpoints ─────────────────────────────────────────────
class ServiceBody(BaseModel):
    service: str


class ServiceDurationBody(BaseModel):
    service: str
    duration: float = 10.0


class DurationBody(BaseModel):
    duration: float = 20.0


def _ensure_cluster() -> BaseCluster:
    if cluster is None:
        raise HTTPException(503, "cluster not initialised")
    return cluster


@app.post("/chaos/kill-pod")
async def kill_pod(body: ServiceBody):
    c = _ensure_cluster()
    asyncio.create_task(c.kill_pod(body.service))
    return {"queued": "kill_pod", "service": body.service}


@app.post("/chaos/cpu-pressure")
async def cpu_pressure(body: ServiceDurationBody):
    c = _ensure_cluster()
    asyncio.create_task(c.cpu_pressure(body.service, body.duration))
    return {"queued": "cpu_pressure", "service": body.service}


@app.post("/chaos/network-partition")
async def network_partition(body: ServiceDurationBody):
    c = _ensure_cluster()
    asyncio.create_task(c.network_partition(body.service, body.duration))
    return {"queued": "network_partition", "service": body.service}


@app.post("/chaos/expire-jwt")
async def expire_jwt():
    c = _ensure_cluster()
    asyncio.create_task(c.expire_jwt())
    return {"queued": "expire_jwt"}


@app.post("/chaos/region-outage")
async def region_outage(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 20.0
    asyncio.create_task(c.region_outage(duration))
    return {"queued": "region_outage", "duration": duration}


@app.post("/chaos/bad-deploy")
async def bad_deploy(body: ServiceBody):
    c = _ensure_cluster()
    asyncio.create_task(c.bad_deploy(body.service))
    return {"queued": "bad_deploy", "service": body.service}


# ─── Extended chaos actions ─────────────────────────────────────────────
@app.post("/chaos/redis-failure")
async def redis_failure(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 20.0
    asyncio.create_task(c.redis_failure(duration))
    return {"queued": "redis_failure", "duration": duration}


@app.post("/chaos/db-failure")
async def db_failure(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 18.0
    asyncio.create_task(c.db_failure(duration))
    return {"queued": "db_failure", "duration": duration}


@app.post("/chaos/slow-network")
async def slow_network(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 25.0
    asyncio.create_task(c.slow_network(duration))
    return {"queued": "slow_network", "duration": duration}


@app.post("/chaos/memory-leak")
async def memory_leak(body: ServiceBody):
    c = _ensure_cluster()
    asyncio.create_task(c.memory_leak(body.service))
    return {"queued": "memory_leak", "service": body.service}


@app.post("/chaos/cert-expiry")
async def cert_expiry():
    c = _ensure_cluster()
    asyncio.create_task(c.cert_expiry())
    return {"queued": "cert_expiry"}


@app.post("/chaos/dns-failure")
async def dns_failure(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 18.0
    asyncio.create_task(c.dns_failure(duration))
    return {"queued": "dns_failure", "duration": duration}


@app.post("/chaos/cascading-failure")
async def cascading_failure():
    c = _ensure_cluster()
    asyncio.create_task(c.cascading_failure())
    return {"queued": "cascading_failure"}


# ─── Scenarios (composed chaos sequences) ───────────────────────────────
class ScenarioBody(BaseModel):
    id: str


@app.post("/chaos/scenario")
async def scenario(body: ScenarioBody):
    c = _ensure_cluster()
    asyncio.create_task(c.run_scenario(body.id))
    return {"queued": "scenario", "id": body.id}


# ─── Educational metadata ───────────────────────────────────────────────
@app.get("/explainers")
async def get_explainers():
    """Returns the educational data for all chaos actions and scenarios.
    The frontend caches this once on load — no need to re-fetch."""
    from app.explainers import EXPLAINERS, SCENARIOS
    return {"actions": EXPLAINERS, "scenarios": SCENARIOS}


# ─── Additional real-world chaos actions ────────────────────────────────
@app.post("/chaos/disk-full")
async def disk_full(body: ServiceBody):
    c = _ensure_cluster()
    asyncio.create_task(c.disk_full(body.service))
    return {"queued": "disk_full", "service": body.service}


@app.post("/chaos/gc-pause")
async def gc_pause(body: ServiceBody):
    c = _ensure_cluster()
    asyncio.create_task(c.gc_pause(body.service))
    return {"queued": "gc_pause", "service": body.service}


@app.post("/chaos/thundering-herd")
async def thundering_herd(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 18.0
    asyncio.create_task(c.thundering_herd(duration))
    return {"queued": "thundering_herd", "duration": duration}


@app.post("/chaos/noisy-neighbor")
async def noisy_neighbor(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 22.0
    asyncio.create_task(c.noisy_neighbor(duration))
    return {"queued": "noisy_neighbor", "duration": duration}


@app.post("/chaos/spot-reclaim")
async def spot_reclaim():
    c = _ensure_cluster()
    asyncio.create_task(c.spot_reclaim())
    return {"queued": "spot_reclaim"}


@app.post("/chaos/autoscaler-stuck")
async def autoscaler_stuck(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 20.0
    asyncio.create_task(c.autoscaler_stuck(duration))
    return {"queued": "autoscaler_stuck", "duration": duration}


@app.post("/chaos/api-throttle")
async def api_throttle(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 20.0
    asyncio.create_task(c.api_throttle(duration))
    return {"queued": "api_throttle", "duration": duration}


@app.post("/chaos/secret-leak")
async def secret_leak(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 22.0
    asyncio.create_task(c.secret_leak(duration))
    return {"queued": "secret_leak", "duration": duration}


@app.post("/chaos/service-mesh-crash")
async def service_mesh_crash(body: ServiceBody):
    c = _ensure_cluster()
    asyncio.create_task(c.service_mesh_crash(body.service))
    return {"queued": "service_mesh_crash", "service": body.service}


@app.post("/chaos/third-party-outage")
async def third_party_outage(body: DurationBody | None = None):
    c = _ensure_cluster()
    duration = body.duration if body else 25.0
    asyncio.create_task(c.third_party_outage(duration))
    return {"queued": "third_party_outage", "duration": duration}
