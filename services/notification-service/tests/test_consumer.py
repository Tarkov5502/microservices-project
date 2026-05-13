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
  8. Each handler returns the correct target_user_ids list (user-scoped SSE).
  9. _process_message calls broadcaster.broadcast with the returned target IDs.
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
    assert "\r" not in body
    # The "X-Injected" / "evil-header" tokens survive as inert text — that's
    # acceptable. The threat we guard against is the CR/LF that would create
    # a new log line; we don't pretend to be an HTML sanitiser.


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


# ─── target_user_ids: user-scoped delivery ────────────────────────────────────
# Each handler must return the correct list of user IDs that should receive
# the event via SSE. If the list is wrong, events go to the wrong users
# (privacy violation) or the right users miss them (functional bug).


@pytest.mark.asyncio
async def test_task_created_returns_both_assignee_and_creator():
    """
    task.created → [assignee_id, creator_id]: the assige learns about the
    assignment; the creator gets confirmation the task was created.
    """
    consumer = _make_consumer()
    consumer.notifier = AsyncMock()
    data = {
        "task_id": "t-1",
        "title": "Do the thing",
        "assignee_id": "user-assignee",
        "creator_id": "user-creator",
    }
    targets = await consumer._on_task_created(data)
    assert "user-assignee" in targets
    assert "user-creator" in targets


@pytest.mark.asyncio
async def test_task_created_returns_only_creator_when_no_assignee():
    """
    No assignee → only creator receives the event (their own confirmation).
    """
    consumer = _make_consumer()
    consumer.notifier = AsyncMock()
    data = {
        "task_id": "t-2",
        "title": "Unassigned",
        "creator_id": "user-creator",
    }
    targets = await consumer._on_task_created(data)
    assert targets == ["user-creator"]


@pytest.mark.asyncio
async def test_task_created_deduplicates_when_creator_is_assignee():
    """
    Creator assigns the task to themselves → only one entry in target list.
    Duplicate IDs would result in duplicate SSE events on the same connection.
    """
    consumer = _make_consumer()
    consumer.notifier = AsyncMock()
    data = {
        "task_id": "t-3",
        "title": "Self-assigned",
        "assignee_id": "user-alice",
        "creator_id": "user-alice",
    }
    targets = await consumer._on_task_created(data)
    assert targets == ["user-alice"], "Should deduplicate when creator == assignee"


@pytest.mark.asyncio
async def test_task_status_changed_returns_creator_and_assignee():
    """
    Status changes notify both the creator (watching progress) and the
    assignee (doing the work).
    """
    consumer = _make_consumer()
    data = {
        "task_id": "t-4",
        "old_status": "todo",
        "new_status": "in_progress",
        "creator_id": "user-creator",
        "assignee_id": "user-assignee",
    }
    targets = await consumer._on_task_status_changed(data)
    assert "user-creator" in targets
    assert "user-assignee" in targets


@pytest.mark.asyncio
async def test_task_status_changed_without_assignee_returns_only_creator():
    consumer = _make_consumer()
    data = {
        "task_id": "t-5",
        "old_status": "todo",
        "new_status": "done",
        "creator_id": "user-creator",
    }
    targets = await consumer._on_task_status_changed(data)
    assert targets == ["user-creator"]


@pytest.mark.asyncio
async def test_task_deleted_returns_creator_and_assignee():
    consumer = _make_consumer()
    data = {
        "task_id": "t-6",
        "creator_id": "user-creator",
        "assignee_id": "user-assignee",
    }
    targets = await consumer._on_task_deleted(data)
    assert "user-creator" in targets
    assert "user-assignee" in targets


@pytest.mark.asyncio
async def test_process_message_calls_broadcast_with_target_ids():
    """
    _process_message must pass target_user_ids returned by the handler to
    broadcaster.broadcast(). If it passes an empty list or the wrong IDs,
    events are silently swallowed or sent to wrong users.
    """
    consumer = _make_consumer()
    consumer.notifier = AsyncMock()
    mock_receiver = AsyncMock()

    raw_body = json.dumps({
        "event_type": "task.created",
        "data": {
            "task_id": "t-7",
            "title": "Test",
            "assignee_id": "user-assignee",
            "creator_id": "user-creator",
        },
    }).encode()
    message = MagicMock()
    message.body = iter([raw_body])

    with patch("app.consumer.broadcaster") as mock_broadcaster:
        mock_broadcaster.broadcast = AsyncMock()
        await consumer._process_message(message, mock_receiver)

    mock_broadcaster.broadcast.assert_awaited_once()
    broadcast_call = mock_broadcaster.broadcast.call_args
    target_ids = broadcast_call.kwargs.get("target_user_ids") or broadcast_call.args[2]
    assert "user-assignee" in target_ids
    assert "user-creator" in target_ids
