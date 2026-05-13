# ─── Build Stage ──────────────────────────────────────────────────────────────
# Multi-stage builds: Stage 1 compiles/installs deps, Stage 2 is the lean image.
# Result: smaller final image (no build tools included).
FROM python:3.12-slim AS builder

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv==0.2.0

WORKDIR /build
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# ─── Runtime Stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Security: create a non-root user to run the app
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --no-create-home appuser

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

WORKDIR /app
COPY app/ ./app/

# Switch to non-root user
USER appuser

EXPOSE 8000

# Health check built into the image (Docker-level, separate from K8s probes)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Use exec form (not shell form) so signals are received properly by uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
