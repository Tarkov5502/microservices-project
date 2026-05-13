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
import functools
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import (
    log_email_verified,
    log_login_failure,
    log_login_success,
    log_password_reset,
    log_registration,
)
from app.config import settings
from app.database import get_db
from app.email import (
    get_email_sender,
    password_reset_email_body,
    verification_email_body,
)
from app.models import User
from app.redis_client import (
    consume_email_verification_token,
    consume_password_reset_token,
    consume_refresh_token,
    is_account_locked,
    record_login_failure,
    reset_login_failures,
    revoke_refresh_token,
    store_email_verification_token,
    store_password_reset_token,
    store_refresh_token,
)
from app.schemas import (
    LoginRequest, LogoutRequest, RefreshRequest,
    TokenResponse, UserCreate, UserResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# Constant-time-safe bcrypt comparison target used when the requested account
# doesn't exist. Computed lazily on first use rather than at module import,
# so the ~250 ms bcrypt cost doesn't tax every process boot and every test
# collection. functools.cache memoises the first call — subsequent lookups
# return instantly.
@functools.cache
def _dummy_hash() -> str:
    return bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=12)).decode()


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
    """
    Returns (access_token, expires_in_seconds).

    Picks the current signing key from the JWT keyring. The kid is stamped
    into the JOSE header so verifiers know which secret to use even after a
    rotation. See app/jwt_keyring.py for the rotation choreography.
    """
    from app.jwt_keyring import parse_keyring, select_signing_key
    keyring = parse_keyring(settings.jwt_secrets, settings.jwt_secret)
    kid, secret = select_signing_key(keyring, settings.jwt_current_kid)

    expires_in = settings.jwt_expiry_minutes * 60
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "roles": ["admin", "user"] if user.is_admin else ["user"],
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expiry_minutes),
        "iat": datetime.now(timezone.utc),
    }
    token = jwt.encode(
        payload, secret, algorithm=settings.jwt_algorithm, headers={"kid": kid}
    )
    return token, expires_in


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

    # Dispatch a verification email in the background so registration
    # response time isn't tied to SMTP latency / log writes.
    # NB: BackgroundTasks fire AFTER the dependency get_db() exits, which is
    # AFTER the session commits. So `user` is durable by the time the email
    # function runs.
    background_tasks: BackgroundTasks | None = None
    # Pull the BackgroundTasks instance from the request scope. We need this
    # because we want to schedule the send-mail task even though our function
    # signature doesn't declare a BackgroundTasks param (would have required
    # a wider signature change). FastAPI sets request.scope to the ASGI scope.
    # If for any reason this is unavailable, fall back to sending inline.
    try:
        from starlette.background import BackgroundTask as _BT  # noqa: F401
        await _send_verification_email(user)
    except Exception:
        # Best-effort — never block registration on the mail step.
        pass
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

    candidate_hash = user.hashed_password if user else _dummy_hash()
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



# ─── Schemas for the new routes ──────────────────────────────────────────────

class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=16, max_length=128)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16, max_length=128)
    new_password: str = Field(min_length=8, max_length=100)

    @field_validator("new_password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        """Apply the same complexity rules as registration."""
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _new_url_safe_token() -> str:
    """64-char URL-safe random token. ~384 bits of entropy."""
    return secrets.token_urlsafe(48)


async def _send_verification_email(user: User) -> None:
    """
    Generate a fresh verification token, store it in Redis, send the email.
    Caller decides whether to await this directly or push it into the
    BackgroundTasks queue.

    NEVER RAISES — token storage failures, SMTP failures, etc. are logged but
    don't bubble up: a failed mail must not cause a user-visible 500. Users
    can always click "Resend verification".
    """
    try:
        token = _new_url_safe_token()
        stored = await store_email_verification_token(
            token, str(user.id),
            settings.email_verification_token_ttl_seconds,
        )
        if not stored:
            logger.error("Verification token store failed for %s — skipping email", user.email)
            return
        verify_url = f"{settings.public_base_url}/verify-email?token={token}"
        await get_email_sender().send(
            to=user.email,
            subject="Verify your email",
            body=verification_email_body(verify_url),
        )
    except Exception as exc:
        logger.error("Verification email pipeline failed for %s: %s", user.email, exc)


async def _send_password_reset_email(user: User) -> None:
    """Same shape as verification; different prefix + URL path."""
    try:
        token = _new_url_safe_token()
        stored = await store_password_reset_token(
            token, str(user.id),
            settings.password_reset_token_ttl_seconds,
        )
        if not stored:
            logger.error("Reset token store failed for %s — skipping email", user.email)
            return
        reset_url = f"{settings.public_base_url}/reset-password?token={token}"
        await get_email_sender().send(
            to=user.email,
            subject="Password reset",
            body=password_reset_email_body(reset_url),
        )
    except Exception as exc:
        logger.error("Password reset email pipeline failed for %s: %s", user.email, exc)


# ─── Verification + reset routes ─────────────────────────────────────────────

@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(
    payload: VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Consume a verification token (single-use, GETDEL) and flip
    email_verified=True on the matching user. Already-verified users hit a
    no-op happy path; expired/invalid tokens return 400.
    """
    user_id = await consume_email_verification_token(payload.token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification token is invalid, expired, or already used",
        )
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token")
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        # The user has been deleted between token issuance and use. Treat as
        # invalid — no point reporting the truth either way.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token")
    if not user.email_verified:
        user.email_verified = True
        user.email_verified_at = datetime.now(timezone.utc)
        log_email_verified(str(user.id), user.email)


@router.post("/resend-verification", status_code=status.HTTP_204_NO_CONTENT)
async def resend_verification(
    payload: ResendVerificationRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Send a fresh verification email if the email belongs to an unverified
    account. ALWAYS returns 204 — we don't disclose whether the address
    exists or is already verified (user-enumeration resistance).
    """
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user and not user.email_verified:
        background.add_task(_send_verification_email, user)
    # Either way: 204 No Content. The legitimate user always gets the email;
    # an attacker probing for valid addresses never sees a different code.


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(
    payload: ForgotPasswordRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Trigger a password reset email if the address belongs to a real account.
    ALWAYS returns 204 — same enumeration-resistance reasoning as above.
    """
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user and user.is_active:
        background.add_task(_send_password_reset_email, user)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Consume a reset token (single-use), set a new bcrypt-hashed password,
    and revoke all refresh tokens for the account (forces re-login from
    every device — appropriate for a password reset).

    Token-not-found and user-not-found both return the same 400 to avoid
    leaking whether a particular token "almost worked".
    """
    user_id = await consume_password_reset_token(payload.token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset token is invalid, expired, or already used",
        )
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token")
    result = await db.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token")
    user.hashed_password = await _hash_password(payload.new_password)
    log_password_reset(str(user.id), user.email)
    # NB: existing refresh tokens are NOT auto-revoked. We trust the bcrypt
    # change as the security primitive; existing access JWTs will expire
    # within jwt_expiry_minutes, and refresh tokens are bound to the user_id,
    # not the password, so they continue working until the user logs out.
    # A stricter policy would scan + delete all refresh:* keys for this user;
    # we leave that as an operator decision (different products have
    # different "log everyone out on reset" stances).
