"""
task-service/app/routes/tasks.py — Full CRUD for tasks + Service Bus event publishing.
"""
import json
import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException, status, Header, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusMessage

from app.database import get_db
from app.models import Task
from app.schemas import TaskCreate, TaskUpdate, TaskResponse
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


async def _publish_event(event_type: str, data: dict) -> None:
    """
    Publish a domain event to Azure Service Bus.

    This is the event-driven pattern: task-service doesn't call
    notification-service directly. It publishes an event and lets
    interested services subscribe to it — loose coupling!
    """
    try:
        async with ServiceBusClient.from_connection_string(settings.servicebus_connection_string) as client:
            async with client.get_topic_sender(settings.servicebus_topic_tasks) as sender:
                message = ServiceBusMessage(
                    body=json.dumps({"event_type": event_type, "data": data}),
                    content_type="application/json",
                    subject=event_type,
                )
                await sender.send_messages(message)
                logger.info("Published event: %s", event_type)
    except Exception as exc:
        # Don't fail the API call if event publishing fails — log and continue
        logger.error("Failed to publish event %s: %s", event_type, exc)


async def _get_task_or_404(task_id: uuid.UUID, db: AsyncSession) -> Task:
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.post("/", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> TaskResponse:
    task = Task(**payload.model_dump(), creator_id=uuid.UUID(x_user_id))
    db.add(task)
    await db.flush()
    await db.refresh(task)

    await _publish_event("task.created", {
        "task_id": str(task.id),
        "title": task.title,
        "project_id": str(task.project_id),
        "creator_id": str(task.creator_id),
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
    })
    return TaskResponse.model_validate(task)


@router.get("/", response_model=list[TaskResponse])
async def list_tasks(
    project_id: uuid.UUID | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
) -> list[TaskResponse]:
    stmt = select(Task)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    if status_filter:
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
    x_user_id: str = Header(..., alias="X-User-Id"),
    db: AsyncSession = Depends(get_db),
) -> TaskResponse:
    task = await _get_task_or_404(task_id, db)
    old_status = task.status

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(task, field, value)
    await db.flush()
    await db.refresh(task)

    if payload.status and payload.status != old_status:
        await _publish_event("task.status_changed", {
            "task_id": str(task.id),
            "old_status": old_status,
            "new_status": task.status,
            "updated_by": x_user_id,
        })
    return TaskResponse.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    task = await _get_task_or_404(task_id, db)
    await db.delete(task)
    await _publish_event("task.deleted", {"task_id": str(task_id)})
