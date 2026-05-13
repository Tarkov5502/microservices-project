"""
Pydantic schemas — define the shape of API request/response bodies.

Schemas ≠ Models:
  - Models (models.py) = database table structure
  - Schemas (here)     = API input/output contract

Using separate schemas gives us flexibility: e.g., never return
hashed_password in responses, even though it's in the DB model.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


class UserCreate(BaseModel):
    """What the client sends when registering."""
    email: EmailStr
    username: str = Field(min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = Field(min_length=8, max_length=100)
    full_name: str | None = Field(None, max_length=255)

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        """Enforce basic password complexity."""
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class UserResponse(BaseModel):
    """What we return to clients — note: NO password field!"""
    id: uuid.UUID
    email: str
    username: str
    full_name: str | None
    is_active: bool
    # is_admin is intentionally excluded from the public response schema.
    # Exposing it lets any authenticated user enumerate who is an admin,
    # which aids privilege escalation planning.
    created_at: datetime
    last_login_at: datetime | None

    model_config = {"from_attributes": True}  # Allows creating from SQLAlchemy model


class AdminUserResponse(UserResponse):
    """Extended response including is_admin — only returned to admin callers."""
    is_admin: bool


class UserUpdate(BaseModel):
    """Partial update — all fields optional."""
    full_name: str | None = Field(None, max_length=255)
    # Apply the SAME password complexity rules as UserCreate. Skipping them here
    # would allow users to downgrade to weak passwords via the update endpoint.
    password: str | None = Field(None, min_length=8, max_length=100)

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int   # seconds until expiry
    user: UserResponse
