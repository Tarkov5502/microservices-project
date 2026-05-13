"""
Authentication routes: register and login.

FIX #3 — Async bcrypt:
  bcrypt is a *blocking* CPU-bound operation (~300ms per call with rounds=12).
  Running it synchronously on the asyncio event loop stalls ALL concurrent
  requests for the entire duration of each hash — effectively a single-threaded
  bottleneck. Under moderate load this cascades into timeouts across the entire
  user-service.

  Fix: wrap both hash and verify in asyncio.to_thread() which offloads the
  blocking call to a thread-pool worker without blocking the event loop.
  Python 3.12 guarantees a minimum pool of 32 threads by default.

Fix — Audit logging:
  All authentication events (success, failure, registration) are now emitted
  as structured JSON audit events. See app/audit.py for query examples.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log_login_failure, log_login_success, log_registration
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import LoginRequest, TokenResponse, UserCreate, UserResponse

logger = logging.getLogger(__name__)
router = APIRouter()


# A pre-computed bcrypt hash of a throwaway string used for timing-safe
# rejection. When a user is not found we still run bcrypt.checkpw() against
# this dummy hash so the response time is indistinguishable from a valid but
# wrong-password attempt, preventing email enumeration via timing analysis.
#
# NOTE: This is computed ONCE at module import time (synchronous is fine here
# because it happens before any event loop is running). Do NOT use a hardcoded
# hash constant — the algorithm version and cost factor are embedded in the
# hash string, so computing it at startup ensures they always match the bcrypt
# version installed in the image.
_DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=12)).decode()


# ─── Async password helpers ───────────────────────────────────────────────────

async def _hash_password(plain: str) -> str:
    """bcrypt hash — runs in a thread pool to avoid blocking the event loop."""
    return await asyncio.to_thread(
        lambda: bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()
    )


async def _verify_password(plain: str, hashed: str) -> bool:
    """bcrypt verify — runs in a thread pool to avoid blocking the event loop."""
    return await asyncio.to_thread(
        lambda: bcrypt.checkpw(plain.encode(), hashed.encode())
    )


def _create_jwt(user: User) -> tuple[str, int]:
    """Returns (token, expires_in_seconds)."""
    expires_in = settings.jwt_expiry_minutes * 60
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "roles": ["admin"] if user.is_admin else ["user"],
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expiry_minutes),
        "iat": datetime.now(timezone.utc),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_in


def _client_ip(request: Request) -> str | None:
    """Extract client IP for audit logging. Best-effort, never raises."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Register a new user account."""
    # Check uniqueness
    existing = await db.execute(
        select(User).where((User.email == payload.email) | (User.username == payload.username))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email or username already registered",
        )

    user = User(
        email=payload.email,
        username=payload.username,
        hashed_password=await _hash_password(payload.password),  # FIX #3
        full_name=payload.full_name,
    )
    db.add(user)
    await db.flush()   # Get the generated ID without committing
    await db.refresh(user)

    log_registration(str(user.id), user.email, _client_ip(request))
    logger.info("New user registered: %s", user.email)
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Authenticate and receive a JWT."""
    ip = _client_ip(request)
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    # SECURITY: Always run bcrypt regardless of whether the user exists.
    # Short-circuiting when user is None leaks valid email addresses through
    # measurably different response times (timing attack / user enumeration).
    candidate_hash = user.hashed_password if user else _DUMMY_HASH
    password_ok = await _verify_password(payload.password, candidate_hash)  # FIX #3

    if not user or not password_ok:
        log_login_failure(payload.email, "invalid_credentials", ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        log_login_failure(payload.email, "account_deactivated", ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account deactivated")

    user.last_login_at = datetime.now(timezone.utc)
    token, expires_in = _create_jwt(user)
    log_login_success(str(user.id), user.email, ip)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserResponse.model_validate(user),
    )
