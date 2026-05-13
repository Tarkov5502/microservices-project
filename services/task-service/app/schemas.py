"""task-service schemas."""
import uuid
from datetime import datetime
from pydantic import BaseModel, Field
from app.models import TaskStatus, TaskPriority


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    owner_id: uuid.UUID
    is_active: bool
    created_at: datetime
    model_config = {"from_attributes": True}


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    # SECURITY: status is NOT a client-settable field at creation time.
    # All tasks start as TODO regardless of client input. Allowing clients
    # to set status=DONE on creation bypasses any workflow enforcement.
    priority: TaskPriority = TaskPriority.MEDIUM
    project_id: uuid.UUID
    assignee_id: uuid.UUID | None = None
    due_date: datetime | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=500)
    description: str | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    assignee_id: uuid.UUID | None = None
    due_date: datetime | None = None

    # PATCH semantics: use model_dump(exclude_unset=True) NOT exclude_none=True.
    # exclude_none would silently drop intentional null values (e.g. clearing
    # an assignee or description), making it impossible to un-set nullable fields.


class TaskResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    status: TaskStatus
    priority: TaskPriority
    project_id: uuid.UUID
    creator_id: uuid.UUID
    assignee_id: uuid.UUID | None
    due_date: datetime | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}
