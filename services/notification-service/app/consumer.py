"""
notification-service/app/consumer.py

Service Bus consumer — the heart of the notification service.

Implements the Competing Consumers pattern: multiple instances of this
service can run simultaneously; Service Bus ensures each message is
delivered to only ONE consumer at a time (via message locking).

USER TARGETING:
  Every event payload carries IDs for the users who should be notified.
  The consumer extracts these IDs and passes them as target_user_ids to
  broadcaster.broadcast(), so only the relevant users receive the event.

  Event → recipients mapping:
    task.created        → assignee_id (new assignee), creator_id (confirmation)
    task.status_changed → creator_id, assignee_id (whoever is present)
    task.deleted        → creator_id, assignee_id (whoever is present)

  This is a business-logic decision: the consumer knows the domain rules for
  who cares about each event type. The broadcaster is just a delivery mechanism
  and has no knowledge of event semantics.
"""
import json
import logging
import asyncio
from typing import Any

from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusReceivedMessage
from prometheus_client import Counter

from app.notifiers.logger import LogNotifier
from app.broadcaster import broadcaster

logger = logging.getLogger(__name__)


def _collect_targets(*ids: str | None) -> list[str]:
    """
    Collect non-null user ID strings into a deduplicated list.

    Args:
        *ids: Any number of nullable user ID strings from event payloads.

    Returns:
        Deduplicated list of non-empty user ID strings, preserving first-seen
        order. Order is stable so tests can assert on exact recipient lists.
    """
    seen: set[str] = set()
    result: list[str] = []
    for uid in ids:
        if uid and uid not in seen:
            seen.add(uid)
            result.append(uid)
    return result


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

        # Open/Closed principle: add new event types here without touching
        # _process_message(). Each handler returns the list of target user IDs.
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
            # return_exceptions=True: one failing receiver doesn't kill the others.
            results = await asyncio.gather(
                *[self._receive_from_topic(client, topic) for topic in self.topics],
                return_exceptions=True,
            )
            for topic, result in zip(self.topics, results):
                if isinstance(result, Exception):
                    logger.error("Receiver for topic '%s' failed: %s", topic, result)

    async def _receive_from_topic(self, client: ServiceBusClient, topic: str) -> None:
        async with client.get_subscription_receiver(
            topic_name=topic,
            subscription_name=self.subscription_name,
            max_wait_time=5,
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
            target_user_ids: list[str] = []
            if handler:
                target_user_ids = await handler(data)
            else:
                logger.warning("No handler for event_type: %s", event_type)

            # Acknowledge BEFORE broadcasting. If broadcast fails, the message
            # is already committed — we do not re-deliver. SSE is best-effort;
            # Service Bus provides durable storage, SSE provides real-time UX.
            await receiver.complete_message(message)
            self.metrics.labels(event_type=event_type, status="success").inc()

            # Broadcast to the users who should receive this event.
            await broadcaster.broadcast(event_type, data, target_user_ids)

        except Exception as exc:
            logger.error("Failed to process message %s: %s", event_type, exc)
            # Abandon = release the lock so Service Bus can retry delivery.
            await receiver.abandon_message(message)
            self.metrics.labels(event_type=event_type, status="failure").inc()

    # ─── Event Handlers ───────────────────────────────────────────────────────
    # Each handler performs domain logic and returns the list of user IDs that
    # should receive the event via SSE.

    async def _on_task_created(self, data: dict) -> list[str]:
        """
        Notify the assignee that a task was created for them, and confirm to
        the creator that the task was successfully published.

        Recipients: assignee (if present) + creator (always).
        """
        assignee_id = data.get("assignee_id")
        creator_id = data.get("creator_id")

        if assignee_id:
            # SECURITY: Sanitize before including in log messages / notifications.
            # A crafted task title with newlines or ANSI codes corrupts log output.
            raw_title = str(data.get("title", ""))
            safe_title = raw_title.replace("\n", " ").replace("\r", " ")[:200]
            await self.notifier.send(
                recipient_id=str(assignee_id),
                subject="You have been assigned a new task",
                body=f"Task '{safe_title}' has been assigned to you.",
            )

        return _collect_targets(assignee_id, creator_id)

    async def _on_task_status_changed(self, data: dict) -> list[str]:
        """
        Notify all stakeholders (creator + assignee) when a task's status
        changes. Both parties care: the assignee is doing the work, the
        creator is waiting for it.

        Recipients: creator (always) + assignee (if present).
        """
        logger.info(
            "Task %s status: %s → %s",
            data.get("task_id"),
            data.get("old_status"),
            data.get("new_status"),
        )
        return _collect_targets(data.get("creator_id"), data.get("assignee_id"))

    async def _on_task_deleted(self, data: dict) -> list[str]:
        """
        Notify the creator and assignee when a task is deleted.

        Recipients: creator (always) + assignee (if present).
        """
        logger.info("Task %s was deleted", data.get("task_id"))
        return _collect_targets(data.get("creator_id"), data.get("assignee_id"))
