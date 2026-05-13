"""
user-service/app/email.py — Pluggable email sender abstraction.

WHY AN ABSTRACTION:
  - Local development and CI must work with zero external dependencies. The
    "log" sender just prints the email body to stdout so you can grab the
    verification/reset link from the container logs.
  - Production wires the same interface to SMTP (SendGrid, Mailgun, Azure
    Communication Services). The application code never changes — only the
    config does.

SECURITY NOTES:
  - We do NOT log full secrets (token values). The full link IS the secret —
    anyone with read access to logs in dev can use the token. That's
    deliberately the trade-off for the no-setup dev flow. In production the
    log sender should NOT be enabled.
  - SMTP credentials live in env / Key Vault, never in the repo.

USAGE:
  from app.email import get_email_sender
  await get_email_sender().send(
      to="user@example.com",
      subject="Verify your email",
      body="Click here: https://app.example.com/verify?token=...",
  )
"""
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from typing import Protocol

from app.config import settings

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class _LogEmailSender:
    """
    Dev/CI sender: writes the email content to the application logger and
    exits. The full body (including any token-bearing URL) appears in the
    logs so a developer can copy the link from `docker compose logs`.
    """

    async def send(self, to: str, subject: str, body: str) -> None:
        # Use a clearly-marked marker so it's grep-able in container logs.
        logger.warning(
            "─── DEV EMAIL ────────────────────────────────────────────────\n"
            "TO:      %s\n"
            "SUBJECT: %s\n"
            "BODY:\n%s\n"
            "──────────────────────────────────────────────────────────────",
            to, subject, body,
        )


class _SmtpEmailSender:
    """
    Production sender: SMTP with STARTTLS.

    Reads the following from settings:
      smtp_host, smtp_port, smtp_username, smtp_password, smtp_from_address
    """

    async def send(self, to: str, subject: str, body: str) -> None:
        # smtplib is sync. For the small volume of transactional mail this
        # service sends (verification + reset on user actions), wrapping it
        # in run_in_executor would be cleaner but the simpler synchronous
        # call here keeps the surface area minimal and obvious. If volume
        # ever exceeds ~1 send/sec this should move to a queue + worker.
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from_address
        msg["To"] = to

        ctx = ssl.create_default_context()
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as s:
                s.starttls(context=ctx)
                if settings.smtp_username:
                    s.login(settings.smtp_username, settings.smtp_password)
                s.send_message(msg)
            logger.info("SMTP email sent to %s subject=%r", to, subject)
        except Exception as exc:
            # Never propagate — a failed mail must not 500 the user request.
            # We log loudly so an operator notices the outage; the caller's
            # business logic (e.g. user registration) succeeds regardless.
            logger.error("SMTP send failed (to=%s subject=%r): %s", to, subject, exc)


_sender: EmailSender | None = None


def get_email_sender() -> EmailSender:
    """
    Return the configured email sender singleton, building it on first call.
    """
    global _sender
    if _sender is None:
        if settings.email_sender == "smtp":
            _sender = _SmtpEmailSender()
        else:
            _sender = _LogEmailSender()
        logger.info("Email sender initialised: %s", settings.email_sender)
    return _sender


# ─── Email templates ─────────────────────────────────────────────────────────

def verification_email_body(verify_url: str) -> str:
    return (
        "Welcome to the platform!\n\n"
        "Please confirm your email address by clicking the link below.\n"
        f"This link is single-use and expires in 24 hours:\n\n{verify_url}\n\n"
        "If you didn't sign up, you can safely ignore this email."
    )


def password_reset_email_body(reset_url: str) -> str:
    return (
        "We received a request to reset your password.\n\n"
        f"Use the link below within 1 hour to choose a new password:\n\n{reset_url}\n\n"
        "If you didn't request this, ignore this email — your account is safe."
    )
