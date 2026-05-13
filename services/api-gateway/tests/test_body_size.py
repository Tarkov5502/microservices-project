"""
tests/test_body_size.py

Tests for BodySizeLimitMiddleware. Verifies both enforcement paths:
  1. Declared Content-Length above the limit → 413 before any byte is read.
  2. Streaming body without Content-Length → 413 as the byte count crosses
     the threshold mid-read.
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware.body_size import BodySizeLimitMiddleware


@pytest.fixture
def client():
    app = FastAPI()
    # Tiny limit (100 bytes) keeps the tests fast.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=100)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"received": len(body)}

    return TestClient(app)


class TestContentLengthPath:

    def test_under_limit_passes(self, client):
        resp = client.post("/echo", content=b"a" * 50)
        assert resp.status_code == 200
        assert resp.json() == {"received": 50}

    def test_at_limit_passes(self, client):
        resp = client.post("/echo", content=b"a" * 100)
        assert resp.status_code == 200

    def test_over_limit_rejected(self, client):
        resp = client.post("/echo", content=b"a" * 101)
        assert resp.status_code == 413
        assert "exceeds" in resp.json()["detail"]

    def test_far_over_limit_rejected(self, client):
        resp = client.post("/echo", content=b"a" * 10_000)
        assert resp.status_code == 413


class TestStreamingPath:
    """
    Simulate a chunked-transfer request by sending a generator. TestClient
    omits Content-Length when the body is a generator/iterator.
    """

    def test_streaming_under_limit_passes(self, client):
        def gen():
            yield b"a" * 30
            yield b"a" * 30

        resp = client.post("/echo", content=gen())
        assert resp.status_code == 200

    def test_streaming_over_limit_rejected(self, client):
        def gen():
            yield b"a" * 60
            yield b"a" * 60  # cumulative 120 > 100

        resp = client.post("/echo", content=gen())
        assert resp.status_code == 413


class TestNonBodyMethods:
    """GET requests must not be blocked by this middleware."""

    def test_get_with_no_body_passes(self):
        app = FastAPI()
        app.add_middleware(BodySizeLimitMiddleware, max_bytes=10)

        @app.get("/hello")
        async def hello():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/hello")
        assert resp.status_code == 200


class TestConfiguration:

    def test_zero_limit_raises_at_construction(self):
        app = FastAPI()
        with pytest.raises(ValueError):
            BodySizeLimitMiddleware(app, max_bytes=0)

    def test_negative_limit_raises_at_construction(self):
        app = FastAPI()
        with pytest.raises(ValueError):
            BodySizeLimitMiddleware(app, max_bytes=-1)
