"""
task-service/app/routes/tasks.py — Full CRUD for tasks + Service Bus event publishing.

Key design decisions fixed here:
  1. EVENT-AFTER-COMMIT: Events are published via FastAPI BackgroundTasks, which
     run AFTER the response is sent — meaning AFTER the DB session has committed.
     Previously events fired before commit, creating a dual-write inconsistency
     (event dispatched, then DB commit fails → phantom events with no matching data).

  2. SB CLIENT POOLING: A module-level lazy singleton sender replaces creating a
     fresh ServiceBusClient on every publish call. Creating a new TCP connection
     and authenticating per-event was a severe performance problem under any load.
"""
import json
import uuid
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Header, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from azure.servicebus.aio import ServiceBusClient, ServiceBusSender
from azure.servicebus import ServiceBusMessage

from app.database import get_db
from app.models import Task
from app.schemas import TaskCreate, TaskUpdate, TaskResponse, TaskStatus
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── Singleton Service Bus Sender ─────────────────────────────────────────────
# Re-using a single sender across all requests avoids the overhead of
# establishing a new AMQP connection + authenticating on every event publish.
# The sender is lazily created on first use and reused for the lifetime of
# the process. A lock guards against concurrent initialization races.
_sb_client: ServiceBusClient | None = None
_sb_sender: ServiceBusSender | None = None
_sb_lock = asyncio.Lock()


async def _get_sender() -> ServiceBusSender | None:
    """Return a cached ServiceBus sender, creating it if needed."""
    global _sb_client, _sb_sender
    if not settings.servicebus_connection_string:
        return None
    if _sb_sender is not None:
        return _sb_sender
    async with _sb_lock:
        # Double-checked locking: another coroutine may have initialised while
        # we were waiting on the lock.
        if _sb_sender is None:
            _sb_client = ServiceBusClient.from_connection_string(
                settings.servicebus_connection_string
            )
            _sb_sender = _sb_client.get_topic_sender(
                topic_name=settings.servicebus_topic_tasks
            )
    return _sb_sender


async def close_sender() -> None:
    """Call this on application shutdown to drain and close the sender."""
    global _sb_client, _sb_sender
    if _sb_sender:
        await _sb_sender.close()
        _sb_sender = None
    if _sb_client:
        await _sb_client.close()
        _sb_client = None


async def _publish_event(event_type: str, data: dict) -> None:
    """
    Publish a domain event to Azure Service Bus using the shared sender.

    This is called from BackgroundTasks so it runs AFTER the DB commit,
    eliminating the dual-write consistency risk.
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
        # Never let event publishing take down the API response.
        # Log for alerting but do not re-raise.
        logger.error("Failed to publish event %s: %s", event_type, exc)


async def _get_task_or_404(task_id: uuid.UUID, db: AsyncSession) -> Task:
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.post("/", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    background: BackgroundTasks,
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> TaskResponse:
    task = Task(**payload.model_dump(), creator_id=uuid.UUID(x_user_id))
    db.add(task)
    await db.flush()
    await db.refresh(task)

    # Snapshot values NOW (before session expires post-commit) so the
    # background task closure captures plain data, not lazy-loaded ORM attrs.
    event_data = {
        "task_id": str(task.id),
        "title": task.title,
        "project_id": str(task.project_id),
        "creator_id": str(task.creator_id),
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
    }
    # BackgroundTasks run after the response body is sent, which is after
    # get_db()'s finally block has committed the session. Safe ordering ✓
    background.add_task(_publish_event, "task.created", event_data)
    return TaskResponse.model_validate(task)


@router.get("/", response_model=list[TaskResponse])
async def list_tasks(
    project_id: uuid.UUID | None = Query(None),
    status_filter: TaskStatus | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
) -> list[TaskResponse]:
    # status_filter is now typed as TaskStatus | None — FastAPI validates and
    # rejects invalid values with a 422 instead of silently returning empty results.
    stmt = select(Task)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if status_filter is not None:
        stmt = stmt.where(Task.status == status_filter)
    result = await db.execute(stmt.order_by(Task.created_at.desc()).limit(100))
    return [TaskResponse.model_validate(t) for t in result.scalars().all()]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> TaskResponse:
    return TaskResponse.model_validate(await _get_task_or_404(task_id, db))


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    background: BackgroundTasks,
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> TaskResponse:
    task = await _get_task_or_404(task_id, db)
    old_status = task.status

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(task, field, value)
    await db.flush()
    await db.refresh(task)

    if payload.status is not None and payload.status != old_status:
        background.add_task(_publish_event, "task.status_changed", {
            "task_id": str(task.id),
            "old_status": old_status.value,
            "new_status": task.status.value,
            "updated_by": x_user_id,
        })
    return TaskResponse.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> None:
    task = await _get_task_or_404(task_id, db)
    task_id_str = str(task.id)
    await db.delete(task)
    # Event fires after commit — task is gone from DB before we announce it ✓
    background.add_task(_publish_event, "task.deleted", {"task_id": task_id_str})
