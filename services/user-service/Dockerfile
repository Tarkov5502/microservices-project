FROM python:3.12-slim AS builder
# PYTHONDONTWRITEBYTECODE: stops Python writing .pyc files — the runtime image
#   has readOnlyRootFilesystem:true, so write attempts produce noisy warnings.
# PYTHONUNBUFFERED: forces stdout/stderr to be unbuffered so crash logs aren't
#   lost in the buffer when the container exits unexpectedly.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv==0.2.0
WORKDIR /build
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

FROM python:3.12-slim AS runtime
RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid 1000 --no-create-home appuser
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
WORKDIR /app
COPY app/ ./app/
# Copy Alembic config and migrations so the entrypoint can run 'alembic upgrade head'
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
USER appuser
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health')"
# entrypoint.sh runs 'alembic upgrade head' then exec's uvicorn.
# This ensures schema migrations run and complete before the app accepts traffic.
CMD ["./entrypoint.sh"]
