"""
api-gateway/app/middleware/correlation.py

X-Request-ID correlation middleware.

WHY CORRELATION IDs?
  A single user action (e.g. "create task") produces log lines across 3 services:
    [api-gateway]        POST /api/v1/tasks 201 in 142ms
    [task-service]       Creating task for project abc-123
    [task-service]       Published event task.created
    [notification-service] Processing task.created for user xyz

  Without a shared ID, you cannot connect these 4 lines in a log aggregator
  (Loki, CloudWatch, Azure Monitor). You know SOMETHING happened, but you
  can't trace the request end-to-end.

  With X-Request-ID propagated through every service boundary, all 4 lines
  share the same ID. One Loki query shows the entire lifecycle:
    {request_id="f47ac10b-58cc-4372-a567-0e02b2c3d479"}

HOW IT WORKS:
  Gateway Middleware (this file):
    1. Check if the client sent an X-Request-ID header.
    2. If yes, validate it (max 128 chars, printable ASCII — prevent log injection).
    3. If no, generate a UUID4.
    4. Store in request.state.request_id.
    5. Add to response headers so clients can reference it in support tickets.

  Proxy (proxy.py):
    6. Read request.state.request_id and forward it to upstream services.

  Upstream services:
    7. Receive X-Request-ID header.
    8. Include it in every log line via a logging filter.
    9. Forward it if they make further outbound calls (not applicable in this
       architecture since services don't call each other directly).

SECURITY:
  We never echo raw client-supplied header values directly — we validate
  the format first. An attacker could inject newlines into the header value
  to poison log files (log injection / CRLF injection). The validation regex
  limits the ID to printable ASCII without whitespace.
"""
import re
import uuid
import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Strict allowlist: UUID4 format, or generic alphanumeric+hyphens up to 128 chars.
# This blocks newlines, nulls, and control characters that could poison logs.
_REQUEST_ID_RE = re.compile(r"^[\w\-]{1,128}$")


def _sanitize_or_generate(raw: str | None) -> str:
    """Validate an incoming request ID or generate a fresh UUID4."""
    if raw and _REQUEST_ID_RE.match(raw):
        return raw
    if raw:
        logger.warning("Rejected malformed X-Request-ID: %r — generating new ID", raw[:64])
    return str(uuid.uuid4())


class CorrelationMiddleware(BaseHTTPMiddleware):
    """
    Generates or validates X-Request-ID and makes it available on request.state.
    Attaches the ID to every response so clients can correlate support issues.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        raw_id = request.headers.get("x-request-id")
        request_id = _sanitize_or_generate(raw_id)

        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
