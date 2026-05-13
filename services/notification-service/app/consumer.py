"""
Service Bus consumer — the heart of the notification service.

Implements the Competing Consumers pattern: multiple instances of this
service can run simultaneously; Service Bus ensures each message is
delivered to only ONE consumer at a time (via message locking).
"""
import json
import logging
import asyncio
from typing import Any

from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusReceivedMessage
from prometheus_client import Counter

from app.notifiers.logger import LogNotifier

logger = logging.getLogger(__name__)


class ServiceBusConsumer:
    """Polls Azure Service Bus topics and dispatches events to notifiers."""

    def __init__(
        self,
        connection_string: str,
        topics: list[str],
        subscription_name: str,
        metrics_counter: Counter,
    ) -> None:
        self.connection_string = connection_string
        self.topics = topics
        self.subscription_name = subscription_name
        self.metrics = metrics_counter
        self.notifier = LogNotifier()

        # Event type → handler method map (Open/Closed principle: add new
        # handlers without modifying the dispatch logic)
        self._handlers: dict[str, Any] = {
            "task.created":        self._on_task_created,
            "task.status_changed": self._on_task_status_changed,
            "task.deleted":        self._on_task_deleted,
        }

    async def run(self) -> None:
        """Main consumer loop — runs forever until cancelled."""
        logger.info("Consumer starting for topics: %s", self.topics)
        while True:
            try:
                await self._consume_all_topics()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Consumer error, restarting in 5s: %s", exc)
                await asyncio.sleep(5)

    async def _consume_all_topics(self) -> None:
        async with ServiceBusClient.from_connection_string(self.connection_string) as client:
            # Run one receiver per topic concurrently
            await asyncio.gather(
                *[self._receive_from_topic(client, topic) for topic in self.topics]
            )

    async def _receive_from_topic(self, client: ServiceBusClient, topic: str) -> None:
        async with client.get_subscription_receiver(
            topic_name=topic,
            subscription_name=self.subscription_name,
            max_wait_time=5,   # seconds to wait for messages
        ) as receiver:
            async for message in receiver:
                await self._process_message(message, receiver)

    async def _process_message(
        self, message: ServiceBusReceivedMessage, receiver: Any
    ) -> None:
        event_type = "unknown"
        try:
            body = json.loads(b"".join(message.body))
            event_type = body.get("event_type", "unknown")
            data = body.get("data", {})

            handler = self._handlers.get(event_type)
            if handler:
                await handler(data)
            else:
                logger.warning("No handler for event_type: %s", event_type)

            # Complete = acknowledge the message (remove from queue)
            await receiver.complete_message(message)
            self.metrics.labels(event_type=event_type, status="success").inc()

        except Exception as exc:
            logger.error("Failed to process message %s: %s", event_type, exc)
            # Abandon = release the lock so Service Bus can retry delivery
            await receiver.abandon_message(message)
            self.metrics.labels(event_type=event_type, status="failure").inc()

    # ─── Event Handlers ──────────────────────────────────────────────────────
    async def _on_task_created(self, data: dict) -> None:
        assignee_id = data.get("assignee_id")
        if assignee_id:
            await self.notifier.send(
                recipient_id=assignee_id,
                subject="You have been assigned a new task",
                body=f"Task '{data.get('title')}' has been assigned to you.",
            )

    async def _on_task_status_changed(self, data: dict) -> None:
        logger.info(
            "Task %s status: %s → %s",
            data.get("task_id"),
            data.get("old_status"),
            data.get("new_status"),
        )

    async def _on_task_deleted(self, data: dict) -> None:
        logger.info("Task %s was deleted", data.get("task_id"))
