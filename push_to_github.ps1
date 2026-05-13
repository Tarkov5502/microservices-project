$git = "C:\Users\s0e086y\AppData\Local\Programs\Git\cmd\git.exe"
$proj = "C:\Users\s0e086y\Documents\puppy_workspace\microservices-project"
Set-Location $proj

& $git config user.email "tarkov5502@github.com"
& $git config user.name "Tarkov5502"

& $git add -A
Write-Host "=== Staged changes ===" -ForegroundColor Cyan
& $git status --short

$msg = @"
fix: 9 issues flagged by senior engineer audit

BUG FIX - users.py update_me used synchronous bcrypt.hashpw():
  bcrypt with rounds=12 takes ~300ms. Running it synchronously on the
  asyncio event loop blocked ALL concurrent requests for that duration.
  Fixed: asyncio.to_thread() — matches the pattern already used in auth.py.
  The auth route was correct; the update route was not.

DOCKERFILE FIX - PYTHONDONTWRITEBYTECODE + PYTHONUNBUFFERED missing:
  With readOnlyRootFilesystem:true, Python logs noisy bytecode-write warnings
  on every startup. PYTHONUNBUFFERED ensures crash logs aren't lost in the
  output buffer when the container exits unexpectedly. Added to all 4 images.

KUBERNETES FIX - imagePullPolicy: Always -> IfNotPresent (all 4 Helm charts):
  With SHA-pinned immutable tags, Always forces a registry roundtrip on every
  pod restart. If ACR is degraded, pods cannot restart even though the image
  is cached on the node. IfNotPresent uses the cache when the tag exists.

KUBERNETES FIX - ServiceMonitor label mismatch (Prometheus discovers nothing):
  The ServiceMonitor selector requires monitoring:"true" on Services.
  None of the 4 Helm Service templates had this label. Prometheus was
  auto-discovering exactly zero services. Added to all 4 service templates.

TERRAFORM FIX - prevent_destroy = false on AKS cluster:
  A comment in the file said "Set to true in real production!" but it wasn't.
  One mistyped terraform destroy would delete the entire production cluster.
  Changed to true. This block must be explicitly removed to destroy the cluster.

TEST DEPS FIX - pytest-asyncio not in any requirements file:
  notification-service tests use @pytest.mark.asyncio. Without pytest-asyncio
  installed, async tests are silently treated as synchronous (they "pass" without
  executing any async body). Added requirements-dev.txt to all 4 services.
  Updated ci.yml to install from requirements-dev.txt instead of inline packages.

MIGRATION FIX - no schema migration system (Alembic):
  create_all() only handles table creation. Schema changes (add column, alter
  constraint) in a live deployment are silently ignored. Added Alembic to
  user-service: alembic.ini, alembic/env.py (async-compatible), initial
  migration, entrypoint.sh (runs alembic upgrade head before uvicorn starts).
  task-service follows the same pattern (documented in deployment guide).

MONITORING FIX - Grafana had zero dashboards:
  Datasource was wired up but dashboard provisioning directory was empty.
  Grafana started completely blank. Added platform-overview.json dashboard
  with 6 panels: request rate, error rate, p50/p95/p99 latency, rate limit
  rejections, Service Bus event processing, and per-service health status.
  Added dashboards.yml provisioning config. Fixed docker-compose volume mount.
"@

& $git commit -m $msg

Write-Host ""
Write-Host "=== Recent commits ===" -ForegroundColor Cyan
& $git log --oneline -5

Write-Host ""
Write-Host "=== Pushing ===" -ForegroundColor Yellow
& $git push origin main
Write-Host "Done!" -ForegroundColor Green
