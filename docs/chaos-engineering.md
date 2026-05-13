# Chaos Engineering Runbook

This document shows how to validate every resilience mechanism in this platform
by deliberately breaking things and observing the system recover. Run these tests
against your **dev** cluster — never production.

The goal is not to find new bugs. The goal is to **prove the ones you already
built actually work** under real failure conditions.

---

## Prerequisites

```bash
# Point kubectl at your dev cluster
az aks get-credentials --resource-group rg-microservices-dev --name aks-microservices-dev

# Verify you're in the right context
kubectl config current-context

# Open Jaeger in another terminal (traces)
kubectl port-forward svc/jaeger-query 16686:16686 -n monitoring &

# Open Grafana in another terminal (metrics)
kubectl port-forward svc/prometheus-grafana 3000:80 -n monitoring &
```

---

## Scenario 1 — Pod Failure + Automatic Recovery

**What we're testing:** Kubernetes self-healing. When a pod crashes, the
ReplicaSet controller notices and starts a replacement.

```bash
# Watch the pods in real time in one terminal
kubectl get pods -n microservices -w

# In another terminal, kill a user-service pod (pick any pod name)
kubectl delete pod -l app=user-service -n microservices --field-selector='status.phase=Running' \
  $(kubectl get pod -l app=user-service -n microservices -o name | head -1)
```

**What you should see:**
- Within ~1s: the pod status changes to `Terminating`
- Within ~10–15s: a new pod appears in `ContainerCreating` state
- Within ~30s: the new pod is `Running` and passes readiness checks
- In Jaeger: requests during the gap show a retry or a 503 for non-GET calls

**Why it works:** `replicaCount: 2` means one pod dying still leaves one serving.
The `readinessProbe` prevents the new pod from receiving traffic until it's ready.

---

## Scenario 2 — Circuit Breaker Activation

**What we're testing:** The api-gateway's circuit breaker opens after repeated
upstream failures, preventing a cascade of timeouts.

```bash
# Scale user-service to 0 (simulates a complete service outage)
kubectl scale deployment user-service --replicas=0 -n microservices

# Hit the login endpoint repeatedly (it will fail)
for i in {1..10}; do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -X POST http://localhost:8080/api/v1/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"test@example.com","password":"Test1234"}'
  sleep 0.5
done
```

**What you should see:**
- First few requests: `502` or `503` (real upstream error)
- After ~5 failures: `503` responses arrive **immediately** (no network delay)
  — this is the circuit breaker in OPEN state, rejecting requests without trying
- Response header: `Retry-After: 30`

**In the Grafana dashboard:** Watch `gateway_circuit_breaker_state` metric
flip from 0 (closed) to 1 (open).

**In the logs:**
```
Circuit breaker [user-service] is OPEN — rejecting request immediately
```

**Recovery:**
```bash
kubectl scale deployment user-service --replicas=2 -n microservices
# Wait ~30s for the half-open probe to succeed
```

---

## Scenario 3 — Retry with Exponential Backoff (GET only)

**What we're testing:** The gateway retries transient 503s on idempotent
requests without client involvement.

```bash
# Simulate a flaky upstream by adding a network delay/drop at the service level
# Use kubectl to add a temporary disruption
kubectl exec -it deployment/user-service -n microservices -- \
  sh -c "sleep 0" # establish exec session

# Alternatively, delete one pod of a 2-replica deployment and watch retries
kubectl delete pod $(kubectl get pod -l app=user-service -n microservices -o name | head -1) -n microservices &

# Immediately hit GET /api/v1/users/me (an idempotent endpoint)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/users/me
```

**What you should see in gateway logs:**
```
Retry 2/3 for GET http://user-service:8001/api/v1/users/me (backoff: 0.1s)
```
The client gets a successful `200` response even though the upstream was
briefly unavailable.

---

## Scenario 4 — Pod Disruption Budget Enforcement

**What we're testing:** That Kubernetes won't drain more than 1 pod at a time
during a node drain, respecting the PDB's `minAvailable: 1`.

```bash
# Identify the node running user-service pods
kubectl get pods -l app=user-service -n microservices -o wide

# Try to drain the node (simulates node maintenance or upgrade)
# Replace NODE_NAME with the actual node name
kubectl drain NODE_NAME --ignore-daemonsets --delete-emptydir-data --dry-run=client

# Actually drain it
kubectl drain NODE_NAME --ignore-daemonsets --delete-emptydir-data
```

**What you should see:**
```
evicting pod microservices/user-service-xxx-yyy
error when evicting pods/"user-service-xxx-yyy" (will retry after 5s):
  Cannot evict pod as it would violate the pod's disruption budget.
```
Kubernetes waits for a replacement pod to be Running+Ready before evicting
the next one. The service remains available throughout.

**Restore the node:**
```bash
kubectl uncordon NODE_NAME
```

---

## Scenario 5 — Horizontal Pod Autoscaler Under Load

**What we're testing:** That HPA scales out pods when CPU utilisation exceeds
the target, and scales back in when load drops.

```bash
# Run a load test against the api-gateway (must be port-forwarded)
# Install: pip install locust
locust -f tests/load_test.py \
  --host http://localhost:8080 \
  --users 100 --spawn-rate 10 \
  --run-time 3m --headless

# In another terminal, watch HPA decisions
kubectl get hpa -n microservices -w
```

**What you should see:**
```
NAME          REFERENCE                     TARGETS   MINPODS   MAXPODS   REPLICAS
api-gateway   Deployment/api-gateway        72%/70%   2         10        2
api-gateway   Deployment/api-gateway        85%/70%   2         10        3
api-gateway   Deployment/api-gateway        91%/70%   2         10        4
```

HPA waits ~3min after load drops before scaling in (to avoid flapping).

---

## Scenario 6 — Database Connection Exhaustion

**What we're testing:** That SQLAlchemy's connection pool prevents runaway
connections from crashing the database.

```bash
# Watch active connections on the PostgreSQL server
kubectl exec -it deployment/user-service -n microservices -- \
  python -c "
import asyncio
from sqlalchemy import text
from app.database import engine

async def count():
    async with engine.connect() as conn:
        result = await conn.execute(text('SELECT count(*) FROM pg_stat_activity'))
        print(f'Active connections: {result.scalar()}')

asyncio.run(count())
"

# Generate concurrent requests to saturate the pool
for i in {1..200}; do
  curl -s http://localhost:8080/api/v1/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"test@test.com","password":"wrong"}' &
done
wait
```

**What you should see:**
- Active connections plateau at the pool max size (`pool_size + max_overflow`)
- Requests beyond the pool wait up to `pool_timeout` then return 503
- The database is never overwhelmed; it only sees ≤ pool_max connections

---

## Scenario 7 — Service Bus Message Retry (Notification Service)

**What we're testing:** That messages are not lost when the notification service
fails to process them — they're abandoned back to the queue for retry.

```bash
# Temporarily break the notification service's handler by killing it
kubectl scale deployment notification-service --replicas=0 -n microservices

# Create a task (which publishes a task.created event)
TOKEN=$(curl -s -X POST http://localhost:8080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"user@test.com","password":"Password1"}' | jq -r .access_token)

curl -X POST http://localhost:8080/api/v1/tasks/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Test chaos","project_id":"REPLACE_PROJECT_ID","priority":"HIGH"}'

# Restore the notification service
kubectl scale deployment notification-service --replicas=1 -n microservices

# Wait ~30s for the consumer to start, then check logs
kubectl logs -l app=notification-service -n microservices --tail=20
```

**What you should see:**
The `task.created` event is re-delivered and processed after the service comes back.
Azure Service Bus guarantees at-least-once delivery with a configurable lock timeout.

---

## Scenario 8 — SSE Connection Resilience

**What we're testing:** That SSE clients automatically reconnect after a
notification-service restart without losing the event stream.

```bash
# Open an SSE connection in one terminal
curl -N -H "Authorization: Bearer $TOKEN" \
  -H "Accept: text/event-stream" \
  http://localhost:8080/api/v1/notifications/stream

# In another terminal, restart the notification service
kubectl rollout restart deployment/notification-service -n microservices

# Create a task to generate an event
curl -X POST http://localhost:8080/api/v1/tasks/ ...
```

**What you should see:**
- SSE stream goes silent for ~10–15s during the restart
- Keepalive comments (`: keepalive`) resume once the new pod is ready
- The `task.created` event arrives (it was re-delivered from Service Bus)

---

## Observability During Chaos

While running any scenario, keep these dashboards open:

| Tool | URL | What to look for |
|------|-----|-----------------|
| Grafana | http://localhost:3000 | Error rate spike, HPA decisions, circuit breaker state |
| Jaeger | http://localhost:16686 | Retry traces, timeout spans, 503 responses with X-Request-ID |
| K9s | `k9s -n microservices` | Pod restarts, event stream, real-time resource usage |
| kubectl events | `kubectl get events -n microservices --sort-by=.lastTimestamp` | PDB violations, scheduling events, OOM kills |

### Useful Prometheus queries (paste into Grafana Explore):

```promql
# Error rate (%)
100 * sum(rate(gateway_requests_total{status_code=~"5.."}[1m]))
    / sum(rate(gateway_requests_total[1m]))

# Circuit breaker state (0=closed, 1=open, 0.5=half-open)
gateway_circuit_breaker_state

# p99 request latency per path
histogram_quantile(0.99, sum(rate(gateway_request_duration_seconds_bucket[5m])) by (le, path))

# HPA-driven pod count
kube_deployment_status_replicas_available{namespace="microservices"}

# Service Bus events processed
rate(notification_events_processed_total[1m])
```

---

## Cleanup

After each test, verify the cluster is back to a healthy state:

```bash
# All pods should be Running
kubectl get pods -n microservices

# No pending events or failed pods
kubectl get events -n microservices --field-selector type=Warning

# HPA back to minimum replicas
kubectl get hpa -n microservices

# All nodes schedulable
kubectl get nodes
```
