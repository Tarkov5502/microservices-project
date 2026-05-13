"""
Identity-header HMAC signing — shared design notes.

THREAT MODEL THIS CLOSES:
  Today, the backend services trust whatever X-User-Id / X-User-Email /
  X-User-Roles headers the api-gateway forwards. The justification has been
  "NetworkPolicy only lets the gateway reach the backends." That's true today.
  But it's a single layer of defence, and it fails in any of the following
  scenarios:

    1. A misapplied or absent NetworkPolicy (e.g. fresh cluster, namespace
       label typo, manual deletion during a debugging session).
    2. A new sidecar or operator pod that gets accidentally allowed to talk
       to the backends but is not the gateway.
    3. A port-forward during incident response — `kubectl port-forward
       svc/user-service` from a workstation lets the operator send anything.
    4. Lateral movement after a compromise of any pod inside the namespace.

  In any of those cases, an attacker can hit the backend directly with
  `X-User-Id: <victim-uuid>` and act as that user. The backend has no way
  to know the headers didn't come from the legitimate gateway.

  Signing the identity headers with an HMAC closes this: the gateway holds
  a secret known only to the backends, signs the identity envelope, and
  attaches the signature. Backends verify the signature before trusting any
  X-User-* claim. An attacker who can reach the backend directly but doesn't
  know the secret cannot mint a valid signature.

ENVELOPE FORMAT:
  The signer computes HMAC-SHA256 over a canonical string of:
    f"{user_id}|{user_email}|{user_roles}|{issued_at_unix}"

  Headers:
    X-Identity-Signature   — base64 of the HMAC bytes
    X-Identity-Issued-At   — unix seconds, prevents indefinite replay
  Verifier requirements:
    - signature MUST match (constant-time compare)
    - issued_at MUST be within IDENTITY_MAX_AGE_SECONDS of now (default 60s)
    - user_id MUST be a UUID

KEY MANAGEMENT:
  Both sides load INTERSERVICE_HMAC_SECRET from env (synced from Key Vault).
  Validate length ≥ 32 bytes at startup just like jwt_secret. Rotate by
  setting the secret to a new value across gateway + backends and rolling
  pods in sequence; the 60-second issuance window absorbs the rolling skew.

WHY NOT mTLS / SPIFFE?
  mTLS is correct for this in the long run and we're deliberately not pretending
  otherwise. It's a heavier lift — cluster issuer, cert distribution, sidecar
  injection. The HMAC approach gets us a real cryptographic identity proof
  today at the cost of a few dozen lines, and the verification interface
  doesn't change if/when we migrate to mTLS later.
"""
import base64
import hashlib
import hmac
import logging
import os
import time
import uuid
from typing import Callable

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

IDENTITY_SIG_HEADER = "X-Identity-Signature"
IDENTITY_TS_HEADER = "X-Identity-Issued-At"
IDENTITY_MAX_AGE_SECONDS = 60


def _canonical(user_id: str, user_email: str, user_roles: str, ts: str) -> bytes:
    """The exact string we sign over. Order is part of the contract."""
    return f"{user_id}|{user_email}|{user_roles}|{ts}".encode("utf-8")


def sign_identity(
    secret: str,
    user_id: str,
    user_email: str,
    user_roles: str,
    issued_at: int | None = None,
) -> tuple[str, str]:
    """
    Compute a signature for an identity envelope.

    Returns (signature_b64, issued_at_str). Caller attaches these as
    X-Identity-Signature and X-Identity-Issued-At headers on the proxied
    request.
    """
    ts = str(issued_at if issued_at is not None else int(time.time()))
    mac = hmac.new(
        secret.encode("utf-8"),
        _canonical(user_id, user_email or "", user_roles or "", ts),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(mac).decode("ascii"), ts


def verify_identity(
    secret: str,
    user_id: str,
    user_email: str,
    user_roles: str,
    issued_at: str,
    signature_b64: str,
    *,
    max_age_seconds: int = IDENTITY_MAX_AGE_SECONDS,
    now: int | None = None,
) -> bool:
    """
    Validate a signature + freshness. Returns True iff:
      - issued_at parses as an integer
      - now() - issued_at is within max_age_seconds (positive or near-zero
        future skew up to 5 s is tolerated)
      - HMAC-SHA256 over the canonical envelope matches the provided signature
        in constant time
    """
    try:
        ts = int(issued_at)
    except (TypeError, ValueError):
        return False

    now = int(now if now is not None else time.time())
    age = now - ts
    # Reject anything more than max_age_seconds in the past, OR more than 5
    # seconds in the future (small forward skew is OK; large = suspicious).
    if age > max_age_seconds or age < -5:
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        _canonical(user_id, user_email or "", user_roles or "", str(ts)),
        hashlib.sha256,
    ).digest()
    try:
        provided = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return False
    return hmac.compare_digest(expected, provided)


# ─── Middleware for the verifier side (backends) ─────────────────────────────

class IdentityVerifierMiddleware(BaseHTTPMiddleware):
    """
    Reject any request whose X-User-* headers are not signed by the gateway's
    shared secret. Exempt health, metrics, and any explicit pre-shared paths
    (typically none — even /api/v1/auth/login flows through the gateway, which
    signs an anonymous identity envelope).

    Usage:
      app.add_middleware(
          IdentityVerifierMiddleware,
          secret=settings.interservice_hmac_secret,
          exempt_paths=["/health", "/health/ready", "/metrics"],
      )
    """
    def __init__(self, app, secret: str, exempt_paths: list[str] | None = None):
        super().__init__(app)
        if not secret or len(secret) < 32:
            raise ValueError(
                "interservice_hmac_secret must be ≥ 32 chars. Generate one with: "
                "python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        self._secret = secret
        self._exempt = frozenset(exempt_paths or [])

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in self._exempt:
            return await call_next(request)

        user_id = request.headers.get("X-User-Id", "")
        user_email = request.headers.get("X-User-Email", "")
        user_roles = request.headers.get("X-User-Roles", "")
        signature = request.headers.get(IDENTITY_SIG_HEADER, "")
        issued_at = request.headers.get(IDENTITY_TS_HEADER, "")

        # If there's no identity at all, this is an anonymous request which
        # the gateway didn't intend to authenticate. Reject anything that
        # tries to pretend it's authenticated without a signature.
        if not signature or not issued_at:
            if user_id:
                # The presence of X-User-Id without a signature is suspicious
                # — someone is trying to forge identity. Log it.
                logger.warning(
                    "Rejecting request with unsigned identity headers — "
                    "user_id=%r path=%s", user_id, request.url.path,
                )
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Missing identity signature"},
                )
            # Fully anonymous (no user, no sig) — fall through. The route
            # itself decides whether to require auth via FastAPI deps.
            return await call_next(request)

        if not verify_identity(
            self._secret, user_id, user_email, user_roles, issued_at, signature
        ):
            logger.warning(
                "Identity signature INVALID — user_id=%r path=%s",
                user_id, request.url.path,
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid identity signature"},
            )

        # Belt-and-braces: even after a valid signature, refuse to forward
        # a malformed user_id. The signature attests "the gateway sent this
        # exact string"; type discipline is still ours to enforce.
        if user_id:
            try:
                uuid.UUID(user_id)
            except (ValueError, AttributeError):
                logger.warning("Signed identity has malformed user_id: %r", user_id)
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Invalid identity claims"},
                )

        return await call_next(request)
