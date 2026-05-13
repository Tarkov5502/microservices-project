# 🌐 Azure Services Guide

> A plain-English explanation of every Azure service used in this project,
> why we use it, and how it connects to the rest of the system.

---

## Azure Resource Group (`azurerm_resource_group`)

**What it is**: A logical container for all your Azure resources.

**Mental model**: Like a folder on your computer. Everything inside shares the same lifecycle — delete the resource group, delete everything in it.

**Why it matters**: Billing, access control, and tags all work at the resource group level. One resource group per environment (dev/prod) means clean separation.

```
rg-microservices-dev/
  ├── aks-microservices-dev
  ├── psql-microservices-dev
  ├── redis-microservices-dev
  ├── sb-microservices-dev
  ├── kv-microservices-dev
  ├── acrmicroservicesdev
  └── vnet-microservices-dev
```

---

## Azure Virtual Network (VNet) + Subnets

**What it is**: Your private network in the cloud. Resources inside a VNet can talk to each other using private IPs. Resources outside cannot, unless you explicitly allow it.

**Mental model**: Like a private office LAN. Computers on the same LAN can see each other; computers on the internet cannot unless you open a specific port.

**Our subnet layout**:

| Subnet | CIDR | What lives here |
|---|---|---|
| `snet-aks-dev` | `10.0.0.0/22` | AKS nodes + pods |
| `snet-db-dev` | `10.0.8.0/24` | PostgreSQL Flexible Server |
| `snet-appgw-dev` | `10.0.9.0/24` | Application Gateway |

**Why subnets?** Different security rules per tier. The DB subnet has an NSG that only allows port 5432 from the AKS subnet — the database is completely unreachable from the internet.

---

## Azure Kubernetes Service (AKS)

**What it is**: Managed Kubernetes. Microsoft runs the control plane (API server, etcd, scheduler) for free. You pay only for worker nodes (VMs).

**What you'd pay for otherwise**: Running Kubernetes yourself requires ~3 control plane VMs at all times, plus expertise to maintain them. AKS eliminates that cost and complexity.

**Key AKS concepts in this project**:

| Concept | What it does |
|---|---|
| **System node pool** | Runs K8s system components (CoreDNS, kube-proxy). Tainted so your apps don't land here |
| **App node pool** | Runs your microservices. Can scale independently of system nodes |
| **Cluster Autoscaler** | Adds/removes VMs based on pending pods. If HPA wants 10 pods but 3 nodes can only fit 8, CA adds a 4th node |
| **Azure CNI networking** | Each pod gets a real VNet IP. Enables direct pod-to-pod routing and Service Endpoints |
| **Managed Identity** | AKS authenticates to Azure services (ACR, Key Vault) without any passwords |
| **OMS Agent** | Streams pod logs and metrics to Log Analytics (Container Insights) |

---

## Azure Container Registry (ACR)

**What it is**: A private Docker image registry, like Docker Hub but in your own Azure account.

**Why not just use Docker Hub?** 
- Docker Hub images are public by default
- Docker Hub rate-limits pulls (unauthenticated: 100/6hrs)
- ACR is in the same Azure region as your AKS cluster → faster pulls, no egress costs

**How AKS authenticates to ACR**: We assign the `AcrPull` role to the AKS kubelet Managed Identity. When a pod starts and needs to pull `acrmicroservicesdev.azurecr.io/api-gateway:abc123`, AKS uses its identity to get a short-lived token from ACR. No passwords anywhere!

---

## Azure Database for PostgreSQL Flexible Server

**What it is**: Fully managed PostgreSQL. Azure handles:
- OS patching
- PostgreSQL version upgrades
- Automated backups (point-in-time restore up to 35 days)
- High Availability with automatic failover
- Connection pooling (PgBouncer built-in)

**Why not run PostgreSQL in Kubernetes?** You could. But:
- K8s databases require StatefulSets, Persistent Volumes, backup operators — significant complexity
- If a node fails and the volume doesn't detach cleanly, you might corrupt data
- Azure handles all of this for you for ~$50-200/month

**Private networking**: The Flexible Server is `delegated` into the DB subnet — it gets a private IP (`10.0.8.x`) and has **no public endpoint at all**. Only resources inside the VNet can connect.

---

## Azure Cache for Redis

**What it is**: Managed Redis — an in-memory key-value store.

**Use cases in this project**:
1. **Session caching**: Store JWT validation results for 60 seconds (avoid re-decoding on every request)
2. **Response caching**: Cache `GET /tasks?project_id=X` results — if nothing changed, no DB query needed
3. **Rate limiting**: Store per-IP request counters (production version of our in-memory rate limiter)

**Key Redis concepts**:
- **TTL** (Time to Live): Every cached value has an expiry. After TTL, it's automatically deleted
- **Eviction policy** (`allkeys-lru`): When memory is full, evict the Least Recently Used key
- **TLS port 6380**: Azure Redis only allows TLS connections (port 6380, not 6379)

---

## Azure Service Bus

**What it is**: Enterprise message broker with Topics, Subscriptions, and dead-letter queues.

**Mental model**: Like email, but for software. A service publishes a message (sends an email) to a Topic. All Subscriptions (recipients) get a copy. If a subscription fails to process it (message goes to junk), Service Bus retries up to `max_delivery_count` times, then moves it to the dead-letter queue.

**Topics vs Queues**:
- **Queue**: One sender, one receiver. Message consumed by one consumer only
- **Topic**: One sender, many receivers. Each Subscription gets its own independent copy

We use Topics so we can add more subscribers later (analytics, audit) without changing task-service.

**Dead-letter queue**: Messages that fail `max_delivery_count` (10) times move here. You can inspect them in Azure Portal, fix the bug, and replay them. Critical for data integrity.

---

## Azure Key Vault

**What it is**: A FIPS 140-2 validated secret store. Stores secrets (passwords), keys (encryption), and certificates (TLS).

**Why not just put passwords in environment variables?**
- Env vars end up in Docker images, K8s manifests, CI logs, and git history
- There's no audit log of who read the password
- Rotating a password means updating env vars everywhere

**With Key Vault**:
- Secrets are stored encrypted in a hardened vault
- Apps fetch secrets at runtime using Managed Identity — no passwords to authenticate!
- Full audit log: every `GET secret/db-password` is logged with who, when, from where
- Rotate a secret? Update it in Key Vault. Apps pick it up on next restart. That's it.

**Soft-delete + Purge Protection**: Even if someone accidentally deletes a secret (or tries ransomware), it's retained for 90 days and can be recovered. During that 90 days, even admins can't permanently delete it.

---

## Azure Log Analytics + Container Insights

**What it is**: Log Analytics is Azure's centralized log aggregation and query service. Container Insights is a pre-built solution on top of it specifically for Kubernetes.

**What gets collected automatically** (after enabling OMS Agent on AKS):
- Container stdout/stderr logs
- Node CPU/memory/disk metrics
- Pod status (Running, Pending, Failed, CrashLoopBackOff)
- Kubernetes events (image pull failures, scheduling failures)

**KQL example** — find all containers that crashed in the last hour:
```kql
ContainerLog
| where TimeGenerated > ago(1h)
| where LogEntry contains "Error" or LogEntry contains "Exception"
| where Namespace == "microservices"
| project TimeGenerated, ContainerName, LogEntry
| order by TimeGenerated desc
```

---

## How They All Connect

```
[Browser]
    │ HTTPS
    ▼
[Application Gateway] ← TLS termination, WAF rules
    │ HTTP
    ▼
[NGINX Ingress] ← Routes by hostname/path
    │ HTTP
    ▼
[api-gateway pod]
    │ reads JWT_SECRET from ──────────────────► [Key Vault]
    │ HTTP proxy to
    ▼
[user-service pod] ──── reads DATABASE_URL ──► [Key Vault]
[task-service pod]  ──── writes events ───────► [Service Bus]
                    ──── reads/writes ─────────► [PostgreSQL]
                    ──── caches ──────────────► [Redis]
    │
    ▼ (async, via Service Bus)
[notification-service pod]
    │
    ▼ (logs)
[Log Analytics] ← all pods stream logs here
```
