"""
user-service/app/telemetry.py — OpenTelemetry distributed tracing setup.

Identical pattern to task-service. See api-gateway/app/telemetry.py for
the full rationale.

For user-service, the most valuable spans are:
  - bcrypt hashing on login/register (~100ms — visible in trace, expected)
  - SELECT users WHERE email=? (should be <5ms with proper index)
  - UPDATE users SET last_login_at (should be <5ms)

Seeing bcrypt as a span immediately explains why login is always "slow"
(by design — it's the cost of secure password hashing).
"""
import logging
import os

logger = logging.getLogger(__name__)


def init_telemetry(app, service_name: str, db_engine=None) -> None:
    """
    Initialise OpenTelemetry for user-service.
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
