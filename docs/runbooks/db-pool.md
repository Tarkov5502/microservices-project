# Runbook: PostgresConnectionPoolExhausted

> The service's SQLAlchemy pool has been at >90% utilisation for 5 min.
> Requests are about to start timing out at `pool_timeout=10s`.

## Instrumenting the metric

This alert requires the `pg_pool_in_use` and `pg_pool_size` gauges. They are
NOT exported by default. Add the following custom collector to the service
that exhibits the symptom (paste at the bottom of `app/database.py`):

```python
from prometheus_client import Gauge

POOL_SIZE   = Gauge("pg_pool_size",   "Total size of the SQLAlchemy pool",   ["service"])
POOL_IN_USE = Gauge("pg_pool_in_use", "Connections currently checked out",   ["service"])

def _update_pool_metrics():
    POOL_SIZE.labels(service=settings.environment).set(engine.pool.size())
    POOL_IN_USE.labels(service=settings.environment).set(engine.pool.checkedout())

# Call from a startup task or a background loop every 10 s.
```

## First three things to check

1. **Slow queries** — `pg_stat_activity` for sessions in `idle in transaction`
   state. Long-held transactions hold pool slots forever.
2. **HPA scaling** — has the service scaled out? Each replica multiplies the
   total connection footprint.
3. **Postgres `max_connections`** — server-side cap. If you've outgrown the
   B1ms SKU's default 50, time to scale up the SKU.

## Mitigation

- Kill the offending sessions: `SELECT pg_terminate_backend(pid)`.
- Temporarily raise `db_pool_size` if the load is real and sustained.
- Long-term: add PgBouncer in front of Postgres so each pod opens fewer
  connections.
