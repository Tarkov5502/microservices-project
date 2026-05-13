"""
tests/test_correlation.py — Tests for the X-Request-ID correlation middleware.
"""
import re
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.correlation import CorrelationMiddleware, _sanitize_or_generate

UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(CorrelationMiddleware)

    # NB: `request: Request` MUST be annotated. Without the type annotation
    # FastAPI treats the parameter as a request-body field and returns 422
    # for any GET that doesn't include a body.
    @app.get("/ping")
    async def ping(request: Request):
        return {"request_id": request.state.request_id}

    return TestClient(app)


class TestCorrelationMiddleware:

    def test_generates_uuid_when_no_header_present(self, client):
        resp = client.get("/ping")
        assert resp.status_code == 200
        rid = resp.headers["x-request-id"]
        assert UUID4_RE.match(rid), f"Expected UUID4, got: {rid}"

    def test_preserves_valid_client_supplied_id(self, client):
        custom_id = "my-trace-abc123"
        resp = client.get("/ping", headers={"X-Request-ID": custom_id})
        assert resp.headers["x-request-id"] == custom_id

    def test_replaces_malicious_id_with_new_uuid(self, client):
        malicious = "valid\r\nX-Injected: evil"
        resp = client.get("/ping", headers={"X-Request-ID": malicious})
        rid = resp.headers["x-request-id"]
        assert "\r\n" not in rid
        assert UUID4_RE.match(rid)

    def test_request_state_contains_id(self, client):
        resp = client.get("/ping")
        body_id = resp.json()["request_id"]
        header_id = resp.headers["x-request-id"]
        assert body_id == header_id

    def test_id_returned_in_response_header_always(self, client):
        for _ in range(3):
            resp = client.get("/ping")
            assert "x-request-id" in resp.headers


class TestSanitizeOrGenerate:
    """Unit tests for the ID sanitisation helper."""

    def test_valid_alphanumeric_accepted(self):
        result = _sanitize_or_generate("abc-123-XYZ")
        assert result == "abc-123-XYZ"

    def test_valid_uuid4_accepted(self):
        uid = "550e8400-e29b-41d4-a716-446655440000"
        assert _sanitize_or_generate(uid) == uid

    def test_newline_injection_rejected(self):
        malicious = "id\r\nEvil: header"
        result = _sanitize_or_generate(malicious)
        assert UUID4_RE.match(result)

    def test_empty_string_generates_uuid(self):
        result = _sanitize_or_generate("")
        assert UUID4_RE.match(result)

    def test_none_generates_uuid(self):
        result = _sanitize_or_generate(None)
        assert UUID4_RE.match(result)

    def test_too_long_id_rejected(self):
        long_id = "a" * 200
        result = _sanitize_or_generate(long_id)
        assert UUID4_RE.match(result)
