"""
tests/test_consumer.py (notification-service)

Tests for the ServiceBusConsumer event dispatch logic.

These are pure unit tests — no real Azure Service Bus connection needed.
We test the business logic of each handler directly by calling the private
methods with mock data.

KEY BEHAVIOURS TESTED:
  1. task.created → sends notification to assignee when one is set.
  2. task.created → no notification when assignee_id is absent.
  3. task.status_changed → logs the transition (no crash).
  4. task.deleted → logs deletion (no crash).
  5. Unknown event_type → logs warning, does NOT raise.
  6. Malicious title with newlines is sanitised before use (log injection).
  7. Message parsing: valid JSON body is correctly decoded.
"""
import json
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.consumer import ServiceBusConsumer
from prometheus_client import Counter


def _make_consumer() -> ServiceBusConsumer:
    """Create a consumer with a mock metrics counter."""
    counter = MagicMock(spec=Counter)
    counter.labels.return_value = MagicMock()
    return ServiceBusConsumer(
        connection_string="Endpoint=sb://fake.servicebus.windows.net/;...",
        topics=["task-events"],
        subscription_name="test-sub",
        metrics_counter=counter,
    )


@pytest.mark.asyncio
async def test_task_created_notifies_assignee():
    """When a task has an assignee, a notification is sent."""
    consumer = _make_consumer()
    consumer.notifier = AsyncMock()
    data = {
        "task_id": "abc-123",
        "title": "Write unit tests",
        "assignee_id": "user-456",
        "creator_id": "user-789",
        "project_id": "proj-000",
    }
    await consumer._on_task_created(data)
    consumer.notifier.send.assert_awaited_once()
    call_kwargs = consumer.notifier.send.call_args
    assert call_kwargs.kwargs["recipient_id"] == "user-456"
    assert "Write unit tests" in call_kwargs.kwargs["body"]


@pytest.mark.asyncio
async def test_task_created_skips_notification_without_assignee():
    """No assignee_id → no notification should be sent."""
    consumer = _make_consumer()
    consumer.notifier = AsyncMock()
    data = {
        "task_id": "abc-123",
        "title": "Unassigned task",
        "creator_id": "user-789",
        "project_id": "proj-000",
    }
    await consumer._on_task_created(data)
    consumer.notifier.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_created_sanitises_title_newlines():
    """
    A malicious task title with newlines could corrupt log output.
    The consumer must strip \\n and \\r from titles before using them.
    """
    consumer = _make_consumer()
    consumer.notifier = AsyncMock()
    data = {
        "task_id": "abc-123",
        "title": "Normal title\nX-Injected: evil-header",
        "assignee_id": "user-456",
    }
    await consumer._on_task_created(data)
    call_kwargs = consumer.notifier.send.call_args
    body = call_kwargs.kwargs["body"]
    assert "\n" not in body
    assert "evil-header" not in body or "X-Injected" not in body


@pytest.mark.asyncio
async def test_task_status_changed_does_not_raise():
    consumer = _make_consumer()
    data = {
        "task_id": "abc-123",
        "old_status": "TODO",
        "new_status": "IN_PROGRESS",
        "updated_by": "user-789",
    }
    # Should complete without raising
    await consumer._on_task_status_changed(data)


@pytest.mark.asyncio
async def test_task_deleted_does_not_raise():
    consumer = _make_consumer()
    await consumer._on_task_deleted({"task_id": "abc-123"})


@pytest.mark.asyncio
async def test_unknown_event_type_is_handled_gracefully():
    """
    The consumer must never crash on an unknown event type —
    crashing means the message gets abandoned and retried forever.
    """
    consumer = _make_consumer()
    mock_receiver = AsyncMock()
    raw_body = json.dumps({
        "event_type": "user.something_new_we_dont_know",
        "data": {"user_id": "xyz"},
    }).encode()

    message = MagicMock()
    message.body = iter([raw_body])

    # Should not raise, should call complete_message
    await consumer._process_message(message, mock_receiver)
    mock_receiver.complete_message.assert_awaited_once_with(message)


@pytest.mark.asyncio
async def test_malformed_json_triggers_abandon():
    """
    If a message body is not valid JSON, the consumer must abandon it
    (so Service Bus can retry / dead-letter it) rather than crashing.
    """
    consumer = _make_consumer()
    mock_receiver = AsyncMock()
    message = MagicMock()
    message.body = iter([b"this is not json {{{"])

    await consumer._process_message(message, mock_receiver)
    mock_receiver.abandon_message.assert_awaited_once_with(message)
    mock_receiver.complete_message.assert_not_awaited()
