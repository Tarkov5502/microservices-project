FROM python:3.12-slim AS builder
# PYTHONDONTWRITEBYTECODE: stops .pyc writes — readOnlyRootFilesystem silently
#   breaks if Python tries to write bytecode to the container layer.
# PYTHONUNBUFFERED: crash logs reach stdout immediately, not buffered on exit.
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
# Copy alembic binary so entrypoint.sh can run 'alembic upgrade head'
COPY --from=builder /usr/local/bin/alembic /usr/local/bin/alembic
WORKDIR /app
COPY app/ ./app/
# Alembic config and migrations must be present for entrypoint to run migrations
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
USER appuser
EXPOSE 8002
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/health')"
# entrypoint.sh runs 'alembic upgrade head' then exec's uvicorn.
# This ensures migrations complete before the service accepts traffic.
CMD ["./entrypoint.sh"]
