"""
task-service/app/routes/tasks.py — Full CRUD for tasks + Service Bus event publishing.

Key design decisions:
  1. AUTHORIZATION (BOLA): Every operation verifies the caller is the task creator
     or assignee. Without this, any authenticated user can read/modify/delete any
     other user's tasks — a textbook Broken Object-Level Authorization flaw.

  2. PROJECT MEMBERSHIP: create_task verifies the target project exists AND the
     caller is its owner before allowing task creation. Without this check, a user
     who knows (or guesses) a project UUID could create tasks inside another user's
     project.

  3. EVENT-AFTER-COMMIT: Events published via FastAPI BackgroundTasks run AFTER
     the response is sent, which is after get_db()'s finally block has committed.
     Events never fire before the DB transaction is durable.

  4. SB CLIENT POOLING: A module-level lazy singleton sender with double-checked
     locking avoids creating a new AMQP connection per event.

  5. PATCH SEMANTICS: Uses exclude_unset=True (not exclude_none) so clients can
     explicitly null-out nullable fields like assignee_id and description.

  6. IDEMPOTENCY: create_task accepts an optional Idempotency-Key header. On a
     duplicate request with the same key, the original 201 response is returned
     from Redis without re-executing the DB write. See app/idempotency.py.
"""
import json
import uuid
import logging
import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response, status, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession
from azure.servicebus.aio import ServiceBusClient, ServiceBusSender
from azure.servicebus import ServiceBusMessage

from app.database import get_db
from app.dependencies import CallerID
from app.idempotency import get_cached_response, cache_response
from app.models import Task, Project, TaskStatus
from app.schemas import TaskCreate, TaskUpdate, TaskResponse
from app.config import settings
from app.pagination import CursorPage, decode_cursor, make_cursor_page

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _get_task_or_404(task_id: uuid.UUID, db: AsyncSession) -> Task:
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


async def _get_authorized_task(
    task_id: uuid.UUID,
    caller_id: uuid.UUID,
    db: AsyncSession,
    *,
    require_owner: bool = False,
) -> Task:
    """
    Fetch a task the caller is allowed to see.

    - Visibility:  caller is the creator OR assignee.
    - Mutation:    require_owner=True additionally asserts caller is the creator.
    """
    task = await _get_task_or_404(task_id, db)

    caller_can_view = (task.creator_id == caller_id or task.assignee_id == caller_id)
    if not caller_can_view:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    if require_owner and task.creator_id != caller_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the task creator can perform this action",
        )
    return task


async def _get_owned_project(
    project_id: uuid.UUID, caller_id: uuid.UUID, db: AsyncSession
) -> Project:
    """
    Verify the project exists and belongs to the caller.

    FIX: Without this check, any user who knows (or guesses) a project UUID
    can create tasks inside it — a Broken Object Level Authorization flaw
    at the resource-creation level, not just the read/update level.
    """
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if project.owner_id != caller_id:
        # Return 404 not 403 — don't confirm the project exists to the caller.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


# ─── Singleton Service Bus Sender ─────────────────────────────────────────────

_sb_client: ServiceBusClient | None = None
_sb_sender: ServiceBusSender | None = None
_sb_lock = asyncio.Lock()


async def _get_sender() -> ServiceBusSender | None:
    global _sb_client, _sb_sender
    if not settings.servicebus_connection_string:
        return None
    if _sb_sender is not None:
        return _sb_sender
    async with _sb_lock:
        if _sb_sender is None:
            _sb_client = ServiceBusClient.from_connection_string(
                settings.servicebus_connection_string
            )
            _sb_sender = _sb_client.get_topic_sender(
                topic_name=settings.servicebus_topic_tasks
            )
    return _sb_sender


async def close_sender() -> None:
    """Drain and close the shared sender on shutdown."""
    global _sb_client, _sb_sender
    if _sb_sender:
        await _sb_sender.close()
        _sb_sender = None
    if _sb_client:
        await _sb_client.close()
        _sb_client = None


async def _publish_event(event_type: str, data: dict) -> None:
    """
    Publish a domain event. Called from BackgroundTasks so it runs after DB commit.
    Never raises — event publishing must never take down the API response.
    """
    try:
        sender = await _get_sender()
        if sender is None:
            logger.warning("Service Bus not configured — skipping event: %s", event_type)
            return
        message = ServiceBusMessage(
            body=json.dumps({"event_type": event_type, "data": data}),
            content_type="application/json",
            subject=event_type,
        )
        await sender.send_messages(message)
        logger.info("Published event: %s", event_type)
    except Exception as exc:
        logger.error("Failed to publish event %s: %s", event_type, exc)


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    background: BackgroundTasks,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(
        None,
        alias="Idempotency-Key",
        description=(
            "Optional UUID4 string. If provided, duplicate requests with the same key "
            "return the original 201 response from cache without creating a second task. "
            "Keys expire after 24 hours. Scoped per user — different users may reuse keys."
        ),
    ),
) -> Response:
    """
    Create a new task.

    IDEMPOTENCY:
      Supply an `Idempotency-Key: <uuid4>` header on the first request.
      If the request times out or the connection drops, retry with the SAME
      key. The server returns the original 201 response without creating a
      second task. Keys are valid for 24 hours.

      Client example:
        curl -X POST /api/v1/tasks \\
          -H 'Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000' \\
          -H 'Authorization: Bearer ...' \\
          -d '{...}'

      If you omit the header, the endpoint works normally but is NOT
      idempotent — retries may create duplicate tasks.
    """
    # ── Idempotency check ─────────────────────────────────────────────────
    if idempotency_key:
        cached = await get_cached_response(caller_id, idempotency_key)
        if cached is not None:
            cached_status, cached_body = cached
            logger.info(
                "Idempotency cache HIT for key %r (caller %s) — returning cached %d",
                idempotency_key, caller_id, cached_status,
            )
            # Return the exact same response as the original request.
            # The Idempotency-Key-Replay header signals to the client that
            # this is a replayed response, not a new creation.
            return JSONResponse(
                content=cached_body,
                status_code=cached_status,
                headers={"Idempotency-Key-Replay": "true"},
            )

    # ── Normal creation path ─────────────────────────────────────────────
    # FIX: Verify the project exists and the caller owns it before allowing
    # task creation. Without this, any user can create tasks in any project
    # by knowing or guessing the project UUID (BOLA at creation level).
    await _get_owned_project(payload.project_id, caller_id, db)

    task = Task(
        **payload.model_dump(),
        creator_id=caller_id,
        status=TaskStatus.TODO,
    )
    db.add(task)
    await db.flush()
    await db.refresh(task)

    response_data = TaskResponse.model_validate(task)
    response_body = response_data.model_dump(mode="json")

    event_data = {
        "task_id": str(task.id),
        "title": task.title,
        "project_id": str(task.project_id),
        "creator_id": str(task.creator_id),
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
    }
    background.add_task(_publish_event, "task.created", event_data)

    # ── Cache for idempotency replay ────────────────────────────────
    # Cache AFTER the response is built. If caching fails (Redis down), the
    # creation still succeeds. The client just won't get replay protection.
    if idempotency_key:
        background.add_task(
            cache_response,
            caller_id,
            idempotency_key,
            status.HTTP_201_CREATED,
            response_body,
        )

    return JSONResponse(
        content=response_body,
        status_code=status.HTTP_201_CREATED,
    )


@router.get("/", response_model=CursorPage[TaskResponse])
async def list_tasks(
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
    project_id: uuid.UUID | None = Query(None),
    status_filter: TaskStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200, description="Max tasks per page"),
    cursor: str | None = Query(None, description="Opaque cursor from previous page's next_cursor"),
) -> CursorPage[TaskResponse]:
    """
    List tasks visible to the caller (creator OR assignee) with cursor pagination.

    PAGINATION (cursor-based, not LIMIT/OFFSET):
      First page:  GET /api/v1/tasks?limit=50
      Next pages:  GET /api/v1/tasks?limit=50&cursor=<next_cursor from response>
      Last page:   next_cursor will be null in the response.

    WHY CURSOR INSTEAD OF OFFSET?
      Offset pagination has correctness issues: if a task is created between
      page 1 and page 2 fetches, page 2 returns a duplicate. If one is deleted,
      page 2 skips an item. Cursor pagination is immune to both defects.

    SECURITY: Scoped to tasks the caller created or is assigned to.
    """
    stmt = select(Task).where(
        or_(Task.creator_id == caller_id, Task.assignee_id == caller_id)
    )
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if status_filter is not None:
        stmt = stmt.where(Task.status == status_filter)
    if cursor:
        try:
            cursor_dt, cursor_id = decode_cursor(cursor)
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Invalid cursor value")
        # Keyset filter: items strictly before the cursor position in the sort order
        stmt = stmt.where(
            or_(
                Task.created_at < cursor_dt,
                (Task.created_at == cursor_dt) & (Task.id < cursor_id),
            )
        )

    # Fetch limit+1 to detect whether a next page exists without a COUNT query
    stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1)
    result = await db.execute(stmt)
    items = list(result.scalars().all())
    page_items, next_cursor = make_cursor_page(items, limit)
    return CursorPage[TaskResponse](
        items=[TaskResponse.model_validate(t) for t in page_items],
        next_cursor=next_cursor,
        count=len(page_items),
    )


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: uuid.UUID,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> TaskResponse:
    """Fetch a task. Only the creator or current assignee can view it."""
    task = await _get_authorized_task(task_id, caller_id, db)
    return TaskResponse.model_validate(task)


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    background: BackgroundTasks,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> TaskResponse:
    """Update a task. Only the creator may modify it."""
    task = await _get_authorized_task(task_id, caller_id, db, require_owner=True)
    old_status = task.status

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    await db.flush()
    await db.refresh(task)

    if payload.status is not None and task.status != old_status:
        background.add_task(_publish_event, "task.status_changed", {
            "task_id":    str(task.id),
            "old_status": old_status.value,
            "new_status": task.status.value,
            "updated_by": str(caller_id),
            # Include stakeholder IDs so the consumer can target SSE delivery
            # to the right users without a DB lookup.
            "creator_id":  str(task.creator_id),
            "assignee_id": str(task.assignee_id) if task.assignee_id else None,
        })
    return TaskResponse.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    background: BackgroundTasks,
    caller_id: CallerID,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a task. Only the creator may delete it."""
    task = await _get_authorized_task(task_id, caller_id, db, require_owner=True)
    # Capture stakeholder IDs before deletion — the ORM object won't be
    # readable after db.delete() executes.
    event_data = {
        "task_id":    str(task.id),
        "creator_id":  str(task.creator_id),
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
    }
    await db.delete(task)
    background.add_task(_publish_event, "task.deleted", event_data)
