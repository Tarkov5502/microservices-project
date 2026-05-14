# chaos-controller

The backend for the **Chaos Theater** — the resilience demonstration dashboard.

## What it does

Exposes an HTTP API that lets a client (the chaos theater frontend) trigger real failure scenarios against the platform and watch the system recover in real time. The recovery happens via the actual resilience primitives already in this codebase: liveness probes, readiness probes, HPA, circuit breakers, NetworkPolicies, multi-AZ failover, automatic rollback.

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness probe |
| GET | `/snapshot` | Current cluster state as JSON |
| GET | `/stream` | SSE event stream (firehose) |
| POST | `/chaos/kill-pod` | `{"service": "..."}` — delete a pod |
| POST | `/chaos/cpu-pressure` | `{"service": "...", "duration": 12}` — load CPU |
| POST | `/chaos/network-partition` | `{"service": "...", "duration": 10}` — cut traffic |
| POST | `/chaos/expire-jwt` | rotate JWT keys, force re-auth |
| POST | `/chaos/region-outage` | `{"duration": 20}` — simulate AZ outage |
| POST | `/chaos/bad-deploy` | `{"service": "..."}` — push a broken image |

## Two operating modes

**MOCK mode (default)** — Simulates the cluster in-memory. No Kubernetes required. Recovery timings, K8s events, latency spikes, and metric responses are all modeled to mirror what a real cluster would produce. Use this for local dev, demos, and educational deployments.

**LIVE mode** — Connects to a real Kubernetes cluster via in-cluster ServiceAccount. Actions are translated into real `kubectl` operations. The cluster state in `/snapshot` is composed from real K8s API + Prometheus queries. Use this only when you actually want to chaos-test your live infrastructure.

Set `CHAOS_MODE=live` to switch.

## Run locally

```bash
docker compose up chaos-controller
# then:
curl http://localhost:8004/health
curl http://localhost:8004/snapshot | jq
```

Or directly:

```bash
cd services/chaos-controller
pip install -r requirements.txt
uvicorn app.main:app --port 8004 --reload
```

## Architecture

```
┌──────────────────────────┐         ┌──────────────────────┐
│  Frontend (chaos-theater)│ ──SSE── │    chaos-controller  │
│  - Status grid           │ ←─────  │                      │
│  - Recovery timeline     │         │  ┌────────────────┐  │
│  - Live charts           │         │  │  Cluster impl  │  │
│  - Narration overlay     │         │  │  Mock | Live   │  │
└────────┬─────────────────┘         │  └───────┬────────┘  │
         │                            │          │           │
         │ POST /chaos/<action>       │          │ K8s API   │
         └─────────────────────────── │          │ (live)    │
                                      │          ▼           │
                                      │   ┌─────────────┐    │
                                      │   │ Real cluster │    │
                                      │   │ or in-mem    │    │
                                      │   │ simulation   │    │
                                      │   └─────────────┘    │
                                      └──────────────────────┘
```

Events flow one-way from controller to all subscribed clients via SSE. Actions flow from client to controller via POST. The cluster abstraction means the frontend can't tell whether it's connected to a real cluster or a simulation — both produce identical event shapes.

## Code layout

```
app/
├── __init__.py
├── config.py     # pydantic-settings (env-driven)
├── events.py     # Pub/sub event hub (one queue per SSE subscriber)
├── cluster.py    # MockCluster + LiveCluster (shared interface)
└── main.py       # FastAPI app + endpoints
```

About 950 lines of Python total. The complexity lives in the mock cluster, which carefully simulates the timing of real Kubernetes recoveries based on observed production behavior.
