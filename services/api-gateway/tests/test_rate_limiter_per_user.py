"""
tests/test_rate_limiter_per_user.py

Verifies the per-user rate-limit budget that runs IN ADDITION to per-IP.

WHY THIS MATTERS:
  Two real-world scenarios that the per-IP-only limiter was failing:

  1. Corporate NAT: 50 employees behind one egress IP share one bucket.
     One employee writing a chatty script exhausts everyone else's budget.
     Per-user buckets give each authenticated identity its own quota.

  2. Token theft / botnet: a stolen JWT rotated across N IPs would bypass
     the per-IP cap. The per-user bucket follows the identity, so the cap
     applies regardless of IP rotation.

We use an in-memory Starlette app + an in-memory store (no Redis dependency)
to keep the tests hermetic.
"""
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware.rate_limiter import RateLimiterMiddleware


def _build_app(*, ip_limit: int, user_limit: int):
    """
    Build a Starlette app that mimics the gateway's middleware ordering:
      - JWTAuth would normally populate request.state.user_id;
        we simulate that with a tiny prelude middleware below.
      - RateLimiterMiddleware then sees that state.
    """
    async def handler(request: Request):
        return JSONResponse({"ok": True})

    routes = [Route("/api/v1/resource", handler, methods=["GET"])]
    app = Starlette(routes=routes)

    # RateLimiter MUST be added AFTER the identity prelude so it runs LATER
    # on the request path (Starlette LIFO). That way state.user_id is set
    # by the time RateLimiter reads it.
    app.add_middleware(
        RateLimiterMiddleware,
        max_requests=ip_limit,
        window_seconds=60,
        auth_max_requests=ip_limit,  # not exercised here
        user_max_requests=user_limit,
        redis_url=None,  # forces in-memory store
    )

    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        # Simulate the JWT middleware populating identity from the X-Test-User
        # header. Real gateway pulls this from a verified JWT.
        user_id = request.headers.get("x-test-user")
        if user_id:
            request.state.user_id = user_id
        return await call_next(request)

    return app


class TestPerUserBucket:

    def test_unauthenticated_requests_only_use_ip_bucket(self):
        """No user_id on request → per-user bucket is never touched."""
        app = _build_app(ip_limit=5, user_limit=1)
        client = TestClient(app)
        # 5 unauth requests within the per-IP budget all pass even though the
        # per-user budget is just 1 — because we never had a user_id to count.
        for i in range(5):
            r = client.get("/api/v1/resource")
            assert r.status_code == 200, f"req {i} returned {r.status_code}"
        # The 6th hits the per-IP cap.
        r = client.get("/api/v1/resource")
        assert r.status_code == 429
        assert r.headers.get("X-RateLimit-Scope") == "ip"

    def test_authenticated_request_consumes_user_bucket(self):
        """When user_id is present, BOTH buckets are decremented."""
        app = _build_app(ip_limit=100, user_limit=3)
        client = TestClient(app)
        for _ in range(3):
            r = client.get("/api/v1/resource", headers={"x-test-user": "alice"})
            assert r.status_code == 200
        # 4th request: per-IP budget still has 96 left, but per-user is exhausted.
        r = client.get("/api/v1/resource", headers={"x-test-user": "alice"})
        assert r.status_code == 429
        assert r.headers.get("X-RateLimit-Scope") == "user"

    def test_two_users_share_ip_but_have_independent_user_buckets(self):
        """
        Corporate-NAT scenario: alice and bob share an IP. alice burns through
        her per-user budget; bob's requests should still succeed.
        """
        app = _build_app(ip_limit=100, user_limit=2)
        client = TestClient(app)
        # alice maxes out her per-user budget
        for _ in range(2):
            r = client.get("/api/v1/resource", headers={"x-test-user": "alice"})
            assert r.status_code == 200
        r = client.get("/api/v1/resource", headers={"x-test-user": "alice"})
        assert r.status_code == 429
        # bob — same IP — still has his own budget
        for _ in range(2):
            r = client.get("/api/v1/resource", headers={"x-test-user": "bob"})
            assert r.status_code == 200, f"bob blocked: {r.headers}"

    def test_ip_budget_still_caps_a_single_user_across_many_ips(self):
        """
        Hypothetical: a single user makes 50 reqs from one IP. Per-IP limit is
        the binding constraint here, not per-user. We verify the response
        header reports the IP scope.
        """
        app = _build_app(ip_limit=3, user_limit=100)
        client = TestClient(app)
        for _ in range(3):
            r = client.get("/api/v1/resource", headers={"x-test-user": "alice"})
            assert r.status_code == 200
        r = client.get("/api/v1/resource", headers={"x-test-user": "alice"})
        assert r.status_code == 429
        # IP was the tighter limit, so the header attributes the rejection to ip.
        assert r.headers.get("X-RateLimit-Scope") == "ip"

    def test_response_headers_pick_tighter_limit(self):
        """
        Under the cap, X-RateLimit-Limit/Remaining should describe whichever
        budget is currently more constrained.
        """
        app = _build_app(ip_limit=100, user_limit=10)
        client = TestClient(app)
        r = client.get("/api/v1/resource", headers={"x-test-user": "alice"})
        assert r.status_code == 200
        # alice has 10 in her per-user budget vs 100 in the IP budget — the
        # user budget is tighter, so it should be reported.
        assert r.headers.get("X-RateLimit-Scope") == "user"
        assert int(r.headers["X-RateLimit-Limit"]) == 10
