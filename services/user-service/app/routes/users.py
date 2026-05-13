"""
User CRUD routes.

FIX: password hashing in update_me was synchronous (bcrypt.hashpw is CPU-bound,
~300ms per call with rounds=12). Running it on the asyncio event loop stalls
ALL concurrent requests for its entire duration. asyncio.to_thread() offloads
the blocking call to a thread-pool worker.
"""
import asyncio
import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.models import User
from app.schemas import UserResponse, UserUpdate
import bcrypt

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_user_id(raw: str) -> uuid.UUID:
    """
    Parse the X-User-Id header value into a UUID.
    Returns 400 instead of an unhandled 500 on malformed input.
    The API Gateway always sends a valid UUID from the verified JWT 'sub' claim,
    but defensive parsing prevents crashes during manual testing or misconfiguration.
    """
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-User-Id header — expected a UUID",
        )


async def _get_user_or_404(user_id: uuid.UUID, db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.get("/me", response_model=UserResponse)
async def get_me(
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Return the currently authenticated user (ID injected by API Gateway)."""
    user = await _get_user_or_404(_parse_user_id(x_user_id), db)
    return UserResponse.model_validate(user)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """
    Fetch another user's public profile.

    SECURITY: Only allow users to look up themselves, or allow the requester
    if they have admin role. Since the task service needs to look up assignees,
    this endpoint is intentionally kept accessible to any authenticated user,
    but the response schema excludes is_admin and hashed_password.

    In a stricter system, add a role check here:
        if str(user_id) != x_user_id and "admin" not in roles:
            raise 403
    The tradeoff is registered here for the learner's awareness.
    """
    user = await _get_user_or_404(user_id, db)
    return UserResponse.model_validate(user)


@router.patch("/me", response_model=UserResponse)
async def update_me(
    payload: UserUpdate,
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    user = await _get_user_or_404(_parse_user_id(x_user_id), db)
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.password is not None:
        # FIX: use asyncio.to_thread so bcrypt doesn't block the event loop.
        # See auth.py for the full explanation of why this matters.
        user.hashed_password = await asyncio.to_thread(
            lambda: bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt(rounds=12)).decode()
        )
    await db.flush()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_me(
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete: deactivate rather than destroying data."""
    user = await _get_user_or_404(_parse_user_id(x_user_id), db)
    user.is_active = False
    logger.info("User deactivated: %s", user.email)
