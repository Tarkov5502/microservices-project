#!/bin/bash
# entrypoint.sh — task-service container startup script.
#
# WHY THIS EXISTS (same rationale as user-service/entrypoint.sh):
#   Running 'alembic upgrade head' here — BEFORE uvicorn starts — ensures:
#   1. Schema is at the correct version before the app accepts any traffic
#   2. Migration failures stop the container immediately (fail-fast)
#   3. PostgreSQL's advisory lock prevents concurrent migration runs when
#      multiple pods start simultaneously (only one process wins the lock,
#      the rest wait then find 'already applied' and skip)
#
# The alternative (K8s initContainer) is architecturally cleaner for very
# large teams but adds manifest complexity. For this project, baking it into
# the container entrypoint is a reasonable tradeoff.
#
# LEARNING NOTE: 'exec' below is critical. Without it, uvicorn is a child of
# the bash shell. SIGTERM from Kubernetes goes to bash, not uvicorn, so the
# container gets killed hard after terminationGracePeriodSeconds rather than
# draining in-flight requests gracefully.

set -e

echo "Running database migrations..."
alembic upgrade head
echo "Migrations applied successfully. Starting server..."

exec uvicorn app.main:app --host 0.0.0.0 --port 8002 --workers 1
