"""
notification-service/app/main.py

An event-driven service that listens to Azure Service Bus topics and
reacts to domain events (task created, status changed, user registered).

This demonstrates the Consumer pattern — it processes messages asynchronously,
completely decoupled from the services that produce those events.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST

from app.config import settings
from app.consumer import ServiceBusConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

EVENTS_PROCESSED = Counter(
    "notification_events_processed_total",
    "Total Service Bus events processed",
    ["event_type", "status"],
)

# Background consumer task handle — stored so we can cancel it on shutdown
_consumer_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _consumer_task
    logger.info("Notification Service starting — launching Service Bus consumer...")

    consumer = ServiceBusConsumer(
        connection_string=settings.servicebus_connection_string,
        topics=[settings.servicebus_topic_tasks, settings.servicebus_topic_users],
        subscription_name=settings.servicebus_subscription_name,
        metrics_counter=EVENTS_PROCESSED,
    )

    # Run the consumer loop as a background asyncio task.
    # This lets FastAPI serve /health + /metrics while consuming messages.
    _consumer_task = asyncio.create_task(consumer.run(), name="servicebus-consumer")
    logger.info("Service Bus consumer started")
    yield

    logger.info("Shutting down consumer...")
    if _consumer_task and not _consumer_task.done():
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass
    logger.info("Notification Service stopped")


app = FastAPI(
    title="Notification Service",
    version="1.0.0",
    docs_url="/docs" if settings.environment != "production" else None,
    lifespan=lifespan,
)


@app.get("/health")
async def liveness() -> dict:
    return {"status": "ok", "service": "notification-service"}


@app.get("/health/ready")
async def readiness() -> dict:
    """
    SECURITY: Never expose raw exception messages to HTTP callers — they may
    contain internal hostnames, connection strings, or stack traces that aid
    infrastructure reconnaissance. Log internally, return generic status.

    BUG FIX: _consumer_task.exception() raises asyncio.CancelledError if the
    task was cancelled (e.g. during graceful shutdown). That would propagate as
    an unhandled 500. We catch it separately and treat cancellation as healthy.
    """
    if _consumer_task is None:
        return {"status": "starting", "consumer": "not_yet_created"}
    if _consumer_task.done():
        try:
            exc = _consumer_task.exception()
        except asyncio.CancelledError:
            # Task was cancelled cleanly — this is expected during shutdown
            return {"status": "stopping", "consumer": "cancelled"}
        if exc is not None:
            logger.error("Consumer task has crashed: %s", exc)
            from fastapi import HTTPException, status as http_status
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Message consumer is unavailable",
            )
    return {"status": "ready", "consumer": "running"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
