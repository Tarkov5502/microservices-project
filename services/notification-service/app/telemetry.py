"""
notification-service/app/telemetry.py — OpenTelemetry distributed tracing setup.

For notification-service, tracing is most useful for correlating event
processing with the originating request. The traceparent header is embedded
in Service Bus messages so spans from the consumer are linked to the
original trace that triggered the event.

Without this link, you'd see the notification-service span as a completely
separate trace — with no connection to the API request that caused it.
"""
import logging
import os

logger = logging.getLogger(__name__)


def init_telemetry(app, service_name: str) -> None:
    """
    Initialise OpenTelemetry for notification-service.
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
        logger.info("OpenTelemetry: exporting to %s", endpoint)

    except ImportError as exc:
        logger.warning("OpenTelemetry packages not installed (%s) — tracing disabled", exc)
    except Exception as exc:
        logger.error("OpenTelemetry init failed: %s", exc)
