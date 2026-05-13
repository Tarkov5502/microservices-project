"""
Shared telemetry initialisation — OpenTelemetry distributed tracing.

WHY DISTRIBUTED TRACING?
  Logs tell you WHAT happened. Metrics tell you HOW OFTEN.
  Traces tell you WHERE TIME WAS SPENT — across service boundaries.

  When a request flows:
    client → api-gateway → user-service → PostgreSQL

  Each hop generates a *span*. The OpenTelemetry SDK propagates a trace ID
  in HTTP headers (W3C Trace Context: 'traceparent'). Every service reads it
  and appends its own spans to the same trace tree. The result in Jaeger:

    ┌─────────────────────────────────────── trace: f47ac10b ──────┐
    │ api-gateway  POST /api/v1/auth/login                142ms    │
    │   └─ user-service  POST /api/v1/auth/login          131ms   │
    │        ├─ sqlalchemy  SELECT users WHERE email=?     18ms   │
    │        └─ sqlalchemy  UPDATE users SET last_login    12ms   │
    └──────────────────────────────────────────────────────────────┘

  Without tracing you'd see two log lines and guess at causality.
  With tracing you can see the exact SQL statement that took 18ms.

ARCHITECTURE:
  Each service exports spans to a local OTEL Collector (docker-compose)
  or Jaeger (kubernetes/monitoring). In production, the collector can fan-out
  to Jaeger, Azure Monitor, and Datadog simultaneously — change destination
  without touching application code.

  This file is kept short intentionally: it only wires SDK → exporter.
  Actual instrumentation is automatic via FastAPIInstrumentor and
  SQLAlchemyInstrumentor — zero manual span creation needed for HTTP + DB.

ENVIRONMENT VARIABLES:
  OTEL_SERVICE_NAME              — service name that appears in Jaeger UI
  OTEL_EXPORTER_OTLP_ENDPOINT   — collector URL (default: http://otel-collector:4317)
  OTEL_TRACES_SAMPLER            — 'always_on' (dev) or 'parentbased_traceidratio' (prod)
  OTEL_TRACES_SAMPLER_ARG        — sample rate 0.0–1.0 (used with traceidratio)

  If OTEL_EXPORTER_OTLP_ENDPOINT is empty or 'disabled', tracing is a no-op.
  This lets local development run without a Jaeger instance.
"""
import logging
import os

logger = logging.getLogger(__name__)


def init_telemetry(
    app,
    service_name: str,
    db_engine=None,
    instrument_httpx: bool = False,
) -> None:
    """
    Initialise OpenTelemetry SDK and instrument the given FastAPI app.

    Args:
        app:              The FastAPI application instance.
        service_name:     Human-readable name shown in Jaeger (e.g. 'api-gateway').
        db_engine:        SQLAlchemy async engine — enables DB query tracing when set.
        instrument_httpx: If True, instrument httpx for outbound request tracing.
                          Enable this only in the api-gateway which makes HTTP calls.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint or endpoint.lower() == "disabled":
        logger.info("OpenTelemetry: OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing disabled")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Instrument FastAPI — creates spans for every HTTP request automatically.
        # Span includes: method, route template, status code, server host/port.
        FastAPIInstrumentor.instrument_app(app)
        logger.info("OpenTelemetry: FastAPI instrumented for service '%s'", service_name)

        # Instrument SQLAlchemy — creates child spans per DB query.
        # Span includes: statement text (sanitised), table name, operation (SELECT/INSERT).
        # Seeing this in Jaeger reveals N+1 queries, missing indexes, and slow joins.
        if db_engine is not None:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument(engine=db_engine.sync_engine)
            logger.info("OpenTelemetry: SQLAlchemy instrumented")

        # Instrument httpx — creates child spans for every outbound HTTP call.
        # In the gateway, this shows exactly how long each upstream service took.
        if instrument_httpx:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            HTTPXClientInstrumentor().instrument()
            logger.info("OpenTelemetry: httpx instrumented")

        logger.info(
            "OpenTelemetry: exporting spans → %s (service: %s)",
            endpoint, service_name,
        )

    except ImportError as exc:
        # Don't crash the service if OTel packages aren't installed.
        # This makes it easy to run locally without the full dep tree.
        logger.warning(
            "OpenTelemetry packages not installed (%s) — tracing disabled. "
            "Install opentelemetry-sdk and opentelemetry-exporter-otlp-proto-grpc.",
            exc,
        )
    except Exception as exc:
        logger.error("OpenTelemetry init failed: %s — tracing disabled", exc)
