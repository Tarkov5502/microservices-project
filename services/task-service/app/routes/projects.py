"""task-service/app/routes/projects.py — Project CRUD."""
import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Project
from app.schemas import ProjectCreate, ProjectResponse

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_user_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-User-Id header — expected a UUID",
        )


async def _get_project_or_404(project_id: uuid.UUID, db: AsyncSession) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    project = Project(**payload.model_dump(), owner_id=_parse_user_id(x_user_id))
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)


@router.get("/", response_model=list[ProjectResponse])
async def list_projects(
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectResponse]:
    caller_id = _parse_user_id(x_user_id)
    result = await db.execute(
        select(Project)
        .where(Project.owner_id == caller_id)
        .where(Project.is_active == True)
        .order_by(Project.created_at.desc())
    )
    return [ProjectResponse.model_validate(p) for p in result.scalars().all()]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """
    Fetch a project by ID.

    SECURITY: Only the project owner can fetch it. Without this check, any
    authenticated user could enumerate all project UUIDs and read their details
    — classic Broken Object-Level Authorization (OWASP API1).
    """
    caller_id = _parse_user_id(x_user_id)
    project = await _get_project_or_404(project_id, db)
    if project.owner_id != caller_id:
        # Return 404, not 403 — don't confirm the project exists to unauthorized callers.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return ProjectResponse.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> None:
    caller_id = _parse_user_id(x_user_id)
    project = await _get_project_or_404(project_id, db)
    if project.owner_id != caller_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the project owner")
    project.is_active = False   # Soft-delete
