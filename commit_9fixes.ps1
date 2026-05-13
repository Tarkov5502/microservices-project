$git  = "C:\Users\s0e086y\AppData\Local\Programs\Git\cmd\git.exe"
$proj = "C:\Users\s0e086y\Documents\puppy_workspace\microservices-project"
Set-Location $proj

& $git config user.email "tarkov5502@github.com"
& $git config user.name  "Tarkov5502"

& $git add -A
Write-Host "=== Staged changes ===" -ForegroundColor Cyan
& $git status --short

$msg = @"
fix: all 9 bugs + gaps from senior engineer audit

=== CRITICAL BUGS (system unusable without these) ===

FIX 1 - Login deadlock: JWT middleware blocked every auth endpoint
  The middleware only exempted /health, /health/ready, /metrics.
  /api/v1/auth/login, /register, /refresh, /logout were NOT exempt.
  Every login attempt returned 401 because you need a token to get a token.
  Fixed: added exempt_prefixes=["/api/v1/auth/"] to JWTAuthMiddleware.
  _is_exempt() now checks both exact paths and startswith() prefixes.

FIX 2 - Notification service crashed on startup: missing routes/__init__.py
  app/routes/ directory had no __init__.py — Python couldn't import
  app.routes.stream as a package. Service exited with ModuleNotFoundError
  on every cold start. Created the missing __init__.py.

=== REAL FUNCTIONAL ISSUES ===

FIX 3 - SSE broadcast sent every event to every connected user (privacy bug)
  broadcaster.broadcast() pushed every task event into every client queue
  regardless of who the event was for. User A received User B's task events.
  Rewrote broadcaster.py: queues are now dict[user_id, dict[conn_id, Queue]].
  broadcast() takes target_user_ids=[...] and only delivers to those users.
  subscribe() registers under the caller's user_id bucket.
  consumer.py updated: each handler returns the correct target_user_ids list.
  Event payloads for status_changed and deleted now include creator_id and
  assignee_id so the consumer can target without a DB lookup.

FIX 4 - Cursor pagination lacked compound indexes (O(N) per page at scale)
  Keyset queries filtered on (creator_id) then sorted all N matching rows in
  memory before applying the (created_at, id) inequality. With 10k tasks,
  every paginated request scanned the whole table for that user.
  Added compound indexes:
    ix_tasks_creator_cursor  (creator_id, created_at, id)
    ix_tasks_assignee_cursor (aee_id, created_at, id)
    ix_projects_owner_active_cursor (owner_id, is_active, created_at, id)
  Added Alembic migration 002_compound_indexes.py with full rationale.
  PostgreSQL can now satisfy keyset pagination in O(log N + page_size).

=== SMALLER GAPS ===

FIX 5 - or_ imported inside conditional block in projects.py
  The import lived inside `if cursor:` — evaluated on every paginated request,
  penalising it with a module dict lookup. Moved to top-level imports.
  Rewrote projects.py cleanly while fixing the import.

FIX 6 - Rate limiter missing X-RateLimit-Reset header
  Clients received X-RateLimit-Limit and X-RateLimit-Remaining but no
  X-RateLimit-Reset. Without the reset timestamp, a 429'd client must guess
  backoff duration. Added X-RateLimit-Reset (Unix epoch: now + window_seconds)
  to ALL responses, including 429. Also ensured 429 includes all three
  X-RateLimit-* headers alongside the existing Retry-After header.

FIX 7 - No per-account brute force protection (only per-IP)
  The gateway rate-limits by IP. An attacker rotating IPs could attempt
  unlimited passwords against one email. Added per-account lockout to
  redis_client.py:
    is_account_locked(email)   -- checks counter >= LOCKOUT_THRESHOLD (10)
    record_login_failure(email) -- INCR + EXPIRE nx=True (fixed window, 15min)
    reset_login_failures(email) -- DELETE on successful login
  login route updated: checks lockout before bcrypt, increments on failure,
  resets on success. Returns the same generic 401 regardless of lock state
  (enumeration resistance). Runs full timing-safe path even when locked.

=== NEW FEATURES ===

FIX 8 - Zero unit tests for core logic modules
  Added 6 new test files covering the gaps:
    api-gateway/tests/test_auth_middleware_prefix_exemptions.py
      -- 11 tests for the login-deadlock fix (exempt_prefixes)
    api-gateway/tests/test_rate_limiter_reset_header.py
      -- 7 tests for X-RateLimit-Reset on success and 429 responses
    task-service/tests/test_pagination.py
      -- 19 tests for encode/decode/make_cursor_page (pure logic, no DB)
    notification-service/tests/test_broadcaster.py
      -- 10 tests for user-scoped SSE fan-out, cleanup, privacy isolation
    user-service/tests/test_brute_force.py
      -- 20 tests for is_account_locked / record_login_failure / reset
         with Redis mocks covering all graceful-degradation paths
    task-service/tests/test_idempotency.py
      -- 15 tests for the idempotency cache get/set/key-scoping/TTL
  Extended notification-service/tests/test_consumer.py with 7 new tests
  verifying target_user_ids return values and broadcaster call contract.

FIX 9 - task creation not idempotent (duplicate tasks on client retry)
  POST /api/v1/tasks had no replay protection. A client retry after timeout
  created a second task. Added Redis-backed idempotency to task-service:
    app/idempotency.py -- get_cached_response / cache_response
    Keys: idempotency:{caller_id}:{idempotency_key}, TTL 24h
    Scoped per caller -- no cross-user key collision possible
  create_task route: accepts optional Idempotency-Key header. Cache hit
  returns original 201 with Idempotency-Key-Replay: true header.
  Cache miss proceeds normally and caches the response via BackgroundTask.
  Graceful degradation: Redis down -> proceeds without protection (no 503).
  Added redis[asyncio]==5.0.4 to task-service/requirements.txt.
  Idempotency Redis client shut down cleanly in lifespan.
"@

& $git commit -m $msg

Write-Host ""
Write-Host "=== Recent commits ===" -ForegroundColor Cyan
& $git log --oneline -5

Write-Host ""
Write-Host "=== Pushing ===" -ForegroundColor Yellow
& $git push origin main
Write-Host "Done!" -ForegroundColor Green
