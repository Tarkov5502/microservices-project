#!/bin/bash
# entrypoint.sh — user-service container startup script.
#
# WHY THIS EXISTS:
#   Running database migrations as part of the application startup (inside
#   lifespan()) has a race condition: if two pods start simultaneously, both
#   run migrations concurrently, which can produce duplicate-constraint errors
#   or partial schema states.
#
#   Running migrations HERE — before uvicorn starts — means:
#   1. Only one migration run happens per pod start
#   2. The app only starts if migrations succeed (fail-fast behaviour)
#   3. In K8s, this is effectively an init step baked into the main container
#      (the alternative is a K8s initContainer, which is cleaner for very
#      large teams but overkill here)
#
# NOTE: In K8s with multiple replicas, Alembic's migration table ('alembic_version')
# acts as a distributed lock via DB-level locking. PostgreSQL ensures only one
# process runs the migration to completion; others wait and then find the
# migration already applied.

set -e  # Exit immediately if any command fails

echo "Running database migrations..."
alembic upgrade head
echo "Migrations applied successfully. Starting server..."

# exec replaces the shell process with uvicorn so signals (SIGTERM/SIGINT)
# go directly to uvicorn for graceful shutdown — not to a shell wrapper.
exec uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers 1
