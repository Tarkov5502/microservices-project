"""
notification-service/app/broadcaster.py — In-memory SSE event broadcaster.

PURPOSE:
  Bridge the Service Bus consumer (which runs in a background asyncio task)
  to SSE connections (which are long-lived HTTP responses). When the consumer
  processes a message it calls broadcast(); every connected SSE client
  immediately receives the event without polling.

ARCHITECTURE:
                  ┌─────────────────┐
  Service Bus ──► │ ServiceBusConsumer│──► broadcaster.broadcast()
                  └─────────────────┘           │
                                                ▼
  HTTP Client ◄── SSE stream ◄── Queue ◄── Broadcaster._queues
                                (one per connected client)

WHY IN-MEMORY QUEUES (not Redis Pub/Sub)?
  For a learning project running 1–2 replicas, asyncio queues are simpler and
  sufficient. In production with many replicas, each pod only broadcasts to
  clients connected to THAT pod. Redis Pub/Sub or a message bus would be used
  to fan-out across all pods. This is deliberately called out as a known
  limitation in the code below.

KNOWN LIMITATION — Multi-pod fan-out:
  If notification-service runs N replicas, a consumer on pod A broadcasts to
  clients on pod A only. Clients on pods B through N miss the event. For a
  stateless fan-out, replace _queues with a Redis Pub/Sub channel and have
  each pod subscribe. The interface here is designed so that swap-out is
  isolated to broadcast() and subscribe() only — callers are unaffected.
"""
import asyncio
import json
import logging
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


class _Broadcaster:
    """
    Fan-out in-memory event bus.

    Thread safety: asyncio.Queue is designed for single-threaded async use.
    All operations must be called from within the same event loop.
    """

    def __init__(self) -> None:
        self._queues: dict[int, asyncio.Queue[str]] = {}
        self._next_id = 0

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def broadcast(self, event_type: str, data: dict) -> None:
        """
        Push an event to all connected SSE clients.
        Called by the Service Bus consumer after each successful message.
        Never raises — a broadcast failure must never crash the consumer.
        """
        if not self._queues:
            return
        payload = json.dumps({"event_type": event_type, "data": data})
        disconnected: list[int] = []
        for client_id, queue in self._queues.items():
            try:
                # put_nowait: non-blocking. If a client's queue is full (maxsize
                # reached), drop tfor that client rather than blocking
                # ALL other clients waiting on a slow consumer.
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("SSE client %d queue full — dropping event %s", client_id, event_type)
            except Exception as exc:
                logger.error("SSE broadcast to client %d failed: %s", client_id, exc)
                disconnected.append(client_id)
        for cid in disconnected:
            self._queues.pop(cid, None)
        if self._queues:
            logger.debug("Broadcast '%s' to %d SSE client(s)", event_type, len(self._queues))

    async def subscribe(self) -> AsyncGenerator[str, None]:
        """
        Async generator: yields SSE-formatted event strings until the client
        disconnects. Automatically cleans up on exit (cancellation or exception).

        Usage:
            async for raw_line in broadcaster.subscribe():
                yield raw_line
        """
        client_id = self._new_id()
        # maxsize=100: at 100 queued events, new events are dropped for this
        # client. This prevents slow clients from consuming unbounded memory.
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._queues[client_id] = queue
        logger.info("SSE client %d connected (total: %d)", client_id, len(self._queues))
        try:
            while True:
                # 30s timeout: send a keepalive comment so the connection
                # isn't silently killed by intermediary proxies that close
                # idle connections. SSE comments start with ':'.
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            self._queues.pop(client_id, None)
            logger.info("SSE client %d disconnected (total: %d)", client_id, len(self._queues))


# Module-level singleton — shared by consumer and SSE route.
broadcaster = _Broadcaster()
