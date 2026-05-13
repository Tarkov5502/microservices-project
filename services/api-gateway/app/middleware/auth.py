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
    """
    Validates Bearer JWTs on every inbound request.

    EXEMPT PATHS vs EXEMPT PREFIXES:
      exact_exempt_paths — full path strings ("/health", "/metrics").
      exempt_prefixes    — path prefixes. Any path starting with a prefix is
                           exempt. Used for the entire /api/v1/auth/* family:

        /api/v1/auth/login    — no token exists yet
        /api/v1/auth/register — same
        /api/v1/auth/refresh  — carries a refresh token, not a JWT
        /api/v1/auth/logout   — must work even with an expired JWT so the
                               client can always revoke its refresh token

      These MUST be exempt or the system is a deadlock: you cannot get a
      token without calling login, and login requires a token.
    """
    def __init__(
        self,
        app,
        exempt_paths: list[str] | None = None,
        exempt_prefixes: list[str] | None = None,
    ):
        super().__init__(app)
        self._exempt_paths: frozenset[str] = frozenset(exempt_paths or [])
        self._exempt_prefixes: tuple[str, ...] = tuple(exempt_prefixes or [])

    def _is_exempt(self, path: str) -> bool:
        if path in self._exempt_paths:
            return True
        return any(path.startswith(p) for p in self._exempt_prefixes)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if self._is_exempt(request.url.path):
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
        # ── Keyring-aware verification ──────────────────────────────────────
        # Inspect the JOSE header without verifying first, so we can pick the
        # correct secret based on the `kid` claim. Then verify normally with
        # that secret. Tokens with an unknown kid are rejected.
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            logger.warning("Malformed JWT header: %s", exc)
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid token"},
            )

        from app.jwt_keyring import parse_keyring, select_verification_key
        try:
            keyring = parse_keyring(settings.jwt_secrets, settings.jwt_secret)
        except Exception as exc:
            logger.error("JWT keyring is misconfigured: %s", exc)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "Server misconfigured"},
            )
        secret = select_verification_key(keyring, unverified_header.get("kid"))
        if secret is None:
            logger.warning(
                "JWT references unknown kid=%r — token rejected",
                unverified_header.get("kid"),
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid token"},
            )

        try:
            payload = jwt.decode(
                token,
                secret,
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
