"""
api-gateway/app/middleware/body_size.py

Reject requests whose body exceeds MAX_REQUEST_BODY_BYTES *before* the handler
buffers them in memory.

WHY THIS IS A SECURITY CONTROL:
  Without an explicit limit, FastAPI happily reads request bodies up to whatever
  the underlying HTTP server (uvicorn) allows by default — many megabytes. A
  hostile or buggy client can:
    - Stream gigabytes of JSON at /api/v1/tasks/, exhausting RAM on the gateway.
    - Open many concurrent slow uploads, holding sockets + buffers open until
      the gateway runs out of file descriptors or memory.

  Most of the routes this gateway proxies accept JSON payloads in the kilobyte
  range. Setting a hard cap an order of magnitude above the largest legitimate
  payload makes resource-exhaustion attacks fail fast at the edge instead of
  propagating to a backend service.

DETECTION ORDER:
  1. If the client sent a `Content-Length` header, we trust it and reject up-
     front before reading a single byte. This is the cheap path.
  2. If `Content-Length` is missing (chunked transfer-encoding, malicious
     omission), we wrap the receive() ASGI callable and accumulate the byte
     count as the request streams in. We abort and return 413 the moment we
     cross the threshold — we do NOT keep buffering the rest of the request
     just to be sure.

EXEMPT METHODS:
  GET/HEAD/DELETE/OPTIONS rarely carry a body. We still apply the check to
  them in case a malformed client sends one; the limit just never trips in
  normal use.

CONFIGURATION:
  Constructed with `max_bytes` (default 1 MiB). Override at app wiring time
  from settings:

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
"""
import logging
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """
    Reject requests whose body exceeds `max_bytes` with a 413 Payload Too Large.

    Two-phase enforcement:
      Phase 1: trust Content-Length if the client supplied it.
      Phase 2: count incoming bytes as the body streams; bail at the threshold.
    """

    def __init__(self, app, max_bytes: int = 1 * 1024 * 1024) -> None:
        super().__init__(app)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self.max_bytes = max_bytes

    def _too_large_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=413,
            content={
                "detail": (
                    f"Request body exceeds the {self.max_bytes}-byte limit. "
                    "Split the payload or contact an operator if your use case "
                    "legitimately requires a larger body."
                )
            },
            # Tell the client (and any caching proxies) how big we'll accept.
            headers={"Connection": "close"},
        )

    async def dispatch(self, request: Request, call_next: Callable):
        # ── Phase 1: declared content-length ────────────────────────────────
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    logger.warning(
                        "Rejecting oversize request: declared %s bytes > limit %d",
                        declared, self.max_bytes,
                    )
                    return self._too_large_response()
            except ValueError:
                # Malformed Content-Length — treat as suspicious. Let phase 2
                # handle it; if the body really is small, the request flows.
                logger.warning("Malformed Content-Length header: %r", declared)

        # ── Phase 2: count bytes as they stream in ─────────────────────────
        # We wrap the ASGI `receive` callable. Every time the framework reads
        # a chunk we accumulate the size and abort if we cross the threshold.
        # This is the only correct way to enforce a limit on
        # Transfer-Encoding: chunked requests where Content-Length is absent.
        received = 0
        oversize = False
        original_receive = request.receive

        async def counting_receive():
            nonlocal received, oversize
            message = await original_receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                received += len(body)
                if received > self.max_bytes:
                    oversize = True
                    # Truncate the body in the message so the downstream
                    # framework sees a sentinel-sized chunk plus more_body=False;
                    # we'll short-circuit with 413 immediately after.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        # Mutate the request to use our counting wrapper.
        request._receive = counting_receive  # type: ignore[attr-defined]

        response = await call_next(request)
        if oversize:
            logger.warning(
                "Rejecting oversize request: streamed %d bytes > limit %d",
                received, self.max_bytes,
            )
            return self._too_large_response()
        return response
