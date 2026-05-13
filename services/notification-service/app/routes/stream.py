"""
notification-service/app/routes/stream.py — Server-Sent Events endpoint.

SSE PROTOCOL PRIMER:
  - Plain HTTP GET that keeps the connection open indefinitely.
  - Response Content-Type: text/event-stream
  - Server sends lines formatted as:  data: <json>\\n\\n
  - Comments (keepalives):            : keepalive\\n\\n
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
  The gateway injects X-User-Id after verifying the JWT, same as all other
  routes. EventSource doesn't support custom headers in all browsers — the
  recommended workaround is to pass the JWT as a query param (?token=...) or
  use a cookie. For this project we accept X-User-Id via the gateway proxy.

  PRIVACY: Unlike the original implementation which broadcast to ALL connected
  clients, this endpoint now calls broadcaster.subscribe(user_id) so that only
  events explicitly targeted at this user's ID are delivered. This is enforced
  in the consumer's broadcast() calls, not here — separation of concerns.

GATEWAY PROXY REQUIREMENT:
  httpx's default mode buffers the full response before returning it, which
  would cause the SSE connection to hang forever waiting for EOF. The gateway
  proxy detects the text/event-stream Accept header and uses streaming mode.
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
    response_description="Server-Sent Events stream — events scoped to the authenticated user",
    tags=["notifications"],
)
async def notification_stream(
    x_user_id: str = Header(..., alias="X-User-Id"),
) -> StreamingResponse:
    """
    Long-lived SSE endpoint. Delivers task events targeted at the authenticated
    user only. Events for other users are never delivered to this connection.

    Events emitted:
      task.created        — A task was assigned to this user
      task.status_changed — A task this user owns or is assigned to was updated
      task.deleted        — A task this user owns was removed

    Keepalive comments (': keepalive') are sent every 30 s to prevent
    upstream proxies from closing the idle connection.

    The stream is scoped by X-User-Id (injected by the API gateway after JWT
    validation). Clients cannot subscribe to another user's stream.
    """
    logger.info("SSE stream opened for user %s", x_user_id)

    async def _event_stream():
        async for chunk in broadcaster.subscribe(user_id=x_user_id):
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
