# Runbook: HighP95Latency

> Gateway p95 has been above 1.0 s for 10 min.

## First three things to check

1. **Service-RED dashboard, p99 line.** Is p99 separating from p95? If yes,
   you have tail-latency degradation (often a slow dependency); if not, the
   whole distribution shifted (often a query plan change or CPU saturation).

2. **Top-N slow endpoints panel.** Which routes dominate? If a single route
   spikes, run an EXPLAIN on its representative SQL — query plans drift after
   ANALYZE on a growing table.

3. **CPU saturation.** If the service container is sitting at >70% of its
   `resources.limits.cpu`, that's enough to cause queueing. HPA should be
   scaling — verify `kubectl get hpa -n microservices`.

## Mitigation

- If a single endpoint is the cause, deploy a fix.
- If load-induced, raise the HPA `maxReplicas` ceiling.
- If a dependency is slow, the gateway's circuit breaker should already be
  cutting it off — check that it's actually opening.

## Escalation

- Platform team Slack: `#platform-oncall`
