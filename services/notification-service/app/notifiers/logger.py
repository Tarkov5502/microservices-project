"""
LogNotifier — a simple notifier that logs to stdout.

In a real system you'd swap this for email (SendGrid), push (Firebase),
Slack, etc. The consumer calls self.notifier.send() without caring HOW
the notification is delivered — classic Dependency Inversion.
"""
import logging

logger = logging.getLogger(__name__)


class LogNotifier:
    """Notifier that writes to the application log (great for dev/testing)."""

    async def send(self, recipient_id: str, subject: str, body: str) -> None:
        logger.info(
            "📬 NOTIFICATION → recipient=%s | subject=%s | body=%s",
            recipient_id,
            subject,
            body,
        )
