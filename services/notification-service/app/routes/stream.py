"""
notification-service/app/routes/stream.py — Server-Sent Events endpoint.

SSE PROTOCOL PRIMER:
  - Plain HTTP GET that keeps the connection open indefinitely.
  - Response Content-Type: text/event-stream
  - Server sends lines formatted as:  data: <json>\n\n
  - Comments (keepalives):            : keepalive\n\n
  - Client reconnects automatically on drop (built into EventSource API).

CLIENT USAGE (JavaScript):
  const es = new EventSource(
    'https://your-domain.com/api/v1/notifications/stream',
    { headers: { Authorization: 'Bearer <token>' } }
  );
  es.onmessage = (e) => {
    const { event_type, data } = JSON.parse(e.data);
    console.log(event_type, data);
  };
  es.onerror = () => console.error('SSE connection dropped, auto-reconnecting...');

AUTHENTICATION:
  The gateway injects X-User-Id after verifying the JWT, same as all other routes.
  EventSource doesn't support custom headers in all browsers — the recommended
  workaround is to pass the JWT as a query param (?token=...) or use a cookie.
  For this learning project we accept X-User-Id via the gateway proxy.

GATEWAY PROXY REQUIREMENT:
  httpx's default mode buffers the full response before returning it, which
  would cause the SSE connection to hang forever waiting for EOF. The gateway
  proxy must detect the text/event-stream Accept header and use streaming mode.
  See api-gateway/app/proxy.py for the implementation.
"""
import logging
from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from app.broadcaster import broadcaster

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/stream",
    summary="Subscribe to real-time notification events via SSE",
    response_description="Server-Sent Events stream",
    tags=["notifications"],
)
async def notification_stream(
    x_user_id: str = Header(..., alias="X-User-Id"),
) -> StreamingResponse:
    """
    Long-lived SSE endpoint. Returns all task events to the connected client.

    Events emitted:
      task.created        — A task was assigned to someone
      task.status_changed — A task's status was updated
      task.deleted        — A task was removed

    The stream sends keepalive comments every 30s to prevent proxy timeouts.
    Clients should reconnect on drop (the EventSource API does this automatically).
    """
    logger.info("SSE stream opened for user %s", x_user_id)

    async def _event_stream():
        async for chunk in broadcaster.subscribe():
            yield chunk

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Tell Nginx not to buffer SSE
            "Connection": "keep-alive",
        },
    )
