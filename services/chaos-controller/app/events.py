"""
chaos-controller/app/events.py

Event bus. The controller has multiple async producers (K8s watch loop,
Prometheus poller, health probe loop, chaos action handlers) that all
need to push events into a single stream consumed by the frontend via SSE.

We use a per-connection asyncio.Queue so each subscriber gets its own
buffer. The Hub keeps a set of active queues and broadcasts to all.

Event shape:
    {
      "ts":     float (unix seconds, source of truth for ordering),
      "kind":   str  (one of: action, k8s, probe, metric, narration, log),
      "level":  str  (info | warn | crit | success),
      "payload": dict
    }

The kind field tells the frontend which UI region the event belongs to:

    action     → buttons disable, T+0 anchor resets
    k8s        → timeline entry (pod lifecycle, readiness probe, etc.)
    probe      → service-health tile updates, outage window detection
    metric     → live latency/RPS/error chart points
    narration  → "what should happen next" overlay items, mark them ✓
    log        → freeform info (the controller's own state)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator


class EventHub:
    """Pub/sub for chaos events. One queue per SSE subscriber."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)

    def publish(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        level: str = "info",
        ts: float | None = None,
    ) -> None:
        """Publish to every subscriber. Drops events on a full subscriber queue
        rather than blocking — slow consumers shouldn't stall the controller."""
        if ts is None:
            ts = time.time()
        event = {"ts": ts, "kind": kind, "level": level, "payload": payload}
        line = f"data: {json.dumps(event)}\n\n"
        dead: list[asyncio.Queue[str]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    async def stream(self, q: asyncio.Queue[str]) -> AsyncIterator[str]:
        """Consume from a subscriber queue. Sends a comment-line every 15s
        to keep the SSE connection alive through any intermediate proxies
        with idle timeouts."""
        try:
            while True:
                try:
                    line = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield line
                except asyncio.TimeoutError:
                    # SSE comment line — ignored by EventSource but keeps proxies happy
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            return


hub = EventHub()
