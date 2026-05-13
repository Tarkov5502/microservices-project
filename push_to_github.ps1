$git = "C:\Users\s0e086y\AppData\Local\Programs\Git\cmd\git.exe"
$proj = "C:\Users\s0e086y\Documents\puppy_workspace\microservices-project"
Set-Location $proj

& $git config user.email "tarkov5502@github.com"
& $git config user.name "Tarkov5502"
& $git add -A
& $git status --short
& $git commit -m "feat: initial platform scaffold

Complete cloud-native microservices learning platform:

Infrastructure (Terraform):
- Modular design: networking, AKS, ACR, PostgreSQL, Redis, Service Bus, Key Vault, monitoring
- Dev + prod environments with separate state backends
- Remote state in Azure Blob Storage with locking

Kubernetes:
- Namespaces with ResourceQuota and LimitRange
- RBAC: ServiceAccounts, Roles, RoleBindings
- NetworkPolicies: default-deny with selective allow
- Deployments, Services, HPAs for all 4 microservices
- Ingress with cert-manager TLS annotations

Services (FastAPI + Python 3.12):
- api-gateway: JWT auth middleware, rate limiting, reverse proxy
- user-service: registration, bcrypt auth, JWT issuance, PostgreSQL
- task-service: CRUD + Service Bus event publishing
- notification-service: async Service Bus consumer

Helm Charts:
- Templated charts for all 4 services
- Environment-agnostic via values.yaml overrides

CI/CD (GitHub Actions):
- ci.yml: lint, test, docker build, terraform validate, helm lint
- deploy-infra.yml: terraform plan on PR, apply on merge (OIDC auth)
- deploy-services.yml: smart change detection, build + helm deploy

Documentation:
- Architecture overview with ASCII diagrams and design decisions
- Azure services guide (plain English)
- Terraform deep dive (concepts + workflow)
- Kubernetes guide (every object explained)
- Getting started guide (step-by-step from zero to running cluster)"
& $git log --oneline -5
Write-Host "`nPushing to GitHub..."
& $git push -u origin main
Write-Host "`nDone! Check https://github.com/Tarkov5502/microservices-project"
