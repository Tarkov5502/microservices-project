# ☸️ Kubernetes Deep Dive

> Every Kubernetes concept used in this project, explained from first principles.

---

## What is Kubernetes?

Kubernetes (K8s) is a **container orchestrator** — it manages where containers run, keeps them healthy, and handles scaling.

**Without Kubernetes**: You have a VM, you run `docker run my-app`. If the process crashes, it stays dead. If traffic spikes, you manually spin up more VMs. If a VM dies, your app goes down.

**With Kubernetes**: You declare "I want 3 replicas of my-app running at all times." K8s makes it so. Pod crashes? K8s restarts it. VM dies? K8s reschedules pods to healthy nodes. Traffic spikes? HPA adds more pods automatically.

---

## Core Objects

### Pod

The **smallest deployable unit** — one or more containers sharing a network namespace and storage.

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: api-gateway-abc12
spec:
  containers:
    - name: api-gateway
      image: myacr.azurecr.io/api-gateway:1.0
      ports:
        - containerPort: 8000
```

**You almost never create Pods directly** — Deployments manage them for you.

### Deployment

A Deployment manages a **ReplicaSet** which manages **Pods**. It's the standard way to run stateless apps.

```
Deployment (desired state: 3 replicas)
    └── ReplicaSet (current state: 3 pods)
            ├── Pod 1 (Running on node-a)
            ├── Pod 2 (Running on node-b)
            └── Pod 3 (Running on node-c)
```

If Pod 2 dies, the ReplicaSet controller creates a replacement. You don't do anything.

Key Deployment features:
- **Rolling updates**: update pods one at a time with zero downtime
- **Rollback**: `kubectl rollout undo deployment/api-gateway`
- **History**: `kubectl rollout history deployment/api-gateway`

### Service

Pods have ephemeral IPs — they change every restart. A **Service** gives you a stable DNS name and IP that load-balances across healthy pods.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: api-gateway
spec:
  selector:
    app: api-gateway        # Select pods with this label
  ports:
    - port: 80              # Service listens on 80
      targetPort: 8000      # Forwards to pod port 8000
  type: ClusterIP           # Only reachable inside the cluster
```

Service types:
| Type | Reachable from | Use case |
|---|---|---|
| `ClusterIP` | Inside cluster only | Inter-service communication |
| `NodePort` | Node's public IP | Dev/testing only |
| `LoadBalancer` | Internet (Azure creates a Load Balancer) | Exposing services directly |

We use `ClusterIP` for all services and let the Ingress handle external traffic.

### Ingress

An Ingress routes **external HTTP/HTTPS** traffic to Services based on hostname and path.

```yaml
spec:
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /
            backend:
              service:
                name: api-gateway
                port:
                  name: http
```

The **Ingress Controller** (NGINX in our case) is a pod that watches Ingress resources and configures itself as a reverse proxy.

```
Internet → Azure Load Balancer → NGINX Ingress pod → Service → Pod
```

### ConfigMap

Key-value store for non-sensitive configuration:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
data:
  ENVIRONMENT: "production"
  LOG_LEVEL: "INFO"
```

### Secret

Like ConfigMap but for sensitive data. Values are base64-encoded (NOT encrypted by default — use Key Vault for real secrets!):
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: app-secrets
type: Opaque
data:
  jwt-secret: dG9wLXNlY3JldC1rZXk=   # base64 encoded
```

In this project, secrets are synced from Azure Key Vault to K8s Secrets using the **Secrets Store CSI Driver**.

---

## Resource Management

Every container should have:
- **`requests`**: What it needs to start. Used by the scheduler to find a node with enough capacity.
- **`limits`**: The hard maximum. If exceeded, the container is OOMKilled (memory) or throttled (CPU).

```yaml
resources:
  requests:
    cpu: "100m"      # 0.1 CPU core (m = millicores)
    memory: "128Mi"  # 128 MiB
  limits:
    cpu: "500m"      # 0.5 CPU cores
    memory: "512Mi"  # 512 MiB
```

**Rule of thumb**: `requests` = what it typically uses. `limits` = what it might spike to.

Without `requests`, the scheduler places pods randomly — a node might be oversubscribed and your pod gets OOMKilled.

Without `limits`, one misbehaving pod can starve all other pods on the same node.

---

## Health Probes

Three probes tell Kubernetes about your container's health:

```yaml
# Liveness: "Is the process alive?" → restart if fails
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 15   # Wait before first check
  periodSeconds: 30         # Check every 30s
  failureThreshold: 3       # Restart after 3 failures

# Readiness: "Can it handle traffic?" → remove from Service LB if fails
readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 3

# Startup: "Has it finished initializing?" → disables liveness until passes
startupProbe:
  httpGet:
    path: /health
    port: 8000
  failureThreshold: 30     # Allow up to 150s for slow startup
  periodSeconds: 5
```

The critical distinction:
- Liveness failure → **container restart** (use for deadlocked processes)
- Readiness failure → **removed from load balancer** (use for "DB connection not ready yet")

---

## Horizontal Pod Autoscaler (HPA)

HPA watches pod metrics and adjusts replica count automatically:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
spec:
  scaleTargetRef:
    kind: Deployment
    name: api-gateway
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70  # Scale when avg CPU > 70%
```

**HPA needs Metrics Server** (pre-installed on AKS) to read pod CPU/memory.

**HPA + Cluster Autoscaler together**:
1. Traffic spikes → HPA wants 8 pods
2. Only 3 nodes, can fit 6 pods max
3. Cluster Autoscaler sees `Pending` pods → adds a 4th node
4. New node ready → pods scheduled → traffic handled

---

## RBAC (Role-Based Access Control)

K8s RBAC controls what API calls pods and users can make to the K8s API server.

```
ServiceAccount (identity for a pod)
    └── bound to Role (via RoleBinding)
              └── allows specific verbs on specific resources
```

```yaml
# What can be done:
kind: Role
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "list"]   # Read only

# Who can do it:
kind: RoleBinding
subjects:
  - kind: ServiceAccount
    name: api-gateway
roleRef:
  kind: Role
  name: secret-reader
```

**Principle of least privilege**: Give pods only the permissions they actually need. A compromised pod should be able to do as little damage as possible.

---

## Network Policies

By default, every pod can reach every other pod. Network Policies lock this down:

```yaml
# Default deny all
spec:
  podSelector: {}      # All pods
  policyTypes: [Ingress, Egress]
  # No rules = deny everything

# Then selectively allow
spec:
  podSelector:
    matchLabels:
      app: user-service
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: api-gateway   # Only api-gateway can call user-service
```

Network Policies require a CNI plugin that supports them (we use Azure CNI with `network_policy = "azure"`).

---

## Useful kubectl Commands

```bash
# List all pods in the microservices namespace
kubectl get pods -n microservices

# Watch pod status in real time
kubectl get pods -n microservices -w

# Get pod logs (last 100 lines, follow)
kubectl logs -n microservices deployment/api-gateway --tail=100 -f

# Execute a command inside a running pod
kubectl exec -it -n microservices deployment/api-gateway -- sh

# Describe a pod (events, resource usage, probe results)
kubectl describe pod -n microservices <pod-name>

# Port-forward to access a service locally
kubectl port-forward -n microservices svc/api-gateway 8000:80

# Check HPA status
kubectl get hpa -n microservices

# Force a rolling restart
kubectl rollout restart deployment/api-gateway -n microservices

# Check rollout status
kubectl rollout status deployment/api-gateway -n microservices

# Roll back to previous version
kubectl rollout undo deployment/api-gateway -n microservices
```
