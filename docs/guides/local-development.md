# Local Development Guide

> Get the entire platform running on your laptop in about 10 minutes — no Azure account needed.

---

## Prerequisites

Install these tools first:

| Tool | Purpose | Install |
|------|---------|---------|
| Docker Desktop | Run containers locally | https://docs.docker.com/get-docker/ |
| Git | Clone the repo | https://git-scm.com |

That's literally it for local development. You don't need Azure CLI, kubectl, Terraform, or Helm to just run and experiment with the services.

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/Tarkov5502/microservices-project.git
cd microservices-project

# 2. Start everything (first run builds the images — takes ~2 minutes)
docker compose up --build

# 3. Wait until you see all four services show "Application startup complete"
# Then visit: http://localhost:8000/docs
```

That's it. You now have:
- **4 microservices** running and talking to each other
- **PostgreSQL** with two isolated databases (userdb, taskdb)
- **Redis** for caching
- **Prometheus** scraping metrics from all services
- **Grafana** with pre-configured dashboards

---

## Service URLs

| Service | URL | Notes |
|---------|-----|-------|
| API Gateway (main entry point) | http://localhost:8000 | All requests should go here |
| API Gateway docs | http://localhost:8000/docs | Swagger UI — try endpoints here |
| User Service (direct) | http://localhost:8001/docs | Bypass gateway for debugging |
| Task Service (direct) | http://localhost:8002/docs | Bypass gateway for debugging |
| Notification Service | http://localhost:8003/docs | No UI — just /health and /metrics |
| Prometheus | http://localhost:9090 | Query raw metrics |
| Grafana | http://localhost:3000 | Dashboards (admin/admin) |

---

## Try It: End-to-End Request Flow

Open http://localhost:8000/docs and follow this sequence:

### Step 1 — Register a user
```
POST /api/v1/auth/register
{
  "email": "alice@example.com",
  "username": "alice",
  "password": "SecurePass1"
}
```

### Step 2 — Log in and get a token
```
POST /api/v1/auth/login
{
  "email": "alice@example.com",
  "password": "SecurePass1"
}
```
Copy the `access_token` from the response. Click **Authorize** in Swagger and paste it.

### Step 3 — Create a project
```
POST /api/v1/projects
{
  "name": "My First Project",
  "description": "Learning AKS"
}
```

### Step 4 — Create a task
```
POST /api/v1/tasks
{
  "title": "Read the Terraform guide",
  "project_id": "<project_id from step 3>",
  "priority": "HIGH"
}
```

### Step 5 — Watch the metrics
Open http://localhost:9090 and query:
```
gateway_requests_total
```
You'll see the counter increment for each request you made.

---

## Running Tests

```bash
# Run all tests across all services
cd services/api-gateway && pip install pytest httpx && pytest tests/ -v
cd services/user-service && pip install pytest && pytest tests/ -v
cd services/task-service && pip install pytest && pytest tests/ -v
cd services/notification-service && pip install pytest pytest-asyncio && pytest tests/ -v
```

Or run them all from the CI workflow locally using `act` (if installed):
```bash
act pull_request
```

---

## Common Commands

```bash
# Start only the infrastructure (postgres + redis), not the services
docker compose up postgres redis

# Rebuild and restart a single service after code changes
docker compose up --build user-service

# Follow logs from all services
docker compose logs -f

# Follow logs from one service
docker compose logs -f api-gateway

# Open a shell inside a running container (for debugging)
docker compose exec user-service bash

# Wipe everything (database data too!) and start fresh
docker compose down -v
docker compose up --build

# Check what's running
docker compose ps
```

---

## Differences from Production (AKS)

| Aspect | Local (Docker Compose) | Production (AKS) |
|--------|----------------------|-----------------|
| PostgreSQL | Single container | Azure PostgreSQL Flexible Server |
| Redis | Single container | Azure Cache for Redis (TLS 6380) |
| Service Bus | Disabled | Azure Service Bus (real events) |
| Secrets | Environment variables | Azure Key Vault |
| HTTPS | Plain HTTP | TLS via cert-manager |
| Scaling | Single replica | HPA 2-10 replicas |
| Networking | Docker bridge | Azure CNI + NetworkPolicies |

The application code is **identical** — only the infrastructure config differs. This is the whole point of the twelve-factor app methodology and containerisation.

---

## Troubleshooting

**Services fail to start with "connection refused" to postgres**
> The DB healthcheck gates service startup. Wait 30 seconds and try again. Run `docker compose ps` to see which containers are healthy.

**"email already registered" on first run**
> The database persisted from a previous run. Run `docker compose down -v` to wipe volumes.

**Port already in use**
> Something else is using port 8000, 8001, etc. Find it with `lsof -i :8000` (Mac/Linux) or `netstat -ano | findstr 8000` (Windows).

**notification-service shows "consumer disabled"**
> Expected! Service Bus is not configured locally. Set `SERVICEBUS_CONNECTION_STRING` in `docker-compose.yml` if you want to test event processing.
