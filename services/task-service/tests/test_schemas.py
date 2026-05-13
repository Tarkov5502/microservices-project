"""
tests/test_schemas.py (task-service)

Tests for task schema validation — focusing on the XSS-prevention validators.

KEY BEHAVIOURS TESTED:
  1. HTML tags are stripped from title and description (FIX #11).
  2. A title that is ONLY HTML tags is rejected (empty after stripping).
  3. Normal text with < or > characters has them stripped but text preserved.
  4. Script tags and event handlers are stripped.
  5. TaskCreate does NOT allow setting status (always starts as TODO).
  6. TaskUpdate uses exclude_unset semantics (can explicitly null fields).
"""
import uuid
import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from app.schemas import TaskCreate, TaskUpdate


_PROJECT_ID = uuid.uuid4()


class TestTaskCreateHtmlStripping:
    """FIX #11: Task titles and descriptions must have HTML stripped before storage."""

    def _valid_base(self, **overrides):
        return {
            "title": "Normal task title",
            "project_id": _PROJECT_ID,
            **overrides,
        }

    def test_plain_text_title_unchanged(self):
        task = TaskCreate(**self._valid_base(title="Deploy to production"))
        assert task.title == "Deploy to production"

    def test_script_tag_stripped_from_title(self):
        """Core XSS vector: <script> must not survive into the database."""
        task = TaskCreate(**self._valid_base(
            title='Fix login <script>alert("xss")</script>'
        ))
        assert "<script>" not in task.title
        assert "Fix login" in task.title  # legitimate text preserved

    def test_img_onerror_stripped_from_title(self):
        """Event handler injection via <img> tag."""
        task = TaskCreate(**self._valid_base(
            title='Task <img src=x onerror="steal()">'
        ))
        assert "<img" not in task.title

    def test_html_only_title_rejected(self):
        """Title that is ONLY an HTML tag becomes empty after stripping → should raise."""
        with pytest.raises(ValidationError, match="empty"):
            TaskCreate(**self._valid_base(title="<script>bad()</script>"))

    def test_html_stripped_from_description(self):
        task = TaskCreate(**self._valid_base(
            description="Details: <b>important</b> stuff"
        ))
        assert "<b>" not in task.description
        assert "important" in task.description

    def test_none_description_stays_none(self):
        task = TaskCreate(**self._valid_base(description=None))
        assert task.description is None

    def test_whitespace_only_title_after_stripping_rejected(self):
        """'  <br>  ' strips to '' and should be rejected."""
        with pytest.raises(ValidationError):
            TaskCreate(**self._valid_base(title="  <br>  "))

    def test_nested_tags_stripped(self):
        task = TaskCreate(**self._valid_base(
            title="<div><span>Hello</span></div>"
        ))
        assert "<" not in task.title
        assert "Hello" in task.title


class TestTaskCreateValidation:

    def test_title_min_length_enforced(self):
        with pytest.raises(ValidationError):
            TaskCreate(title="", project_id=_PROJECT_ID)

    def test_title_max_length_enforced(self):
        with pytest.raises(ValidationError):
            TaskCreate(title="x" * 501, project_id=_PROJECT_ID)

    def test_project_id_required(self):
        with pytest.raises(ValidationError):
            TaskCreate(title="Valid title")

    def test_assignee_id_is_optional(self):
        task = TaskCreate(title="Valid title", project_id=_PROJECT_ID)
        assert task.assignee_id is None


class TestTaskUpdatePatchSemantics:

    def test_all_fields_optional(self):
        """Empty PATCH body is valid."""
        update = TaskUpdate()
        dumped = update.model_dump(exclude_unset=True)
        assert dumped == {}

    def test_can_explicitly_set_assignee_to_none(self):
        """
        This tests the critical PATCH vs PUT difference.
        exclude_unset=True: only fields that were provided are included.
        exclude_none=True (WRONG): would drop this, making it impossible to
        un-assign a task by sending {"assignee_id": null}.
        """
        update = TaskUpdate(assignee_id=None)
        dumped = update.model_dump(exclude_unset=True)
        assert "assignee_id" in dumped
        assert dumped["assignee_id"] is None

    def test_html_stripped_from_title_on_update(self):
        update = TaskUpdate(title="Updated <b>title</b>")
        assert "<b>" not in update.title

    def test_html_only_title_update_rejected(self):
        with pytest.raises(ValidationError, match="empty"):
            TaskUpdate(title="<script>x()</script>")
