"""
task-service/app/dependencies.py

Shared FastAPI dependencies used across multiple route modules.

WHY A DEPENDENCIES FILE?
  _parse_user_id() was copy-pasted into tasks.py, projects.py, and any future
  route file. That's 3 places to update if the logic changes (e.g. if we switch
  from UUID to ULID). A shared dependency is defined once and declared anywhere.

  FastAPI Depends() is also testable: inject a different function in tests
  without modifying the route under test.
"""
import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status


def _parse_caller_id(
    x_user_id: str = Header(..., alias="X-User-Id"),
) -> uuid.UUID:
    """
    Parse the X-User-Id header injected by the API Gateway.
    Returns 400 (not 500) on malformed input — defensive parsing.
    The gateway always injects a valid UUID from the verified JWT sub claim,
    but this guard prevents crashes during misconfiguration or direct access.
    """
    try:
        return uuid.UUID(x_user_id)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-User-Id header — expected a UUID",
        )


# Annotated type alias: declare caller_id: CallerID in any route signature.
# Identical to `Annotated[uuid.UUID, Depends(_parse_caller_id)]` inline,
# but far easier to read and grep for.
CallerID = Annotated[uuid.UUID, Depends(_parse_caller_id)]
