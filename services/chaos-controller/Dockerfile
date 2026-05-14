# =============================================================================
# chaos-controller — Dockerfile
# Minimal Python image. No build step beyond pip install. The service is
# pure Python with a single async runtime.
# =============================================================================
FROM python:3.12-slim

# Non-root user for runtime
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --create-home app
WORKDIR /app

# System deps (none required — we only need Python + uvicorn)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app/ ./app/

USER app

EXPOSE 8004

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8004/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004", "--no-access-log"]
