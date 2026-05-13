"""
Authentication routes: register and login.
"""
import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
_DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=12)).decode()


def _hash_password(plain: str) -> str:
    """bcrypt hash — each call generates a unique salt."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


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


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: UserCreate, db: AsyncSession = Depends(get_db)) -> UserResponse:
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
        hashed_password=_hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(user)
    await db.flush()   # Get the generated ID without committing
    await db.refresh(user)
    logger.info("New user registered: %s", user.email)
    return UserResponse.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """Authenticate and receive a JWT."""
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    # SECURITY: Always run bcrypt regardless of whether the user exists.
    # Short-circuiting when user is None leaks valid email addresses through
    # measurably different response times (timing attack / user enumeration).
    candidate_hash = user.hashed_password if user else _DUMMY_HASH
    password_ok = _verify_password(payload.password, candidate_hash)

    if not user or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account deactivated")

    user.last_login_at = datetime.now(timezone.utc)
    token, expires_in = _create_jwt(user)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserResponse.model_validate(user),
    )
