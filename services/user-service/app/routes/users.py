"""
user-service/app/routes/users.py — User profile + admin management routes.

ADMIN ENDPOINTS:
  All routes prefixed with /admin/ require the 'admin' role (enforced via
  RequireAdmin dependency). Regular users attempting these receive 403.

  Admin capabilities:
    GET  /admin/users              — paginated list of all users
    GET  /admin/users/{id}         — full profile including is_admin flag
    POST /admin/users/{id}/promote — grant admin role
    POST /admin/users/{id}/demote  — revoke admin role
    DELETE /admin/users/{id}       — hard delete (permanent, with audit log)

USER ENDPOINTS (self-service):
    GET   /me       — own profile
    PATCH /me       — update own name/password
    DELETE /me      — soft-deactivate own account
"""
import asyncio
import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CallerID, RequireAdmin
from app.models import User
from app.schemas import AdminUserResponse, UserResponse, UserUpdate
from app import audit
import bcrypt

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Shared helpers ───────────────────────────────────────────────────────────

async def _get_user_or_404(user_id: uuid.UUID, db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


# ─── Self-service routes ──────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse)
async def get_me(caller_id: CallerID, db: AsyncSession = Depends(get_db)) -> UserResponse:
    """Return the currently authenticated user's profile."""
    user = await _get_user_or_404(caller_id, db)
    return UserResponse.model_validate(user)


@router.patch("/me", response_model=UserResponse)
async def update_me(
    payload: UserUpdate,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    user = await _get_user_or_404(caller_id, db)
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.password is not None:
        user.hashed_password = await asyncio.to_thread(
            lambda: bcrypt.hashpw(payload.password.encode(), bcrypt.gensalt(rounds=12)).decode()
        )
        audit.log_password_changed(str(user.id))
    await db.flush()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_me(caller_id: CallerID, db: AsyncSession = Depends(get_db)) -> None:
    """Soft-delete: deactivate rather than destroying data."""
    user = await _get_user_or_404(caller_id, db)
    user.is_active = False
    logger.info("User self-deactivated: %s", user.email)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Fetch a user's public profile. Accessible to any authenticated user."""
    user = await _get_user_or_404(user_id, db)
    return UserResponse.model_validate(user)


# ─── Admin-only routes ────────────────────────────────────────────────────────

@router.get("/admin/users", response_model=dict)
async def admin_list_users(
    _: RequireAdmin,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    active_only: bool = Query(False),
) -> dict:
    """
    [ADMIN] List all users with pagination.
    Returns full profiles including is_admin flag via AdminUserResponse.
    """
    stmt = select(User)
    if active_only:
        stmt = stmt.where(User.is_active == True)
    count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = count_result.scalar_one()

    result = await db.execute(
        stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
    )
    users = [AdminUserResponse.model_validate(u) for u in result.scalars().all()]
    logger.info("Admin %s listed %d users", caller_id, len(users))
    return {"total": total, "limit": limit, "offset": offset, "users": users}


@router.get("/admin/users/{user_id}", response_model=AdminUserResponse)
async def admin_get_user(
    user_id: uuid.UUID,
    _: RequireAdmin,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> AdminUserResponse:
    """[ADMIN] Fetch full user profile including is_admin flag."""
    user = await _get_user_or_404(user_id, db)
    return AdminUserResponse.model_validate(user)


@router.post("/admin/users/{user_id}/promote", response_model=AdminUserResponse)
async def admin_promote_user(
    user_id: uuid.UUID,
    _: RequireAdmin,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> AdminUserResponse:
    """[ADMIN] Grant admin role to a user."""
    user = await _get_user_or_404(user_id, db)
    if user.is_admin:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="User is already an admin")
    user.is_admin = True
    await db.flush()
    await db.refresh(user)
    logger.info("Admin %s promoted user %s to admin", caller_id, user_id)
    return AdminUserResponse.model_validate(user)


@router.post("/admin/users/{user_id}/demote", response_model=AdminUserResponse)
async def admin_demote_user(
    user_id: uuid.UUID,
    _: RequireAdmin,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> AdminUserResponse:
    """[ADMIN] Revoke admin role from a user. An admin cannot demote themselves."""
    if user_id == caller_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Admins cannot demote themselves")
    user = await _get_user_or_404(user_id, db)
    user.is_admin = False
    await db.flush()
    await db.refresh(user)
    logger.info("Admin %s demoted user %s", caller_id, user_id)
    return AdminUserResponse.model_validate(user)


@router.delete("/admin/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_user(
    user_id: uuid.UUID,
    _: RequireAdmin,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    [ADMIN] Hard delete a user. Irreversible — use with caution.
    Admins cannot delete themselves.
    """
    if user_id == caller_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Admins cannot delete their own account via admin endpoint")
    user = await _get_user_or_404(user_id, db)
    logger.warning("Admin %s hard-deleted user %s (%s)", caller_id, user_id, user.email)
    await db.delete(user)
