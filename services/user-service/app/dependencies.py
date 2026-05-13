"""
user-service/app/dependencies.py

Shared FastAPI dependencies — identical pattern to task-service/app/dependencies.py.

CallerID:      Parse X-User-Id header into uuid.UUID.
RequireAdmin:  Raise 403 if the caller doesn't hold the 'admin' role.

WHY HEADER-BASED ROLE CHECKING?
  The gateway is the only process that issues and validates JWTs. It extracts
  the 'roles' claim from the verified token and injects it as X-User-Roles
  before forwarding the request. Internal services trust this header because:

  1. NetworkPolicy: only the gateway can reach internal services.
     No client can send requests directly to user-service.
  2. Header stripping: the gateway explicitly removes X-User-Roles from any
     client-supplied headers before setting its own (see proxy.py _GATEWAY_OWNED).

  If a request somehow bypasses the gateway and reaches user-service directly
  with a spoofed X-User-Roles header, the worst case is accessing admin endpoints
  from inside the cluster — which NetworkPolicy already prohibits.

SECURITY: Never re-validate the JWT here. Double-validation creates drift risk:
  if the gateway and service use different keys, tokens valid at one are rejected
  at the other. The single validation point is the gateway.
"""
import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status


def _parse_caller_id(
    x_user_id: str = Header(..., alias="X-User-Id"),
) -> uuid.UUID:
    """Parse X-User-Id injected by the gateway. Returns 400 on malformed input."""
    try:
        return uuid.UUID(x_user_id)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid X-User-Id header — expected a UUID",
        )


def _require_admin(
    x_user_roles: str = Header(default="", alias="X-User-Roles"),
) -> None:
    """
    Raise 403 if the caller doesn't have the 'admin' role.

    X-User-Roles is a comma-separated list injected by the gateway from the
    verified JWT 'roles' claim. Example header value: 'user' or 'admin,user'.
    """
    roles = {r.strip() for r in x_user_roles.split(",") if r.strip()}
    if "admin" not in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required for this operation",
        )


# Type aliases — declare in route signatures for clean, grep-able code.
CallerID     = Annotated[uuid.UUID, Depends(_parse_caller_id)]
RequireAdmin = Annotated[None, Depends(_require_admin)]
