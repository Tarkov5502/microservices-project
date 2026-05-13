"""
tests/test_audit.py (user-service)

Tests for the audit logging module.

These tests verify that audit events are emitted with the correct structure
and that sensitive data (passwords, tokens) is never included.
"""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestAuditLogging:
    """Verify structured audit log output."""

    def _capture_log(self, fn, *args, **kwargs):
        """Run fn and capture what it passed to logger.info."""
        with patch("app.audit.logger") as mock_logger:
            fn(*args, **kwargs)
            assert mock_logger.info.called, "Audit function must call logger.info"
            raw = mock_logger.info.call_args[0][0]
            return json.loads(raw)

    def test_login_success_emits_correct_event_type(self):
        from app.audit import log_login_success
        record = self._capture_log(log_login_success, "user-123", "alice@example.com", "1.2.3.4")
        assert record["event_type"] == "login_success"

    def test_login_success_includes_user_id_and_email(self):
        from app.audit import log_login_success
        record = self._capture_log(log_login_success, "user-123", "alice@example.com", "1.2.3.4")
        assert record["user_id"] == "user-123"
        assert record["email"] == "alice@example.com"
        assert record["client_ip"] == "1.2.3.4"

    def test_login_success_never_includes_password(self):
        from app.audit import log_login_success
        record = self._capture_log(log_login_success, "user-123", "alice@example.com")
        record_str = json.dumps(record)
        assert "password" not in record_str.lower()
        assert "secret" not in record_str.lower()

    def test_login_failure_emits_reason(self):
        from app.audit import log_login_failure
        record = self._capture_log(log_login_failure, "bob@example.com", "invalid_credentials", "5.6.7.8")
        assert record["event_type"] == "login_failure"
        assert record["reason"] == "invalid_credentials"
        assert record["email"] == "bob@example.com"

    def test_registration_emits_user_id(self):
        from app.audit import log_registration
        record = self._capture_log(log_registration, "new-user-456", "carol@example.com")
        assert record["event_type"] == "registration"
        assert record["user_id"] == "new-user-456"

    def test_audit_is_flag_is_true(self):
        """The 'audit: true' field enables filtering in Azure Log Analytics."""
        from app.audit import log_login_success
        record = self._capture_log(log_login_success, "uid", "e@mail.com")
        assert record["audit"] is True

    def test_timestamp_is_present(self):
        from app.audit import log_login_success
        record = self._capture_log(log_login_success, "uid", "e@mail.com")
        assert "timestamp_utc" in record
        assert isinstance(record["timestamp_utc"], float)

    def test_optional_ip_defaults_to_none(self):
        from app.audit import log_login_success
        record = self._capture_log(log_login_success, "uid", "e@mail.com")  # no IP
        assert record["client_ip"] is None
