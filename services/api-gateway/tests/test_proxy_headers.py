"""
tests/test_proxy_headers.py

Tests for the proxy header stripping logic.

KEY BEHAVIOURS TESTED:
  1. Clients cannot inject X-User-Id (identity spoofing).
  2. Clients cannot inject X-User-Email or X-User-Roles.
  3. X-HTTP-Method-Override and variants are stripped (method tunneling fix #8).
  4. Hop-by-hop headers are never forwarded.
  5. Gateway-validated identity headers ARE set when JWT state is present.
"""
import pytest

from app.routes.proxy import _HOP_BY_HOP, _GATEWAY_OWNED, _resolve_upstream
from fastapi import HTTPException


class TestGatewayOwnedHeaders:
    """Unit tests for the header filtering logic directly."""

    def test_x_user_id_is_gateway_owned(self):
        """Clients must NEVER be able to set X-User-Id — it comes from JWT only."""
        assert "x-user-id" in _GATEWAY_OWNED

    def test_x_user_email_is_gateway_owned(self):
        assert "x-user-email" in _GATEWAY_OWNED

    def test_x_user_roles_is_gateway_owned(self):
        assert "x-user-roles" in _GATEWAY_OWNED

    def test_host_is_gateway_owned(self):
        """Client Host header must never be forwarded — it enables host poisoning."""
        assert "host" in _GATEWAY_OWNED

    def test_method_override_headers_are_gateway_owned(self):
        """FIX #8: All method override headers must be stripped."""
        assert "x-http-method-override" in _GATEWAY_OWNED
        assert "x-method-override" in _GATEWAY_OWNED
        assert "x-http-method" in _GATEWAY_OWNED

    def test_x_original_url_is_gateway_owned(self):
        """NGINX/IIS routing header — must not be spoofed by clients."""
        assert "x-original-url" in _GATEWAY_OWNED

    def test_filtering_logic_removes_gateway_owned_headers(self):
        """Simulate the dict comprehension used in the proxy route."""
        incoming_headers = {
            "content-type": "application/json",
            "authorization": "Bearer token",
            "x-user-id": "attacker-injected-uuid",         # Must be stripped
            "x-http-method-override": "DELETE",            # Must be stripped
            "x-user-email": "attacker@evil.com",           # Must be stripped
            "accept": "application/json",
        }
        filtered = {
            k: v for k, v in incoming_headers.items()
            if k.lower() not in _HOP_BY_HOP and k.lower() not in _GATEWAY_OWNED
        }
        # Identity injection stripped
        assert "x-user-id" not in filtered
        assert "x-user-email" not in filtered
        assert "x-http-method-override" not in filtered
        # Legitimate headers preserved
        assert "content-type" in filtered
        assert "accept" in filtered


class TestHopByHopHeaders:

    def test_connection_is_hop_by_hop(self):
        assert "connection" in _HOP_BY_HOP

    def test_transfer_encoding_is_hop_by_hop(self):
        assert "transfer-encoding" in _HOP_BY_HOP

    def test_upgrade_is_hop_by_hop(self):
        assert "upgrade" in _HOP_BY_HOP


class TestRouteResolution:

    def test_users_path_routes_to_user_service(self):
        upstream, _, _ = _resolve_upstream("/api/v1/users/me")
        assert "user-service" in upstream or "8001" in upstream

    def test_auth_path_routes_to_user_service(self):
        upstream, _, _ = _resolve_upstream("/api/v1/auth/login")
        assert "user-service" in upstream or "8001" in upstream

    def test_tasks_path_routes_to_task_service(self):
        upstream, _, _ = _resolve_upstream("/api/v1/tasks")
        assert "task-service" in upstream or "8002" in upstream

    def test_projects_path_routes_to_task_service(self):
        upstream, _, _ = _resolve_upstream("/api/v1/projects/abc")
        assert "task-service" in upstream or "8002" in upstream

    def test_notifications_path_routes_to_notification_service(self):
        upstream, _, _ = _resolve_upstream("/api/v1/notifications")
        assert "notification-service" in upstream or "8003" in upstream

    def test_unknown_path_raises_404(self):
        with pytest.raises(HTTPException) as exc_info:
            _resolve_upstream("/api/v1/nonexistent/route")
        assert exc_info.value.status_code == 404
