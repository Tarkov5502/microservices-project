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
Internet ──► App Gateway ──►  AKS Cluster                                    │
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

---

## 📚 What You'll Learn

| Technology | Concepts Covered |
|---|---|
| **Azure** | Resource Groups, VNets, Subnets, NSGs, AKS, ACR, PostgreSQL, Redis, Service Bus, Key Vault, App Gateway, Log Analytics |
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

## 🚀 Quick Start

> **Prerequisites:** Azure CLI, Terraform, kubectl, Helm, Docker

```bash
# 1. Clone the repo
git clone git@github.com:Tarkov5502/microservices-project.git
cd microservices-project

# 2. Read the getting started guide first!
cat docs/guides/getting-started.md

# 3. Set up Azure credentials
az login
az account set --subscription "YOUR_SUBSCRIPTION_ID"

# 4. Deploy dev infrastructure (Terraform)
cd terraform/environments/dev
terraform init
terraform plan
terraform apply

# 5. Connect kubectl to your new AKS cluster
az aks get-credentials --resource-group rg-microservices-dev --name aks-microservices-dev

# 6. Deploy Kubernetes base resources
kubectl apply -f kubernetes/namespaces/
kubectl apply -f kubernetes/rbac/
kubectl apply -f kubernetes/network-policies/

# 7. Deploy services with Helm
helm upgrade --install api-gateway ./helm/api-gateway -n microservices
helm upgrade --install user-service ./helm/user-service -n microservices
helm upgrade --install task-service ./helm/task-service -n microservices
helm upgrade --install notification-service ./helm/notification-service -n microservices
```

---

## 📖 Documentation Index

| Guide | Description |
|---|---|
| [Getting Started](docs/guides/getting-started.md) | Prerequisites, account setup, first deploy |
| [Architecture Overview](docs/architecture/overview.md) | System design, data flow, decisions |
| [Azure Services Guide](docs/azure/azure-services.md) | Every Azure service used, explained simply |
| [Terraform Guide](docs/terraform/terraform-guide.md) | IaC concepts, modules, state, workspaces |
| [Kubernetes Guide](docs/kubernetes/kubernetes-guide.md) | K8s objects, networking, scaling |
| [Local Development](docs/guides/local-development.md) | Run all services locally with Docker Compose |
| [Deployment Guide](docs/guides/deployment.md) | Step-by-step production deployment |

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
