"""
user-service/app/audit.py

Structured audit logging for security-relevant events.

WHY THIS EXISTS:
  Standard Python logging goes to stdout as plaintext — great for debugging,
  terrible for security auditing. Azure Log Analytics ingests structured JSON
  logs from stdout and lets you write KQL queries like:

    AuditLog_CL
    | where event_type == "login_failure"
    | summarize failures = count() by email_s
    | where failures > 10

  This module is a thin wrapper that emits JSON-structured log lines. The key
  is that every security event (login attempt, failure, password change) is:
    1. Consistently structured (always has event_type, timestamp, user_id)
    2. Never contains secrets (no passwords, no tokens)
    3. Includes enough context for incident response (IP, user agent)

WHAT YOU'LL LEARN:
  - Structured logging patterns
  - The difference between application logs (debugging) and audit logs (security)
  - How to query audit logs in Azure Log Analytics (KQL)
"""
import json
import logging
import time

logger = logging.getLogger("audit")


def _emit(event_type: str, data: dict) -> None:
    """Emit a single structured audit event as JSON to stdout."""
    record = {
        "audit": True,
        "event_type": event_type,
        "timestamp_utc": time.time(),
        **data,
    }
    # Use logger.info so Azure Log Analytics / Container Insights picks it up.
    # We emit raw JSON — our logging config uses the message directly, so
    # json.dumps here means the record IS the JSON line.
    logger.info(json.dumps(record))


def log_login_success(user_id: str, email: str, ip: str | None = None) -> None:
    """Log a successful authentication event."""
    _emit("login_success", {
        "user_id": user_id,
        "email": email,
        "client_ip": ip,
    })


def log_login_failure(email: str, reason: str, ip: str | None = None) -> None:
    """Log a failed login attempt.

    SECURITY NOTE: Never log the attempted password — even hashed.
    Only log the email (for account lockout analysis) and a generic reason.
    """
    _emit("login_failure", {
        "email": email,
        "reason": reason,
        "client_ip": ip,
    })


def log_registration(user_id: str, email: str, ip: str | None = None) -> None:
    """Log a new user registration."""
    _emit("registration", {
        "user_id": user_id,
        "email": email,
        "client_ip": ip,
    })


def log_account_deactivated(user_id: str, target_user_id: str) -> None:
    """Log when an account is deactivated."""
    _emit("account_deactivated", {
        "actor_user_id": user_id,
        "target_user_id": target_user_id,
    })


def log_password_changed(user_id: str, ip: str | None = None) -> None:
    """Log a password change evewithout logging the new password!)."""
    _emit("password_changed", {
        "user_id": user_id,
        "client_ip": ip,
    })
