# Runbook: HighErrorRate

> The gateway is returning 5xx for more than 1% of requests over the last 10 min.

## First three things to check

1. **Recent deploys.** Open the GitHub Actions runs for `deploy-services.yml`.
   If a deploy completed within the last 30 min, that's your first suspect.
   `kubectl rollout undo deployment/<service> -n microservices` rolls back to
   the previous version.

2. **The service-RED dashboard.** Filter to the affected service. Which
   endpoint is leading the error mass? If one path dominates, look at that
   handler and any new code touching it.

3. **Upstream dependencies.**
   - Postgres: `SELECT count(*) FROM pg_stat_activity;` — pool exhaustion?
   - Redis: `redis-cli info clients` — connection saturation?
   - Service Bus: Azure Portal → namespace metrics → "Server errors".

## Mitigation

If you can't identify a cause within 10 min, scale the service:

```
kubectl scale deployment/<service> --replicas=<current * 2> -n microservices
```

Then continue investigating. Doubling capacity often masks a load-induced
cascade long enough to root-cause it.

## Escalation

- Platform team Slack: `#platform-oncall`
- Page the backup on-call after 30 min of unsolved page.
