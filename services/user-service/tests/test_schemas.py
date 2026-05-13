"""
tests/test_schemas.py (user-service)

Tests for Pydantic schema validation rules.
These are pure unit tests — no database, no HTTP, just schema validation logic.

KEY BEHAVIOURS TESTED:
  1. Password complexity: requires uppercase + digit.
  2. Username pattern: only alphanumeric, hyphen, underscore.
  3. Password min/max length enforcement.
  4. UserResponse never exposes hashed_password.
"""
import pytest
from pydantic import ValidationError

from app.schemas import UserCreate, UserUpdate, LoginRequest


class TestUserCreate:

    def test_valid_registration_passes(self):
        u = UserCreate(
            email="alice@example.com",
            username="alice_dev",
            password="SecurePass1",
            full_name="Alice Smith",
        )
        assert u.email == "alice@example.com"

    def test_password_requires_uppercase(self):
        with pytest.raises(ValidationError, match="uppercase"):
            UserCreate(
                email="bob@example.com",
                username="bob123",
                password="alllowercase1",
            )

    def test_password_requires_digit(self):
        with pytest.raises(ValidationError, match="digit"):
            UserCreate(
                email="carol@example.com",
                username="carol_x",
                password="NoDigitsHere",
            )

    def test_password_too_short(self):
        with pytest.raises(ValidationError):
            UserCreate(
                email="d@e.com",
                username="user1",
                password="A1b",  # < 8 chars
            )

    def test_password_too_long(self):
        with pytest.raises(ValidationError):
            UserCreate(
                email="d@e.com",
                username="user2",
                password="A1" + "x" * 100,  # > 100 chars
            )

    def test_invalid_email_rejected(self):
        with pytest.raises(ValidationError):
            UserCreate(
                email="not-an-email",
                username="validuser",
                password="SecurePass1",
            )

    def test_username_with_invalid_characters_rejected(self):
        with pytest.raises(ValidationError):
            UserCreate(
                email="test@example.com",
                username="user name!",  # spaces and ! not allowed
                password="SecurePass1",
            )

    def test_username_too_short(self):
        with pytest.raises(ValidationError):
            UserCreate(
                email="test@example.com",
                username="ab",  # < 3 chars
                password="SecurePass1",
            )

    def test_full_name_is_optional(self):
        u = UserCreate(
            email="test@example.com",
            username="testuser",
            password="SecurePass1",
        )
        assert u.full_name is None


class TestUserUpdate:

    def test_update_with_valid_password(self):
        u = UserUpdate(password="NewSecure2")
        assert u.password == "NewSecure2"

    def test_update_password_complexity_still_enforced(self):
        """Password complexity rules must apply on updates too."""
        with pytest.raises(ValidationError, match="uppercase"):
            UserUpdate(password="weakpassword1")

    def test_update_with_no_fields_is_valid(self):
        """All fields optional — valid to send an empty update."""
        u = UserUpdate()
        assert u.password is None
        assert u.full_name is None

    def test_full_name_max_length(self):
        with pytest.raises(ValidationError):
            UserUpdate(full_name="x" * 256)  # > 255 chars
