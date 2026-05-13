"""
tests/test_security_headers.py (user-service)

Verifies the shared SecurityHeadersMiddleware adds defence-in-depth headers
to backend responses. Identical assertions hold for task-service and
notification-service — the middleware module is intentionally duplicated to
keep each service's container image self-contained.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security_headers import SecurityHeadersMiddleware


def _make_app():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return TestClient(app)


class TestSecurityHeaders:

    def test_x_content_type_options_nosniff(self):
        r = _make_app().get("/ping")
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_x_frame_options_deny(self):
        r = _make_app().get("/ping")
        assert r.headers["X-Frame-Options"] == "DENY"

    def test_csp_is_restrictive(self):
        r = _make_app().get("/ping")
        csp = r.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_referrer_policy_strict_origin(self):
        r = _make_app().get("/ping")
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"

    def test_xss_protection_disabled(self):
        """The legacy XSS filter is unsafe; we explicitly set it to 0."""
        r = _make_app().get("/ping")
        assert r.headers["X-XSS-Protection"] == "0"
