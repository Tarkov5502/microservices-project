"""
tests/test_circuit_breaker.py — Unit tests for the circuit breaker state machine.

These tests verify the CLOSED → OPEN → HALF_OPEN → CLOSED transition logic
without making any real network calls. The circuit breaker is pure Python state
machine logic — no mocking needed.
"""
import time
import pytest
from app.circuit_breaker import CircuitBreaker, CircuitState, CircuitBreakerRegistry


class TestCircuitBreakerStateMachine:
    """Verify the three-state transition logic."""

    def test_initial_state_is_closed(self):
        cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=30.0)
        assert cb.state == CircuitState.CLOSED
        assert not cb.is_open()

    def test_single_failure_stays_closed_below_threshold(self):
        cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=30.0)
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=30.0)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.is_open()

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=30.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()  # Reset
        cb.record_failure()
        cb.record_failure()
        # Only 2 failures since last success — still closed
        assert cb.state == CircuitState.CLOSED

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=0.05)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        time.sleep(0.1)  # Wait for recovery timeout
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allow_probe()

    def test_half_open_success_closes_circuit(self):
        cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=0.05)
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=0.05)
        for _ in range(3):
            cb.record_failure()
        time.sleep(0.1)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_open_not_half_open_before_timeout(self):
        cb = CircuitBreaker("svc", failure_threshold=3, recovery_timeout=60.0)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        # No sleep — recovery timeout hasn't elapsed
        assert not cb.allow_probe()

    def test_recovery_resets_failure_count(self):
        cb = CircuitBreaker("svc", failure_threshold=2, recovery_timeout=0.05)
        cb.record_failure()
        cb.record_failure()  # Opens
        time.sleep(0.1)
        cb.record_success()  # Closes via half-open probe
        # Now one failure should NOT reopen (failure count was reset)
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED


class TestCircuitBreakerRegistry:
    """Verify lazy creation and state snapshot."""

    def test_returns_same_instance_for_same_name(self):
        reg = CircuitBreakerRegistry()
        cb1 = reg.get("user-service")
        cb2 = reg.get("user-service")
        assert cb1 is cb2

    def test_returns_different_instances_for_different_names(self):
        reg = CircuitBreakerRegistry()
        cb1 = reg.get("user-service")
        cb2 = reg.get("task-service")
        assert cb1 is not cb2

    def test_all_states_returns_current_snapshot(self):
        reg = CircuitBreakerRegistry()
        reg.get("user-service")  # Create
        states = reg.all_states()
        assert "user-service" in states
        assert states["user-service"] == "CLOSED"

    def test_all_states_reflects_open_circuit(self):
        reg = CircuitBreakerRegistry()
        cb = reg.get("broken-service")
        for _ in range(5):
            cb.record_failure()
        states = reg.all_states()
        assert states["broken-service"] == "OPEN"
