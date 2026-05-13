"""task-service/app/routes/projects.py — Project CRUD."""
import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import CallerID
from app.models import Project
from app.schemas import ProjectCreate, ProjectResponse
from app.pagination import CursorPage, decode_cursor, make_cursor_page

logger = logging.getLogger(__name__)
router = APIRouter()


async def _get_project_or_404(project_id: uuid.UUID, db: AsyncSession) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    project = Project(**payload.model_dump(), owner_id=caller_id)
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return ProjectResponse.model_validate(project)


@router.get("/", response_model=CursorPage[ProjectResponse])
async def list_projects(
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None, description="Opaque cursor from previous page's next_cursor"),
) -> CursorPage[ProjectResponse]:
    """
    List the caller's active projects, newest first, with cursor pagination.

    The query is backed by ix_projects_owner_active_cursor
    (owner_id, is_active, created_at, id), so fetching any page costs
    O(log N + page_size) regardless of how many projects exist.
    """
    stmt = (
        select(Project)
        .where(Project.owner_id == caller_id)
        .where(Project.is_active == True)  # noqa: E712 — SQLAlchemy needs == not is
    )
    if cursor:
        try:
            cursor_dt, cursor_id = decode_cursor(cursor)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid cursor value",
            )
        # Keyset filter: items strictly before the cursor in (created_at DESC, id DESC) order
        stmt = stmt.where(
            or_(
                Project.created_at < cursor_dt,
                (Project.created_at == cursor_dt) & (Project.id < cursor_id),
            )
        )

    stmt = stmt.order_by(Project.created_at.desc(), Project.id.desc()).limit(limit + 1)
    result = await db.execute(stmt)
    items = list(result.scalars().all())
    page_items, next_cursor = make_cursor_page(items, limit)
    return CursorPage[ProjectResponse](
        items=[ProjectResponse.model_validate(p) for p in page_items],
        next_cursor=next_cursor,
        count=len(page_items),
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """
    Fetch a project by ID.

    SECURITY: Only the project owner can fetch it. Without this check, any
    authenticated user could enumerate all project UUIDs and read their details
    — classic Broken Object-Level Authorization (OWASP API1).
    Return 404 (not 403) so the existence of the project is not confirmed to
    an unauthorised caller.
    """
    project = await _get_project_or_404(project_id, db)
    if project.owner_id != caller_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return ProjectResponse.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Soft-delete a project (sets is_active=False).

    Tasks cascade via the ORM relationship — they are not deleted from the DB,
    but the project becomes invisible to list_projects queries.
    """
    project = await _get_project_or_404(project_id, db)
    if project.owner_id != caller_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not the project owner")
    project.is_active = False  # Soft-delete — preserves audit history
