# ☁️ AKS Cloud-Native Microservices Platform

> A production-grade learning platform covering **Azure**, **Kubernetes (AKS)**, and **Terraform** end-to-end.
> Built for absolute beginners who want to understand how real cloud-native systems are architected and deployed.

---

## 🏗️ What You're Building

A **Task Management Platform** (think mini-Jira) composed of four independent microservices,
all running on Azure Kubernetes Service, provisioned entirely with Terraform.

```
                           ┌─────────────────────────────────────────────────┐
                           │             Azure Resource Group                 │
                           │                                                  │
Internet ──► NGINX Ingress ►  AKS Cluster                                    │
                           │   ├── api-gateway   (routes requests)           │
                           │   ├── user-service  (auth + user management)    │
                           │   ├── task-service  (CRUD tasks + projects)     │
                           │   └── notification-service (async messaging)    │
                           │                                                  │
                           │  Azure Services:                                 │
                           │   ├── Azure Container Registry (ACR)            │
                           │   ├── Azure PostgreSQL Flexible Server          │
                           │   ├── Azure Cache for Redis                     │
                           │   ├── Azure Service Bus                         │
                           │   ├── Azure Key Vault                           │
                           │   └── Log Analytics + Container Insights        │
                           └─────────────────────────────────────────────────┘
```

> NGINX Ingress Controller is the cluster's external entry point today. Azure
> Application Gateway with WAF in front of the cluster is a planned
> enhancement — it is referenced in some docs but is not provisioned by the
> current Terraform.

---

## 📚 What You'll Learn

| Technology | Concepts Covered |
|---|---|
| **Azure** | Resource Groups, VNets, Subnets, NSGs, AKS, ACR, PostgreSQL, Redis, Service Bus, Key Vault, Log Analytics |
| **Kubernetes** | Pods, Deployments, Services, Ingress, ConfigMaps, Secrets, RBAC, Network Policies, HPA, Resource Limits |
| **Terraform** | Providers, Modules, Variables, Outputs, State, Workspaces, Remote Backend, Environment separation |
| **CI/CD** | GitHub Actions, Docker multi-stage builds, Helm charts, automated deployment pipelines |
| **Observability** | Prometheus metrics, Grafana dashboards, Azure Container Insights |

---

## 📁 Project Structure

```
microservices-project/
├── .github/workflows/          # CI/CD pipelines (GitHub Actions)
│   ├── ci.yml                  # Build + test every service on PR
│   ├── deploy-infra.yml        # Terraform plan/apply pipeline
│   └── deploy-services.yml     # Build images + deploy to AKS
│
├── terraform/                  # All infrastructure as code
│   ├── bootstrap/              # One-time remote-state backend bootstrap
│   ├── modules/                # Reusable building blocks
│   │   ├── networking/         # VNet, Subnets, NSGs
│   │   ├── aks/                # AKS cluster config
│   │   ├── acr/                # Azure Container Registry
│   │   ├── database/           # PostgreSQL Flexible Server
│   │   ├── redis/              # Azure Cache for Redis
│   │   ├── servicebus/         # Azure Service Bus
│   │   ├── keyvault/           # Azure Key Vault
│   │   └── monitoring/         # Log Analytics workspace
│   └── environments/
│       ├── dev/                # Dev environment (smaller/cheaper)
│       └── prod/               # Prod environment (HA, redundant)
│
├── kubernetes/                 # Raw Kubernetes manifests
│   ├── namespaces/             # Namespace definitions
│   ├── rbac/                   # Role-based access control
│   ├── network-policies/       # Pod-to-pod traffic rules
│   ├── ingress/                # NGINX Ingress controller config
│   ├── monitoring/             # Prometheus + Grafana
│   └── services/               # Per-service K8s manifests
│       ├── api-gateway/
│       ├── user-service/
│       ├── task-service/
│       └── notification-service/
│
├── services/                   # Application source code
│   ├── api-gateway/            # FastAPI — routes + auth middleware
│   ├── user-service/           # FastAPI — users, JWT auth
│   ├── task-service/           # FastAPI — tasks, projects
│   └── notification-service/   # FastAPI — async event processor
│
├── helm/                       # Helm charts (K8s package manager)
│   ├── api-gateway/
│   ├── user-service/
│   ├── task-service/
│   └── notification-service/
│
├── docs/                       # Deep-dive documentation
│   ├── architecture/           # System design + diagrams
│   ├── azure/                  # Azure services explained
│   ├── terraform/              # Terraform concepts + walkthrough
│   ├── kubernetes/             # K8s concepts + walkthrough
│   └── guides/                 # Getting started, local dev, deploy
│
└── scripts/                    # Helper shell scripts
```

---

## 🐳 Local Development (No Azure Required)

```bash
docker compose up --build
# → API Gateway:  http://localhost:8000/docs
# → Grafana:      http://localhost:3000  (admin/admin)
# → Prometheus:   http://localhost:9090
```

See [docs/guides/local-development.md](docs/guides/local-development.md) for a full walkthrough.

---

## 🚀 Quick Start

> **Prerequisites:** Azure CLI, Terraform 1.7+, kubectl, Helm 3, Docker

```bash
# 1. Clone the repo
git clone https://github.com/Tarkov5502/microservices-project.git
cd microservices-project

# 2. Read the getting-started guide first!
cat docs/guides/getting-started.md

# 3. Set up Azure credentials
az login
az account set --subscription "YOUR_SUBSCRIPTION_ID"

# 4. Bootstrap the Terraform remote-state backend (ONCE per subscription)
cd terraform/bootstrap
terraform init
terraform apply
# Note the `storage_account_name` output — you'll need it in step 5.
cd ../..

# 5. Deploy the dev environment (Terraform)
cd terraform/environments/dev
terraform init \
  -backend-config="storage_account_name=<paste-output-from-step-4>"
terraform plan
terraform apply
cd ../../..

# 6. Connect kubectl to your new AKS cluster
az aks get-credentials \
  --resource-group rg-microservices-dev \
  --name aks-microservices-dev

# 7. Deploy Kubernetes base resources
kubectl apply -f kubernetes/namespaces/
kubectl apply -f kubernetes/rbac/
kubectl apply -f kubernetes/network-policies/

# 8. Deploy services with Helm. Pass the ACR login server so image
#    references resolve correctly.
ACR_LOGIN_SERVER=$(az acr show -n <your-acr-name> --query loginServer -o tsv)

for svc in api-gateway user-service task-service notification-service; do
  helm upgrade --install "$svc" "./helm/$svc" \
    -n microservices \
    --set "image.repository=${ACR_LOGIN_SERVER}/${svc}" \
    --set "image.tag=$(git rev-parse --short HEAD)"
done

# 9. Bootstrap the first admin user.
#    a. Register a user normally:
#         curl -X POST https://<your-ingress>/api/v1/auth/register \
#              -H 'content-type: application/json' \
#              -d '{"email":"you@example.com","username":"you","password":"YourPass1"}'
#    b. Set INITIAL_ADMIN_EMAIL on the user-service deployment to that email
#       and let the pod restart. On startup the matching user is promoted.
#         kubectl set env deploy/user-service \
#           -n microservices INITIAL_ADMIN_EMAIL=you@example.com
#    c. Once promoted, UNSET the env var so future restarts are a no-op:
#         kubectl set env deploy/user-service -n microservices INITIAL_ADMIN_EMAIL-
```

> Prefer raw `kubectl apply` over Helm? Run `ACR_LOGIN_SERVER=<acr.azurecr.io>
> ./scripts/render-k8s-manifests.sh` first to expand the image placeholders,
> then `kubectl apply -f kubernetes/services-rendered/`.

---

## 📖 Documentation Index

| Guide | Description |
|---|---|
| [Getting Started](docs/guides/getting-started.md) | Prerequisites, account setup, first deploy |
| [Architecture Overview](docs/architecture/overview.md) | System design, data flow, decisions |
| [Azure Services Guide](docs/azure/azure-services.md) | Every Azure service used, explained simply |
| [Terraform Guide](docs/terraform/terraform-guide.md) | IaC concepts, modules, state, workspaces |
| [Kubernetes Guide](docs/kubernetes/kubernetes-guide.md) | K8s objects, networking, scaling |
| [Security Guide](docs/security/security-guide.md) | Defence-in-depth: every security control explained |
| [Local Development](docs/guides/local-development.md) | Run all services with Docker Compose (no Azure needed) |
| [Deployment Guide](docs/guides/deployment.md) | Step-by-step production deployment to AKS |

---

## 🧩 Microservices

| Service | Port | Responsibilities |
|---|---|---|
| `api-gateway` | 8000 | Request routing, JWT validation, rate limiting |
| `user-service` | 8001 | User CRUD, registration, login (issues JWTs) |
| `task-service` | 8002 | Projects, tasks, CRUD, event publishing |
| `notification-service` | 8003 | Consumes Service Bus events, sends notifications |

---

## 🤝 Contributing

See [docs/guides/getting-started.md](docs/guides/getting-started.md).
All PRs trigger the CI pipeline automatically.

---

*Built with ❤️ as a learning platform. Start with the docs — they're written for beginners!*
