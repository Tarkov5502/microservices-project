"""User CRUD routes."""
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
    user = await _get_user_or_404(uuid.UUID(x_user_id), db)
    return UserResponse.model_validate(user)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(user_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> UserResponse:
    user = await _get_user_or_404(user_id, db)
    return UserResponse.model_validate(user)


@router.patch("/me", response_model=UserResponse)
async def update_me(
    payload: UserUpdate,
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    user = await _get_user_or_404(uuid.UUID(x_user_id), db)
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.password is not None:
        user.hashed_password = bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt(12)).decode()
    await db.flush()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_me(
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete: deactivate rather than destroying data."""
    user = await _get_user_or_404(uuid.UUID(x_user_id), db)
    user.is_active = False
    logger.info("User deactivated: %s", user.email)
