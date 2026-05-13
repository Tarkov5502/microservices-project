"""
JWT Authentication Middleware

Validates Bearer tokens on every request (except exempt paths).
On success, injects user_id into request.state for downstream use.

Fix #7 — JWT sub claim UUID validation:
  The 'sub' claim must be a UUID string, not an arbitrary value. Without
  this check, a crafted token with sub="../../admin" or sub="*" gets
  forwarded as X-User-Id to backend services which parse it as-is. If
  any service does string-level comparisons rather than UUID parsing,
  unexpected results follow. We validate here at the gateway boundary.

Fix — Case-insensitive Bearer prefix:
  HTTP headers are case-insensitive per RFC 7230. The Authorization header
  value however is defined by RFC 6750 to use "Bearer" (capital B). We
  normalise to handle both "Bearer" and "bearer" for robustness.
"""
import uuid
import logging
from typing import Callable

import jwt
from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger(__name__)


class JWTAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, exempt_paths: list[str] | None = None):
        super().__init__(app)
        self.exempt_paths = set(exempt_paths or [])

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self.exempt_paths:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        # Case-insensitive prefix check (RFC 7230)
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Missing or invalid Authorization header"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = auth_header[7:].strip()  # Remove exactly 7 chars: "bearer "
        try:
            payload = jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )

            # FIX: Validate 'sub' is a real UUID before forwarding it.
            # This prevents type-confusion attacks on downstream services.
            raw_sub = payload.get("sub", "")
            try:
                user_uuid = uuid.UUID(str(raw_sub))
            except (ValueError, AttributeError):
                logger.warning("JWT 'sub' claim is not a valid UUID: %r", raw_sub)
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Invalid token claims"},
                )

            request.state.user_id = str(user_uuid)  # Canonical lowercase form
            request.state.user_email = payload.get("email")
            request.state.user_roles = payload.get("roles", [])

        except jwt.ExpiredSignatureError:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Token expired"},
            )
        except jwt.InvalidTokenError as exc:
            logger.warning("Invalid JWT: %s", exc)
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid token"},
            )

        return await call_next(request)
