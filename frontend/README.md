# Frontend

A single-file vanilla-JS UI for the platform. No build system, no npm —
deliberately so. The whole UI lives in `index.html` and exercises every API
the gateway exposes: register, login, refresh, logout, project CRUD, task
CRUD, the SSE notification stream, and self-service account deletion.

## Quick test (local)

```
# In one terminal — bring the platform up locally
docker compose up --build

# In another — serve this directory on port 8080
cd frontend
python3 -m http.server 8080
```

Open <http://localhost:8080>. The page calls the gateway at the same origin
by default — for a same-host docker-compose setup, set `API_BASE` at the top
of `index.html` to `http://localhost:8000`. CORS must include
`http://localhost:8080` in `ALLOWED_ORIGINS`.

## Production

For an AKS deploy, the cleanest pattern is:

1. Upload the contents of this directory to an Azure Blob Storage `$web`
   container (static website hosting). That gives you a public HTTPS URL
   like `https://<account>.z19.web.core.windows.net`.
2. Set the gateway's `ALLOWED_ORIGINS` to that URL.
3. Set `API_BASE` in `index.html` to your gateway's public hostname.

Or serve it directly from NGINX Ingress alongside the API. The Ingress
already routes `/api/*` to the gateway; add a second rule for `/` →
a tiny static-files Service backed by a ConfigMap holding `index.html`.

## Why no React / build step?

This is a learning project showcasing infrastructure, not a frontend
project. A single file you can read top-to-bottom is more valuable here
than a webpack pipeline. If/when you want to grow it into a real SPA,
swap this file for whatever framework you like — the API contract is
unchanged.
