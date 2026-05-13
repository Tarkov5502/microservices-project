"""
api-gateway/app/middleware/security_headers.py

Security headers middleware — applied to EVERY response before it leaves the gateway.

HEADERS APPLIED:
  X-Content-Type-Options: nosniff
    → Browsers must not MIME-sniff. Prevents e.g. a JSON response from being
      executed as JavaScript if a legacy browser mis-identifies the type.

  X-Frame-Options: DENY
    → Blocks this API from being embedded in an <iframe>. Prevents clickjacking.
      Note: CSP frame-ancestors is the modern successor, but X-Frame-Options
      still protects legacy browsers.

  X-XSS-Protection: 0
    → Deliberately disabling the old IE/Chrome XSS filter. It's been removed
      from all modern browsers and keeping it enabled can *create* vulnerabilities
      (reflected XSS via filter-bypass). CSP is the correct defense.

  Strict-Transport-Security: max-age=63072000; includeSubDomains; preload
    → Forces HTTPS for 2 years. The preload directive means browsers will
      refuse HTTP connections entirely (HSTS preload list). Only enable once
      you're 100% committed to HTTPS.

  Content-Security-Policy: default-src 'none'; frame-ancestors 'none'
    → This is a JSON API. There is zero reason to load scripts, images, or
      fonts from it. A fully restrictive CSP prevents any response from being
      used as a XSS launch pad if a frontend erroneously renders our JSON.
      frame-ancestors 'none' supersedes X-Frame-Options in modern browsers.

  Referrer-Policy: strict-origin-when-cross-origin
    → Prevents the full URL (including query params, auth codes, etc.) from
      leaking in the Referer header to third-party origins.

  Permissions-Policy
    → Disables browser features the API never needs. Belt-and-suspenders
      against XSS pivoting to camera/mic/geolocation access.

  Cache-Control: no-store (auth paths only)
    → Prevents JWTs and session data from being cached by any intermediate
      proxy, CDN, or browser. Applied selectively to /auth/* paths.
"""
import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Paths where Cache-Control: no-store MUST be set.
# These paths hand out or consume credentials.
_CREDENTIAL_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/register",
    "/api/v1/auth/refresh",
    "/api/v1/users/me/password",
})

# Headers applied to every single response.
_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options":   "nosniff",
    "X-Frame-Options":          "DENY",
    "X-XSS-Protection":         "0",
    # 2 years in seconds; includeSubDomains + preload for max coverage.
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    # Fully restrictive CSP for a pure JSON API.
    "Content-Security-Policy":   "default-src 'none'; frame-ancestors 'none'",
    "Referrer-Policy":           "strict-origin-when-cross-origin",
    "Permissions-Policy":        (
        "camera=(), microphone=(), geolocation=(), "
        "interest-cohort=(), payment=()"
    ),
}

# Headers to actively REMOVE from responses to reduce fingerprinting surface.
# The 'server' header leaks the web server name + version (e.g. "uvicorn").
# 'x-powered-by' is commonly added by frameworks and reveals the stack.
_HEADERS_TO_REMOVE = frozenset({"server", "x-powered-by"})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Outermost middleware — runs LAST on the response path, so it's guaranteed
    to apply security headers even if inner middleware forgets to.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Apply all static security headers
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value

        # Apply Cache-Control on credential-sensitive paths
        if request.url.path in _CREDENTIAL_PATHS:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"

        # Strip fingerprinting headers
        for h in _HEADERS_TO_REMOVE:
            if h in response.headers:
                del response.headers[h]

        return response
