"""
api-gateway/app/circuit_breaker.py

Per-upstream circuit breaker with three-state machine.

WHY A CIRCUIT BREAKER?
  When user-service crashes, the gateway without a circuit breaker will:
  1. Accept every incoming request
  2. Open a new connection to user-service
  3. Wait for the timeout (30s by default)
  4. Return 503 to the client after 30s

  With 100 concurrent users, that's 100 threads blocked for 30s each, holding
  file descriptors and memory. The gateway itself slows down or runs out of
  resources — a *cascading* failure that takes down healthy services too.

  The circuit breaker short-circuits this. After N consecutive failures, it
  opens — and immediately returns 503 without touching the network. Clients
  fail fast (milliseconds, not seconds). Healthy services stay healthy.
  After a cooling-off period, it enters HALF_OPEN and sends one probe request.
  Success → CLOSED (normal). Failure → back to OPEN.

STATE MACHINE:
  CLOSED ──[N failures]──► OPEN ──[timeout elapsed]──► HALF_OPEN
    ▲                                                        │
    └────────────────────[probe success]────────────────────┘
    HALF_OPEN ──[probe failure]──► OPEN

THREAD SAFETY:
  The gateway runs as a single asyncio event loop (workers=1). State mutations
  happen only in the dispatch coroutine, which is never concurrent with itself
  for the same coroutine context. No locks needed — asyncio cooperative
  multitasking gives us safe sequential access.

  If you ever switch to multi-worker mode (workers > 1), each worker process
  has its own Python interpreter and memory space — circuit state won't be
  shared. You'd need Redis to share breaker state across workers, similar to
  the rate limiter pattern.
"""
import time
import logging
from enum import Enum, auto

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = auto()    # Normal: requests flow through
    OPEN = auto()      # Failing fast: reject without network call
    HALF_OPEN = auto() # Testing recovery: one probe request allowed


class CircuitBreaker:
    """
    Per-upstream circuit breaker.

    Create one instance per upstream service URL and store in a registry.
    Calling is_open() before a request and record_success()/record_failure()
    after tells the breaker what happened.
    """

    def __init__(
        self,
        service_name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ) -> None:
        self.service_name = service_name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None

    @property
    def state(self) -> CircuitState:
        """Return current state, transitioning OPEN → HALF_OPEN if timeout elapsed."""
        if (
            self._state == CircuitState.OPEN
            and self._last_failure_time is not None
            and time.monotonic() - self._last_failure_time >= self.recovery_timeout
        ):
            logger.info(
                "Circuit breaker [%s]: OPEN → HALF_OPEN (recovery window elapsed)",
                self.service_name,
            )
            self._state = CircuitState.HALF_OPEN
        return self._state

    def is_open(self) -> bool:
        """Return True if the circuit is OPEN (requests should be rejected immediately)."""
        return self.state == CircuitState.OPEN

    def allow_probe(self) -> bool:
        """Return True if the circuit is HALF_OPEN (one probe request allowed)."""
        return self.state == CircuitState.HALF_OPEN

    def record_success(self) -> None:
        """Record a successful request. Resets failure count; closes circuit."""
        if self._state != CircuitState.CLOSED:
            logger.info(
                "Circuit breaker [%s]: %s → CLOSED (probe succeeded)",
                self.service_name,
                self._state.name,
            )
        self._failure_count = 0
        self._last_failure_time = None
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """
        Record a failed request.
        Opens circuit after failure_threshold consecutive failures.
        In HALF_OPEN, any failure immediately reopens the circuit.
        """
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            logger.warning(
                "Circuit breaker [%s]: HALF_OPEN → OPEN (probe failed)",
                self.service_name,
            )
            self._state = CircuitState.OPEN
        elif self._failure_count >= self.failure_threshold:
            logger.warning(
                "Circuit breaker [%s]: CLOSED → OPEN (%d consecutive failures)",
                self.service_name,
                self._failure_count,
            )
            self._state = CircuitState.OPEN


class CircuitBreakerRegistry:
    """
    Module-level registry of breakers keyed by upstream service name.
    Call get() to retrieve (or lazily create) a breaker for a given service.
    """

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, service_name: str) -> CircuitBreaker:
        if service_name not in self._breakers:
            self._breakers[service_name] = CircuitBreaker(service_name)
        return self._breakers[service_name]

    def all_states(self) -> dict[str, str]:
        """Return a snapshot of all breaker states — useful for health endpoints."""
        return {name: cb.state.name for name, cb in self._breakers.items()}


# Module-level singleton — imported by proxy.py
registry = CircuitBreakerRegistry()
