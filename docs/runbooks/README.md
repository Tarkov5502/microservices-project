# Runbooks

When an alert fires, the on-call engineer is sent here. Each runbook should:

1. Restate what the alert means in human language.
2. List the first three things to check (logs, dashboards, recent deploys).
3. Give concrete mitigation commands.
4. Name the people / teams to escalate to if the runbook doesn't fix it.

Keep runbooks SHORT (one page). They're read at 2 AM by someone with adrenaline,
not by an engineer in flow.

## Index

| Runbook | When it fires |
|---|---|
| [high-error-rate](high-error-rate.md) | Gateway 5xx rate above 1% for 10 min |
| [high-latency](high-latency.md) | Gateway p95 above 1 s for 10 min |
| [slo-fast-burn](slo-fast-burn.md) | Multi-window burn-rate alert is paging |
| [db-pool](db-pool.md) | Database connection pool is saturated |
| [circuit-breaker-open](circuit-breaker-open.md) | A gateway → backend circuit broke |
