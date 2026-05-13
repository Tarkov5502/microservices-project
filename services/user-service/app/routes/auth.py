"""
user-service/app/routes/auth.py

Authentication routes: register, login, refresh, logout.

REFRESH TOKEN FLOW:
  POST /login  → { access_token (60min JWT), refresh_token (30d opaque UUID) }
  POST /refresh → { new access_token, new refresh_token (old one is revoked) }
  POST /logout  → revoke the refresh token (access JWT still valid until expiry)

TOKEN ROTATION:
  Each /refresh call deletes the presented token and issues a new one.
  An attacker who intercepts a refresh token and uses it before the legitimate
  user does will trigger a revoked-token error on the user's next refresh —
  alerting them to the compromise.
"""
import asyncio
import logging
import uuid
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
from app.redis_client import (
    consume_refresh_token,
    is_account_locked,
    record_login_failure,
    reset_login_failures,
    revoke_refresh_token,
    store_refresh_token,
)
from app.schemas import (
    LoginRequest, LogoutRequest, RefreshRequest,
    TokenResponse, UserCreate, UserResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=12)).decode()


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _hash_password(plain: str) -> str:
    return await asyncio.to_thread(
        lambda: bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()
    )


async def _verify_password(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(
        lambda: bcrypt.checkpw(plain.encode(), hashed.encode())
    )


def _create_access_jwt(user: User) -> tuple[str, int]:
    """Returns (access_token, expires_in_seconds)."""
    expires_in = settings.jwt_expiry_minutes * 60
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "roles": ["admin", "user"] if user.is_admin else ["user"],
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expiry_minutes),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm), expires_in


async def _issue_token_response(user: User) -> TokenResponse:
    """Build a full TokenResponse including a fresh refresh token."""
    access_token, expires_in = _create_access_jwt(user)
    refresh_token = str(uuid.uuid4())
    stored = await store_refresh_token(refresh_token, str(user.id))
    return TokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        user=UserResponse.model_validate(user),
        refresh_token=refresh_token if stored else None,
    )


def _client_ip(request: Request) -> str | None:
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else None)


# ─── Routes ──────────────────────────────────────────────────────────────────

@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    existing = await db.execute(
        select(User).where((User.email == payload.email) | (User.username == payload.username))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Email or username already registered")

    user = User(
        email=payload.email,
        username=payload.username,
        hashed_password=await _hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    log_registration(str(user.id), user.email, _client_ip(request))
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Authenticate with email + password. Returns JWT + opaque refresh token.

    BRUTE FORCE PROTECTION (per-account):
      The gateway rate-limits by IP address (10 req/min). An attacker who
      rotates IPs bypasses the IP limit but is caught here: we track failed
      attempts per email in Redis. After LOCKOUT_THRESHOLD failures within
      LOCKOUT_WINDOW_SECONDS, all further attempts return 401 regardless of
      password correctness.

      ENUMERATION RESISTANCE: Both "wrong password" and "account locked" return
      the same 401 body. The attacker can't distinguish them — they just see
      a constant stream of failures and can't confirm the account exists or
      is locked.

      The timing-safe dummy hash path still runs for non-existent accounts
      even if the lockout check fires, so timing analysis reveals nothing.
    """
    ip = _client_ip(request)

    # ── Step 1: Check per-account lockout BEFORE hitting the DB or bcrypt. ──
    # If the account is locked we still run the full auth path for timing
    # consistency, then return 401 at the end. This prevents timing-based
    # enumeration: locked real accounts and non-existent accounts both take
    # the same amount of time.
    account_is_locked = await is_account_locked(payload.email)

    # ── Step 2: Fetch user + timing-safe bcrypt. ──────────────────────────
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    candidate_hash = user.hashed_password if user else _DUMMY_HASH
    password_ok = await _verify_password(payload.password, candidate_hash)

    # ── Step 3: Evaluate outcome. ───────────────────────────────────
    if account_is_locked or not user or not password_ok:
        reason = "account_locked" if account_is_locked else "invalid_credentials"
        log_login_failure(payload.email, reason, ip)
        # Always increment the counter, even on lockout, to keep the count
        # accurate. (It won't matter to the user, already locked.)
        if user and not account_is_locked:
            # Only increment for real accounts with wrong passwords. Don't
            # increment for non-existent accounts (would allow DoS by
            # pre-locking accounts with fake attempts against a valid email
            # from a leaked list).
            await record_login_failure(payload.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.is_active:
        log_login_failure(payload.email, "account_deactivated", ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account deactivated")

    # ── Step 4: Successful login. ────────────────────────────────────
    user.last_login_at = datetime.now(timezone.utc)
    await reset_login_failures(payload.email)  # Clear the failure counter
    log_login_success(str(user.id), user.email, ip)
    return await _issue_token_response(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """
    Exchange a refresh token for a new access token + rotated refresh token.

    Single-use: presenting this token consumes it. The response contains the
    next refresh token to store. If Redis is unavailable, returns 503.
    """
    user_id = await consume_refresh_token(payload.refresh_token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token is invalid, expired, or already used",
        )

    result = await db.execute(select(User).where(User.id == uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User account is unavailable")

    return await _issue_token_response(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest) -> None:
    """
    Revoke the refresh token, invalidating the session.
    The access JWT remains valid until its natural expiry (max 60 min).
    Clients should discard it locally on logout.
    """
    await revoke_refresh_token(payload.refresh_token)
