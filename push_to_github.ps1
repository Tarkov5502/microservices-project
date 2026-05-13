$git = "C:\Users\s0e086y\AppData\Local\Programs\Git\cmd\git.exe"
$proj = "C:\Users\s0e086y\Documents\puppy_workspace\microservices-project"
Set-Location $proj

& $git config user.email "tarkov5502@github.com"
& $git config user.name "Tarkov5502"

& $git add -A

Write-Host "=== Staged changes ===" -ForegroundColor Cyan
& $git status --short

$msg = @"
feat: tests, docker-compose, monitoring, local-dev + deployment docs

Tests (was: all stubs, CI passed while testing nothing):
  api-gateway: rate limiter, auth middleware, security headers, proxy headers
  user-service: schema validators, audit log structure
  task-service: HTML stripping validators, PATCH semantics
  notification-service: event dispatch, graceful error handling

Local development:
  docker-compose.yml - full stack with postgres, redis, 4 services, grafana
  scripts/init-db.sql - per-service database creation
  monitoring/prometheus.yml - scrape config for all 4 services
  monitoring/grafana/provisioning - auto-wired Prometheus datasource
  notification-service - graceful no-op when Service Bus env var is empty

Kubernetes:
  kubernetes/monitoring/monitoring.yaml - ServiceMonitor CRD + PrometheusRules

Documentation (was: dead links in README):
  docs/guides/local-development.md - docker compose quickstart, troubleshooting
  docs/guides/deployment.md - 10-phase production deployment guide
  README.md - added local dev section, fixed all broken doc links
"@

& $git commit -m $msg

Write-Host ""
Write-Host "=== Recent commits ===" -ForegroundColor Cyan
& $git log --oneline -6

Write-Host ""
Write-Host "=== Pushing to GitHub... ===" -ForegroundColor Yellow
& $git push origin main

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
