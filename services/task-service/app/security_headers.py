"""
A tiny shared SecurityHeadersMiddleware for internal services.

WHY THIS EXISTS:
  The api-gateway already applies a comprehensive set of security headers.
  Backend services (user/task/notification) are NetworkPolicy-restricted —
  only the gateway can reach them, so in theory they never face a browser
  directly. But "in theory" is brittle: NetworkPolicy misconfiguration, a
  port-forward during incident response, or a future internal browser-based
  admin tool can all expose these services to user agents.

  Defense-in-depth says each service should harden its own responses. The
  headers cost nothing to add and protect us against the cases we forgot.

DIFFERENCES FROM THE GATEWAY MIDDLEWARE:
  - No CORS coupling — these services should NOT advertise CORS.
  - No HSTS — backend services are not directly reachable over the public
    Internet, so HSTS has no audience and could confuse internal tooling.
  - Tighter CSP (default-src 'none') — same as gateway.
  - Strips the `server` and `x-powered-by` fingerprinting headers.

USAGE (in each service's main.py):

    from app.security_headers import SecurityHeadersMiddleware
    app.add_middleware(SecurityHeadersMiddleware)

  (We can't `from services._shared_security_headers import ...` because each
  service is its own container with its own module path. The intended pattern
  is to copy this module into each service's app/ tree as security_headers.py
  — small, identical, deliberately duplicated.)
"""
import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

_SECURITY_HEADERS: dict[str, str] = {
    # Browsers must not MIME-sniff. Prevents a JSON response from being
    # executed as JavaScript if a legacy browser mis-identifies the type.
    "X-Content-Type-Options":   "nosniff",
    # Blocks the API from being embedded in an <iframe>. Backend services
    # have no frontend, so a frame embed is by definition malicious.
    "X-Frame-Options":          "DENY",
    # Deliberately disable the legacy XSS filter — it can introduce bypass
    # vulnerabilities. CSP is the correct defence.
    "X-XSS-Protection":         "0",
    # Fully restrictive CSP for a pure JSON API. No legitimate script or
    # resource load should ever originate from a backend response.
    "Content-Security-Policy":  "default-src 'none'; frame-ancestors 'none'",
    # Prevents the full URL (including auth codes, tokens) from leaking via
    # the Referer header on any cross-origin navigation.
    "Referrer-Policy":          "strict-origin-when-cross-origin",
}

# Headers we actively strip from responses to reduce fingerprinting surface.
_HEADERS_TO_REMOVE = frozenset({"server", "x-powered-by"})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Applies the security header set above to every response and strips
    fingerprinting headers. Idempotent — running this twice (e.g. nested
    behind the gateway) does no harm.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value
        for h in _HEADERS_TO_REMOVE:
            if h in response.headers:
                del response.headers[h]
        return response
