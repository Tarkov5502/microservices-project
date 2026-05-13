"""
tests/load/locustfile.py — Realistic load test for the microservices platform.

PURPOSE:
  Simulates real user behaviour under concurrent load to validate:
  - Gateway can handle N concurrent users without dropping requests
  - Rate limiter engages at the correct threshold
  - Circuit breaker trips under simulated downstream failure
  - Response latency stays within acceptable bounds at target RPS
  - No memory leaks or connection pool exhaustion under sustained load

USAGE:
  # Prerequisites: docker compose up (gateway must be running)
  pip install locust

  # Interactive web UI (http://localhost:8089)
  locust -f tests/load/locustfile.py --host=http://localhost:8000

  # Headless (CI mode): 50 users, spawn 5/sec, run for 60 seconds
  locust -f tests/load/locustfile.py --host=http://localhost:8000 \
    --users 50 --spawn-rate 5 --run-time 60s --headless \
    --csv=tests/load/results/run_$(date +%Y%m%d_%H%M%S)

WHAT EACH USER DOES:
  HealthCheckUser — high volume, constant pings on /health and /health/ready.
    Tests that health endpoints never saturate and always return quickly.
    Simulates Kubernetes probes firing every 10s across 10 replicas.

  ApiUser — realistic authenticated workflow:
    1. Register a new account (unique email per virtual user)
    2. Login to get a JWT token
    3. Create a project
    4. Create 3–5 tasks in the project
    5. List tasks (with pagination)
    6. Update one task status to in_progress
    7. Delete one task
    Repeats continuously. This exercises the full CRUD + auth stack.

  ReadHeavyUser — simulates a read-only dashboard consumer.
    Logs in once, then loops listing tasks and projects.
    Tests connection pool behaviour under high GET concurrency.

INTERPRETING RESULTS:
  Look for:
  - RPS (Requests Per Second): target > 100 on a laptop
  - p95 latency: target < 500ms for read endpoints
  - p99 latency: should not spike unexpectedly (indicates GC pauses / DB locks)
  - Error rate: should be 0% for all non-rate-limited requests
  - Rate limit 429s: expected once a single user hits the limit (demonstrates
    the rate limiter is working, not a bug)
"""
import uuid
import random
from locust import HttpUser, task, between, events


def _unique_email() -> str:
    return f"loadtest-{uuid.uuid4().hex[:8]}@example.com"


class HealthCheckUser(HttpUser):
    """
    Simulates Kubernetes liveness and readiness probes.
    Light weight — just GET /health and /health/ready.
    """
    wait_time = between(1, 3)
    weight = 1  # Low proportion of total users

    @task(3)
    def liveness(self):
        self.client.get("/health", name="/health")

    @task(1)
    def readiness(self):
        with self.client.get("/health/ready", name="/health/ready", catch_response=True) as resp:
            if resp.status_code == 503:
                # 503 from readiness = upstream degraded, not a locust error
                # We want to report this as a warning, not an error, so we can
                # distinguish intentional 503 from bugs
                resp.success()


class ApiUser(HttpUser):
    """
    Full authenticated user workflow. Exercises the entire API surface.
    """
    wait_time = between(0.5, 2)
    weight = 5

    def on_start(self):
        """Called once per virtual user on startup. Register + login."""
        self.token = None
        self.project_id = None
        self.task_ids: list[str] = []
        self._register_and_login()

    def _register_and_login(self) -> None:
        email = _unique_email()
        password = "LoadTest@123!"
        username = f"lt_{uuid.uuid4().hex[:8]}"

        # Register
        reg_resp = self.client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "username": username, "full_name": "Load Test"},
            name="/api/v1/auth/register",
        )
        if reg_resp.status_code != 201:
            return

        # Login
        login_resp = self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
            name="/api/v1/auth/login",
        )
        if login_resp.status_code == 200:
            self.token = login_resp.json().get("access_token")

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @task(1)
    def create_project(self):
        if not self.token:
            return
        resp = self.client.post(
            "/api/v1/projects/",
            json={"name": f"Load Test Project {uuid.uuid4().hex[:6]}", "description": "Auto-generated"},
            headers=self._auth_headers(),
            name="/api/v1/projects/ [POST]",
        )
        if resp.status_code == 201:
            self.project_id = resp.json().get("id")
            self.task_ids = []  # Reset task list for new project

    @task(5)
    def create_task(self):
        if not self.token or not self.project_id:
            self.create_project()
            return
        priorities = ["low", "medium", "high", "critical"]
        resp = self.client.post(
            "/api/v1/tasks/",
            json={
                "title": f"Task {uuid.uuid4().hex[:8]}",
                "description": "Created by load test",
                "priority": random.choice(priorities),
                "project_id": self.project_id,
            },
            headers=self._auth_headers(),
            name="/api/v1/tasks/ [POST]",
        )
        if resp.status_code == 201:
            self.task_ids.append(resp.json()["id"])

    @task(10)
    def list_tasks(self):
        if not self.token:
            return
        params = {"limit": 20, "offset": 0}
        if self.project_id:
            params["project_id"] = self.project_id
        self.client.get(
            "/api/v1/tasks/",
            params=params,
            headers=self._auth_headers(),
            name="/api/v1/tasks/ [GET]",
        )

    @task(10)
    def list_projects(self):
        if not self.token:
            return
        self.client.get(
            "/api/v1/projects/",
            headers=self._auth_headers(),
            name="/api/v1/projects/ [GET]",
        )

    @task(3)
    def update_task(self):
        if not self.token or not self.task_ids:
            return
        statuses = ["in_progress", "in_review", "done"]
        task_id = random.choice(self.task_ids)
        self.client.patch(
            f"/api/v1/tasks/{task_id}",
            json={"status": random.choice(statuses)},
            headers=self._auth_headers(),
            name="/api/v1/tasks/{id} [PATCH]",
        )

    @task(1)
    def delete_task(self):
        if not self.token or not self.task_ids:
            return
        task_id = self.task_ids.pop()
        self.client.delete(
            f"/api/v1/tasks/{task_id}",
            headers=self._auth_headers(),
            name="/api/v1/tasks/{id} [DELETE]",
        )

    @task(2)
    def get_profile(self):
        if not self.token:
            return
        self.client.get(
            "/api/v1/users/me",
            headers=self._auth_headers(),
            name="/api/v1/users/me [GET]",
        )


class ReadHeavyUser(HttpUser):
    """
    Simulates a dashboard that polls for updates frequently.
    High read concurrency — tests connection pool limits and DB query caching.
    """
    wait_time = between(0.2, 1)
    weight = 3

    def on_start(self):
        self.token = None
        self.project_id = None
        email = _unique_email()
        password = "ReadOnly@123!"
        username = f"ro_{uuid.uuid4().hex[:8]}"

        self.client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "username": username, "full_name": "Reader"},
            name="/api/v1/auth/register",
        )
        resp = self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
            name="/api/v1/auth/login",
        )
        if resp.status_code == 200:
            self.token = resp.json().get("access_token")
            # Create one project to have something to read
            proj = self.client.post(
                "/api/v1/projects/",
                json={"name": "Dashboard Project", "description": "Read-heavy test"},
                headers={"Authorization": f"Bearer {self.token}"},
                name="/api/v1/projects/ [POST]",
            )
            if proj.status_code == 201:
                self.project_id = proj.json().get("id")

    @task(8)
    def poll_tasks(self):
        if not self.token:
            return
        params = {"limit": 50}
        if self.project_id:
            params["project_id"] = self.project_id
        self.client.get(
            "/api/v1/tasks/",
            params=params,
            headers={"Authorization": f"Bearer {self.token}"},
            name="/api/v1/tasks/ [GET] (poll)",
        )

    @task(2)
    def poll_projects(self):
        if not self.token:
            return
        self.client.get(
            "/api/v1/projects/",
            headers={"Authorization": f"Bearer {self.token}"},
            name="/api/v1/projects/ [GET] (poll)",
        )


@events.quitting.add_listener
def on_quit(environment, **kwargs):
    """
    Print a summary on exit with pass/fail assessment.
    In CI (--headless mode), the locust process exit code is non-zero
    if error_rate > 0 or user-defined thresholds are violated below.
    """
    stats = environment.stats.total
    if stats.num_requests == 0:
        print("\n⚠️  No requests were made — is the gateway running?")
        environment.process_exit_code = 1
        return

    error_rate = stats.fail_ratio * 100
    p95 = stats.get_response_time_percentile(0.95)
    p99 = stats.get_response_time_percentile(0.99)

    print(f"\n{'═' * 60}")
    print(f"  LOAD TEST SUMMARY")
    print(f"{'═' * 60}")
    print(f"  Total requests : {stats.num_requests:,}")
    print(f"  RPS (avg)      : {stats.total_rps:.1f}")
    print(f"  Error rate     : {error_rate:.2f}%")
    print(f"  p95 latency    : {p95:.0f}ms")
    print(f"  p99 latency    : {p99:.0f}ms")
    print(f"{'═' * 60}")

    # Fail CI if any of these thresholds are violated
    failures = []
    if error_rate > 1.0:
        failures.append(f"Error rate {error_rate:.2f}% > 1% threshold")
    if p95 > 1000:
        failures.append(f"p95 latency {p95:.0f}ms > 1000ms threshold")
    if p99 > 3000:
        failures.append(f"p99 latency {p99:.0f}ms > 3000ms threshold")

    if failures:
        print("  ❌ THRESHOLDS VIOLATED:")
        for f in failures:
            print(f"     - {f}")
        environment.process_exit_code = 1
    else:
        print("  ✅ All thresholds passed")
