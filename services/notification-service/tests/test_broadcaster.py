"""
tests/test_broadcaster.py (notification-service)

Unit tests for the user-scoped _Broadcaster.

THE BUG THESE TESTS GUARD AGAINST:
  The original broadcaster sent every event to every connected SSE client.
  These tests verify that events are delivered ONLY to the intended user(s),
  and that other users' connections never receive them.

KEY BEHAVIOURS TESTED:
  1. broadcast() with target_user_ids delivers only to those users.
  2. broadcast() with empty target_user_ids is a silent no-op.
  3. A user not currently connected receives no error — event is silently skipped.
  4. A single user with multiple open connections receives the event on all of them.
  5. subscribe() cleans up its queue on disconnect (CancelledError / GeneratorExit).
  6. active_user_count() and active_connection_count() track correctly.
  7. Two simultaneous users do not receive each other's events.
  8. _collect_targets deduplicates and preserves order.

APPROACH:
  We use asyncio.Task to drive the subscribe() generator concurrently with
  broadcast() calls. This tests the real async fan-out path without any mocks.
"""
import asyncio
import json
import pytest
import pytest_asyncio

from app.broadcaster import _Broadcaster
from app.consumer import _collect_targets


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _collect_n_events(broadcaster: _Broadcaster, user_id: str, n: int) -> list[dict]:
    """
    Subscribe as user_id, collect exactly n real events (skip keepalives),
    then cancel the subscription. Returns parsed event dicts.
    """
    events: list[dict] = []
    async for chunk in broadcaster.subscribe(user_id=user_id):
        if chunk.startswith(": keepalive"):
            continue
        # SSE format: "data: <json>\n\n"
        payload_str = chunk.removeprefix("data: ").strip()
        events.append(json.loads(payload_str))
        if len(events) >= n:
            break
    return events


# ─── _collect_targets ─────────────────────────────────────────────────────────

class TestCollectTargets:
    """Pure unit tests — no async needed."""

    def test_returns_empty_list_for_all_none(self):
        assert _collect_targets(None, None) == []

    def test_filters_out_none(self):
        assert _collect_targets("user-a", None) == ["user-a"]

    def test_deduplicates_identical_ids(self):
        """creator_id == assignee_id → only one entry."""
        assert _collect_targets("user-a", "user-a") == ["user-a"]

    def test_preserves_order_first_seen(self):
        result = _collect_targets("user-b", "user-a")
        assert result == ["user-b", "user-a"]

    def test_empty_string_is_filtered(self):
        """Empty strings are falsy and should be excluded."""
        assert _collect_targets("", "user-a") == ["user-a"]

    def test_multiple_distinct_ids_all_included(self):
        result = _collect_targets("alice", "bob", "carol")
        assert result == ["alice", "bob", "carol"]


# ─── _Broadcaster ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestBroadcaster:

    async def test_broadcast_delivers_to_subscribed_user(self):
        """The subscribed user receives the event."""
        b = _Broadcaster()
        task = asyncio.create_task(
            _collect_n_events(b, user_id="alice", n=1)
        )
        # Give the subscription a moment to register
        await asyncio.sleep(0)

        await b.broadcast("task.created", {"task_id": "t1"}, target_user_ids=["alice"])
        events = await asyncio.wait_for(task, timeout=2.0)

        assert len(events) == 1
        assert events[0]["event_type"] == "task.created"
        assert events[0]["data"]["task_id"] == "t1"

    async def test_broadcast_does_not_deliver_to_other_user(self):
        """
        Events targeted at alice must NOT arrive in bob's stream.
        This is the primary regression test for the privacy bug.
        """
        b = _Broadcaster()

        alice_events: list[dict] = []
        bob_events: list[dict] = []

        async def collect_alice():
            async for chunk in b.subscribe(user_id="alice"):
                if chunk.startswith(": keepalive"):
                    continue
                alice_events.append(json.loads(chunk.removeprefix("data: ").strip()))
                break  # Stop after first event

        async def collect_bob():
            # Bob subscribes but we'll check he gets nothing within the window
            try:
                async for chunk in b.subscribe(user_id="bob"):
                    if chunk.startswith(": keepalive"):
                        continue
                    bob_events.append(json.loads(chunk.removeprefix("data: ").strip()))
                    break
            except asyncio.CancelledError:
                pass

        alice_task = asyncio.create_task(collect_alice())
        bob_task = asyncio.create_task(collect_bob())
        await asyncio.sleep(0)  # Let both subscribe

        # Send event ONLY to alice
        await b.broadcast("task.created", {"for": "alice"}, target_user_ids=["alice"])

        await asyncio.wait_for(alice_task, timeout=2.0)
        bob_task.cancel()
        try:
            await bob_task
        except asyncio.CancelledError:
            pass

        assert len(alice_events) == 1
        assert len(bob_events) == 0, (
            "Bob must not receive events targeted at Alice. "
            "This was the privacy bug in the original implementation."
        )

    async def test_broadcast_with_empty_targets_is_noop(self):
        """Empty target_user_ids → no delivery, no error."""
        b = _Broadcaster()
        # No subscribers registered — this must not raise
        await b.broadcast("task.created", {"x": 1}, target_user_ids=[])
        assert b.active_connection_count() == 0

    async def test_broadcast_to_unconnected_user_is_silent(self):
        """Broadcasting to a user with no active connections must not raise."""
        b = _Broadcaster()
        # nobody is subscribed
        await b.broadcast("task.created", {}, target_user_ids=["nonexistent-user"])
        # Should complete without exception

    async def test_single_user_multiple_connections_receives_on_all(self):
        """
        Same user with two browser tabs (two connections) must receive the
        event on BOTH connections — one user, multiple queues.
        """
        b = _Broadcaster()

        results: list[list[dict]] = [[], []]

        async def collect(idx: int):
            async for chunk in b.subscribe(user_id="alice"):
                if chunk.startswith(": keepalive"):
                    continue
                results[idx].append(json.loads(chunk.removeprefix("data: ").strip()))
                break

        t0 = asyncio.create_task(collect(0))
        t1 = asyncio.create_task(collect(1))
        await asyncio.sleep(0)

        assert b.active_connection_count() == 2

        await b.broadcast("task.created", {"msg": "hello"}, target_user_ids=["alice"])

        await asyncio.wait_for(asyncio.gather(t0, t1), timeout=2.0)

        assert len(results[0]) == 1, "First connection should receive the event"
        assert len(results[1]) == 1, "Second connection should also receive the event"

    async def test_subscribe_cleans_up_on_cancellation(self):
        """
        When an SSE connection is cancelled (client disconnects), the queue
        must be removed from the broadcaster. Memory must not leak.
        """
        b = _Broadcaster()

        async def long_running():
            async for _ in b.subscribe(user_id="alice"):
                pass  # Never exits normally — waits forever

        task = asyncio.create_task(long_running())
        await asyncio.sleep(0)  # Let it register

        assert b.active_connection_count() == 1

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert b.active_connection_count() == 0, (
            "Cancelled connection must be removed from the broadcaster. "
            "If it stays, every disconnect leaks a queue entry."
        )

    async def test_active_user_count_tracks_correctly(self):
        b = _Broadcaster()
        assert b.active_user_count() == 0

        async def subscribe_and_wait():
            async for _ in b.subscribe(user_id="alice"):
                break  # yield one keepalive then stop

        # Before subscribe
        assert b.active_user_count() == 0

        task = asyncio.create_task(subscribe_and_wait())
        await asyncio.sleep(0)
        assert b.active_user_count() == 1

        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass

        # After disconnect
        assert b.active_user_count() == 0

    async def test_broadcast_to_multiple_users_simultaneously(self):
        """
        An event targeted at [alice, bob] must be delivered to both.
        """
        b = _Broadcaster()

        alice_task = asyncio.create_task(_collect_n_events(b, "alice", 1))
        bob_task = asyncio.create_task(_collect_n_events(b, "bob", 1))
        await asyncio.sleep(0)

        await b.broadcast(
            "task.status_changed",
            {"task_id": "t1", "status": "done"},
            target_user_ids=["alice", "bob"],
        )

        alice_events, bob_events = await asyncio.wait_for(
            asyncio.gather(alice_task, bob_task), timeout=2.0
        )

        assert len(alice_events) == 1
        assert len(bob_events) == 1
        assert alice_events[0]["event_type"] == "task.status_changed"
        assert bob_events[0]["event_type"] == "task.status_changed"

    async def test_sse_payload_format_is_correct(self):
        """
        The SSE line format must be "data: <json>\\n\\n" for real events.
        Clients depend on this exact format.
        """
        b = _Broadcaster()

        raw_chunks: list[str] = []

        async def collect_raw():
            async for chunk in b.subscribe(user_id="alice"):
                if chunk.startswith(": keepalive"):
                    continue
                raw_chunks.append(chunk)
                break

        task = asyncio.create_task(collect_raw())
        await asyncio.sleep(0)

        await b.broadcast("task.deleted", {"task_id": "t99"}, target_user_ids=["alice"])
        await asyncio.wait_for(task, timeout=2.0)

        assert len(raw_chunks) == 1
        chunk = raw_chunks[0]
        assert chunk.startswith("data: "), f"SSE line must start with 'data: ', got: {chunk!r}"
        assert chunk.endswith("\n\n"), f"SSE line must end with '\\n\\n', got: {chunk!r}"
        payload = json.loads(chunk[len("data: "):].strip())
        assert payload["event_type"] == "task.deleted"
