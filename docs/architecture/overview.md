# 🏗️ Architecture Overview

This document explains **why** the system is designed this way, not just **what** it does.

---

## The Big Picture

```
                        ┌──────────────────────────────────────────────────────────────┐
                        │                    Azure Cloud                                │
                        │                                                              │
                        │  ┌──────────────────────────────────────────────────────┐   │
                        │  │               Azure Virtual Network (VNet)            │   │
                        │  │                   10.0.0.0/16                         │   │
                        │  │                                                        │   │
                        │  │  ┌─────────────────┐    ┌───────────────────────────┐ │   │
Internet ──► DNS ──────►│  │  │  App GW Subnet  │    │       AKS Subnet          │ │   │
                        │  │  │  10.0.9.0/24    │    │       10.0.0.0/22         │ │   │
                        │  │  │                 │    │                           │ │   │
                        │  │  │  ┌───────────┐  │    │  ┌─────────────────────┐  │ │   │
                        │  │  │  │ App       │──┼────┼──►  api-gateway pod    │  │ │   │
                        │  │  │  │ Gateway   │  │    │  │  (JWT + routing)    │  │ │   │
                        │  │  │  └───────────┘  │    │  └─────────┬───────────┘  │ │   │
                        │  │  └─────────────────┘    │            │               │ │   │
                        │  │                          │    ┌───────┴──────────┐    │ │   │
                        │  │                          │    │                  │    │ │   │
                        │  │                          │  ┌─▼──────────┐ ┌────▼──┐ │ │   │
                        │  │                          │  │user-service│ │task-  │ │ │   │
                        │  │                          │  │(auth/users)│ │service│ │ │   │
                        │  │                          │  └────────────┘ └───┬───┘ │ │   │
                        │  │                          │                      │     │ │   │
                        │  │                          │  ┌───────────────────▼──┐  │ │   │
                        │  │                          │  │  notification-service │  │ │   │
                        │  │                          │  │  (Service Bus consumer│  │ │   │
                        │  │                          │  └──────────────────────┘  │ │   │
                        │  │                          └───────────────────────────┘ │   │
                        │  │                                                        │   │
                        │  │  ┌─────────────────┐    ┌───────────────────────────┐ │   │
                        │  │  │   DB Subnet     │    │    Azure PaaS Services    │ │   │
                        │  │  │  10.0.8.0/24    │    │                           │ │   │
                        │  │  │  ┌───────────┐  │    │  • Azure Container Registry│ │   │
                        │  │  │  │PostgreSQL │  │    │  • Azure Key Vault         │ │   │
                        │  │  │  │Flex Server│  │    │  • Azure Service Bus       │ │   │
                        │  │  │  └───────────┘  │    │  • Azure Cache for Redis   │ │   │
                        │  │  └─────────────────┘    │  • Log Analytics Workspace │ │   │
                        │  │                          └───────────────────────────┘ │   │
                        │  └──────────────────────────────────────────────────────┘   │
                        └──────────────────────────────────────────────────────────────┘
```

---

## Why Microservices?

### The Problem with Monoliths

Imagine all 4 services as one giant app:
- One bug in the notification code could crash the **entire** platform
- You can't scale just the task API during high load — you scale everything
- Every deployment requires testing and restarting the whole app
- Different teams can't work independently

### The Microservices Solution

Each service:
- **Fails independently** — if notification-service crashes, users can still log in and create tasks
- **Scales independently** — if task-service gets 10x traffic, only task pods multiply
- **Deploys independently** — fix a bug in user-service without touching anything else
- **Is owned independently** — different team, different repo, different tech if needed

### The Trade-offs (be honest!)

Microservices are **NOT free**:
- **Distributed systems complexity** — network calls fail, you need retries, timeouts, circuit breakers
- **Observability overhead** — tracing a request across 4 services is harder than reading one log file
- **Operational burden** — 4 Dockerfiles, 4 Helm charts, 4 CI pipelines

**Bottom line**: This architecture makes sense for teams of 5+ people working on 4+ features simultaneously. For a solo developer, a well-structured monolith is often better. We build it as microservices here because **you're learning**, not because it's always the right choice.

---

## Data Flow: Creating a Task

Here's what happens when a user creates a task (the life of one HTTP request):

```
1. Browser sends:  POST https://api.your-domain.com/api/v1/tasks
                   Authorization: Bearer eyJhbGciOi...

2. App Gateway (Azure) → routes TCP to NGINX Ingress pod

3. NGINX Ingress → routes HTTP to api-gateway Service (ClusterIP)

4. api-gateway pod:
   a. JWT middleware: validates token, extracts user_id
   b. Rate limiter: checks request count for this IP
   c. Proxy: forwards to http://task-service:8002/api/v1/tasks
      with header: X-User-Id: <user_id>

5. task-service pod:
   a. Receives request, validates body with Pydantic
   b. Writes task row to PostgreSQL (via VNet private endpoint)
   c. Publishes "task.created" event to Azure Service Bus topic

6. Service Bus:
   a. Stores message durably (survives restarts)
   b. Delivers to all subscribers (currently: notification-service)

7. notification-service pod:
   a. Background consumer receives message
   b. Logs notification (or sends email in a real system)
   c. Acknowledges message (removes from queue)

8. Response flows back: task-service → api-gateway → NGINX → App GW → Browser
   HTTP 201 Created with task JSON body
```

---

## Key Design Decisions

### 1. Why AKS instead of Azure Container Apps or Azure App Service?

**Azure Container Apps** would be simpler but teaches less. AKS exposes the full Kubernetes primitives — you understand **why** things work, not just that they do.

**Azure App Service** is great for traditional apps but doesn't teach container orchestration at all.

**AKS** gives you: node pools, pod scheduling, RBAC, network policies, custom metrics — the full cloud-native toolkit.

### 2. Why Azure Service Bus instead of direct HTTP calls?

Direct calls: `task-service → POST http://notification-service/notify`

Problems with direct calls:
- What if notification-service is down? The task creation fails too
- What if you want to add an analytics-service later? You'd modify task-service
- What if the notification takes 10 seconds? The user waits 10 seconds

Service Bus (async messaging) solves all three:
- notification-service can be down — messages queue up and are delivered when it recovers
- Add analytics-service? Just add another subscription — task-service doesn't change
- Notifications happen asynchronously — users get instant responses

### 3. Why not store secrets in environment variables directly?

Bad approach: `DATABASE_PASSWORD=MyPassword123` in a K8s YAML file committed to git

Problems:
- Git history = permanent record. Rotate the password, the old one is still in git history
- Anyone with repo access sees all passwords
- No audit trail of who accessed what

**Key Vault + CSI Driver** approach:
- Secrets stored in Azure Key Vault (FIPS-validated HSM)
- AKS pods mount secrets as files at runtime using Managed Identity (no passwords to authenticate!)
- Full audit log of every secret access in Azure Monitor

---

## Security Layers

```
Layer 1: Azure AD / Entra ID
  → Who can access Azure resources at all?

Layer 2: Azure RBAC
  → What can each identity do in Azure? (AcrPull, KeyVault Secrets User)

Layer 3: Network (VNet + NSGs + Private Endpoints)
  → What can reach what over the network? (DB is not internet-accessible)

Layer 4: Kubernetes RBAC
  → What can pods do within the cluster? (read secrets, not create them)

Layer 5: Kubernetes Network Policies
  → Which pods can call which other pods?

Layer 6: Application (JWT)
  → Is this HTTP request from an authenticated user?

Layer 7: Application (Authorization)
  → Can THIS user perform THIS action on THIS resource?
```

Seven layers. Every layer is redundant — if one fails, the others still protect you.
