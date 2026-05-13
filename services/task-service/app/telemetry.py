"""
task-service/app/telemetry.py — OpenTelemetry distributed tracing setup.

See api-gateway/app/telemetry.py for the full explanation of WHY tracing
matters and how the trace propagation works across services.

For task-service specifically, SQLAlchemy instrumentation is the most
valuable piece — it surfaces individual SQL queries as child spans under
the FastAPI route span, making it immediately obvious when a slow query
or N+1 problem is causing latency.

Example trace in Jaeger for 'GET /api/v1/tasks':
  api-gateway  GET /api/v1/tasks                    89ms
    └─ task-service  GET /api/v1/tasks              81ms
         ├─ sqlalchemy  SELECT tasks WHERE ...       12ms
         └─ sqlalchemy  (no second query)
"""
import logging
import os

logger = logging.getLogger(__name__)


def init_telemetry(app, service_name: str, db_engine=None) -> None:
    """
    Initialise OpenTelemetry for task-service.
    No-op if OTEL_EXPORTER_OTLP_ENDPOINT is not set.
    """
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint or endpoint.lower() == "disabled":
        logger.info("OpenTelemetry: tracing disabled (OTEL_EXPORTER_OTLP_ENDPOINT not set)")
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

        FastAPIInstrumentor.instrument_app(app)
        logger.info("OpenTelemetry: FastAPI instrumented for '%s'", service_name)

        if db_engine is not None:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            SQLAlchemyInstrumentor().instrument(engine=db_engine.sync_engine)
            logger.info("OpenTelemetry: SQLAlchemy instrumented")

        logger.info("OpenTelemetry: exporting to %s", endpoint)

    except ImportError as exc:
        logger.warning("OpenTelemetry packages not installed (%s) — tracing disabled", exc)
    except Exception as exc:
        logger.error("OpenTelemetry init failed: %s", exc)
