"""
notification-service/app/broadcaster.py — User-scoped in-memory SSE broadcaster.

WHY THIS WAS REWRITTEN:
  The original implementation had a single flat pool of queues shared across
  all connected clients. Every event was sent to every client regardless of
  who it was intended for. That broke two things:

    1. Privacy — User A received task notifications intended for User B.
    2. Correctness — The entire point of targeted notifications is that the
       right person hears about the right event. All-or-nothing broadcast
       is just spam.

NEW ARCHITECTURE:
  Queues are now organised by user_id, not a global client counter.

    _queues: dict[user_id: str, dict[conn_id: int, Queue]]

  A single user can have multiple open connections (e.g., two browser tabs).
  Each connection gets its own queue, but they share the same user_id bucket.

  broadcast(event_type, data, target_user_ids=[...]) pushes the event only
  into the queues belonging to the listed user IDs. This means the consumer
  must know WHO should receive each event — which it does, because event
  payloads carry creator_id, assignee_id, etc.

INTERFACE CONTRACT (unchanged for callers):
  broadcaster.broadcast(event_type, data, target_user_ids=[...])
  broadcaster.subscribe(user_id)   → async generator of SSE-formatted strings

KNOWN LIMITATION — Multi-pod fan-out:
  Same as before: this works for single-replica or well-pinned clients.
  For multi-replica fan-out replace _queues with Redis Pub/Sub channels;
  broadcast() publishes to a channel, subscribe() listens on it.
  The interface here is designed so that swap-out is isolated to this
  module only — callers (consumer.py, stream.py) are unaffected.
"""
import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

# Maximum events buffered per connection before we start dropping.
# Prevents a slow client from consuming unbounded memory.
_QUEUE_MAXSIZE = 100


class _Broadcaster:
    """
    Fan-out in-memory event bus, scoped per user.

    Thread safety: asyncio.Queue is designed for single-threaded async use.
    All operations MUST run in the same event loop.
    """

    def __init__(self) -> None:
        # user_id → {conn_id → Queue[str]}
        # defaultdict so we never need to check "does this user exist?"
        self._queues: dict[str, dict[int, asyncio.Queue]] = defaultdict(dict)
        self._next_conn_id = 0

    def _new_conn_id(self) -> int:
        self._next_conn_id += 1
        return self._next_conn_id

    # ─── Broadcast ────────────────────────────────────────────────────────────

    async def broadcast(
        self,
        event_type: str,
        data: dict,
        target_user_ids: list[str],
    ) -> None:
        """
        Push an event to every open connection belonging to each user in
        target_user_ids.

        Rules:
          - If target_user_ids is empty, the event is silently discarded.
            This is intentional: an event with no targets is a no-op, not
            a reason to spam every connected client.
          - Never raises. A broadcast failure must never crash the consumer.
          - Stale connections (queues that are full) are dropped for that
            specific event; they remain registered until subscribe() exits.

        Args:
            event_type:      The event name, e.g. "task.created".
            data:            The event payload dict. Must be JSON-serialisable.
            target_user_ids: Explicit list of user IDs who should receive this
                             event. Caller is responsible for deciding who
                             the recipients are (assignee, creator, etc.).
        """
        if not target_user_ids:
            logger.debug("broadcast: no target_user_ids for '%s' — discarding", event_type)
            return

        payload = json.dumps({"event_type": event_type, "data": data})
        total_delivered = 0
        total_dropped = 0

        for user_id in target_user_ids:
            user_conns = self._queues.get(user_id)
            if not user_conns:
                # User not currently connected — that's fine, they'll poll
                # on reconnect or miss the event entirely (SSE is best-effort).
                logger.debug("broadcast: user %s has no active SSE connections", user_id)
                continue

            dead_conn_ids: list[int] = []

            for conn_id, queue in user_conns.items():
                try:
                    queue.put_nowait(payload)
                    total_delivered += 1
                except asyncio.QueueFull:
                    logger.warning(
                        "SSE conn %d (user %s) queue full — dropping '%s'",
                        conn_id, user_id, event_type,
                    )
                    total_dropped += 1
                except Exception as exc:
                    logger.error(
                        "SSE broadcast to conn %d (user %s) failed: %s",
                        conn_id, user_id, exc,
                    )
                    dead_conn_ids.append(conn_id)

            for cid in dead_conn_ids:
                user_conns.pop(cid, None)

        logger.debug(
            "Broadcast '%s' → %d delivered, %d dropped",
            event_type, total_delivered, total_dropped,
        )

    # ─── Subscribe ────────────────────────────────────────────────────────────

    async def subscribe(self, user_id: str) -> AsyncGenerator[str, None]:
        """
        Async generator that yields SSE-formatted strings for a specific user.

        Each call creates one connection slot under that user's bucket.
        A single user may hold multiple concurrent connections (e.g., two
        browser tabs) — each gets an independent queue.

        Yields:
            "data: <json>\\n\\n"  for real events
            ": keepalive\\n\\n"   every 30 s to prevent proxy timeout
        """
        conn_id = self._new_conn_id()
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

        # Register under the user's bucket
        self._queues[user_id][conn_id] = queue
        total_conns = sum(len(v) for v in self._queues.values())
        logger.info(
            "SSE conn %d opened for user %s (total connections: %d)",
            conn_id, user_id, total_conns,
        )

        try:
            while True:
                try:
                    # 30 s timeout: emit keepalive comment to prevent proxy
                    # servers (nginx, Azure Application Gateway) from silently
                    # closing idle persistent connections.
                    payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            # Clean up this connection slot
            user_conns = self._queues.get(user_id, {})
            user_conns.pop(conn_id, None)
            # Remove the user's bucket entirely if no connections remain
            if not user_conns:
                self._queues.pop(user_id, None)
            remaining = sum(len(v) for v in self._queues.values())
            logger.info(
                "SSE conn %d closed for user %s (total connections: %d)",
                conn_id, user_id, remaining,
            )

    # ─── Introspection (used by tests + health endpoint) ─────────────────────

    def active_user_count(self) -> int:
        """Number of distinct users with at least one open SSE connection."""
        return len(self._queues)

    def active_connection_count(self) -> int:
        """Total number of open SSE connections across all users."""
        return sum(len(v) for v in self._queues.values())


# Module-level singleton — shared by consumer and SSE route.
broadcaster = _Broadcaster()
