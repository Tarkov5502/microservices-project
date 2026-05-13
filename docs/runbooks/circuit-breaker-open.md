# Runbook: CircuitBreakerOpen

> The gateway has tripped the circuit breaker for one of its backend services
> after 5+ consecutive failures.

## What this means

The gateway is refusing all traffic to the affected backend until either:

- A probe request succeeds (after the cooling-off window), OR
- An operator restarts the gateway pod.

This protects the rest of the platform from cascading failures.

## First three things to check

1. **The backend's logs.** `kubectl logs deployment/<service> -n microservices --tail=200`
2. **The backend's readiness probe.** Is it actually serving `/health/ready`?
   `kubectl get pods -n microservices` — Ready/total column.
3. **Dependencies of that backend.** Most circuit trips trace back to the
   Postgres / Redis / Service Bus layer the backend depends on.

## Mitigation

- Fix the underlying issue.
- The breaker recovers on its own — no manual reset needed.
- If you must force a reset: `kubectl rollout restart deployment/api-gateway`.

## Wiring the alert

The circuit breaker state is not yet exported as a Prometheus metric. To
make the alert in `alert-rules.yml` fire, add a `Gauge` in
`app/circuit_breaker.py` that exports `gateway_circuit_breaker_state{service="..."}`
on state transitions.
