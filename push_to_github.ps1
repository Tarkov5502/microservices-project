$git = "C:\Users\s0e086y\AppData\Local\Programs\Git\cmd\git.exe"
$proj = "C:\Users\s0e086y\Documents\puppy_workspace\microservices-project"
Set-Location $proj

& $git config user.email "tarkov5502@github.com"
& $git config user.name "Tarkov5502"

# Stage everything
& $git add -A

# Show a quick status
Write-Host "=== Staged changes ===" -ForegroundColor Cyan
& $git status --short

# Commit with a detailed message listing every fix
& $git commit -m "fix: 12 security hardening fixes + PDB + audit logging

Critical fixes:
  - Rate limiter: was keying off NGINX pod IP (all users shared one bucket).
    Now reads X-Real-IP header set by NGINX → correct per-client buckets.
  - Auth endpoints: separate stricter rate limit (10/min) to block brute-force.
  - Service Bus NetworkPolicy: was completely missing → AMQP events silently
    dropped. Added allow-egress-to-servicebus (5671 AMQP + 443 HTTPS fallback).

High severity fixes:
  - bcrypt async: was blocking asyncio event loop (~300ms per call). Now uses
    asyncio.to_thread() to offload to thread pool without stalling requests.
  - Redis NetworkPolicy: was missing 'to:' field → allowed port 6380 to ANY
    destination (not just Redis). Scoped to AKS subnet CIDR.
  - OpenAPI schema: /openapi.json was exposed in production. Disabling /docs
    alone is insufficient - /openapi.json is an independent endpoint.
  - Security headers middleware: new SecurityHeadersMiddleware applies CSP,
    HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy,
    Permissions-Policy, and Cache-Control (no-store on auth paths) to every
    response. Removes server + x-powered-by fingerprinting headers.
  - X-Response-Time header now suppressed in production (timing side-channel).

Medium severity fixes:
  - JWT sub claim now validated as UUID before forwarding as X-User-Id.
  - X-HTTP-Method-Override, X-Method-Override, X-HTTP-Method added to
    gateway-owned headers list (stripped from all client requests).
  - PodDisruptionBudgets added for all 4 services (minAvailable: 1).
    Without PDBs, node drains can evict all replicas simultaneously.
  - Container-level securityContext added to all 4 Helm charts:
    allowPrivilegeEscalation: false, readOnlyRootFilesystem: true,
    capabilities.drop: [ALL], seccompProfile: RuntimeDefault.
  - Namespace PodSecurity upgraded from baseline to restricted.
  - Task title/description: Pydantic field_validator strips HTML tags
    before storage, preventing stored XSS attacks.
  - Audit logging: new app/audit.py in user-service emits structured JSON
    for login_success, login_failure, registration events.
  - .gitignore: now covers .env.* and *.env variants (not just .env).
  - DB pool_timeout=10 added to both services (prevents indefinite hangs).

New files:
  services/api-gateway/app/middleware/security_headers.py
  services/user-service/app/audit.py
  kubernetes/disruption-budgets/pdb.yaml
  docs/security/security-guide.md"

Write-Host ""
Write-Host "=== Recent commits ===" -ForegroundColor Cyan
& $git log --oneline -5

Write-Host ""
Write-Host "=== Pushing to GitHub... ===" -ForegroundColor Yellow
& $git push -u origin main

Write-Host ""
Write-Host "✅ Done! Check https://github.com/Tarkov5502/microservices-project" -ForegroundColor Green
