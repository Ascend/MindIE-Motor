# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Unit tests for CircuitBreakerManager state machine."""

from motor.coordinator.domain.circuit_breaker import CircuitBreakerManager


class TestCircuitBreaker:
    """Core state machine: failure counting, tripping, success reset, recovery."""

    def test_state_defaults(self):
        """New instances are closed with zero counters."""
        cb = CircuitBreakerManager()
        assert cb.is_closed(1)
        assert not cb.is_open(1)

    def test_failure_count_no_trip(self):
        """Failures 1 and 2 do not trip the circuit."""
        cb = CircuitBreakerManager()
        assert cb.process_failure(1) == (False, 0)
        assert cb.process_failure(1) == (False, 0)
        assert cb.is_closed(1)

    def test_third_failure_trips(self):
        """Third consecutive failure trips with base timeout 30s."""
        cb = CircuitBreakerManager()
        cb.process_failure(1)
        cb.process_failure(1)
        should_trip, timeout = cb.process_failure(1)
        assert should_trip is True
        assert timeout == 30
        assert cb.is_open(1)

    def test_extra_failure_while_open_ignored(self):
        """Failures reported after trip are ignored (race window)."""
        cb = CircuitBreakerManager()
        for _ in range(3):
            cb.process_failure(1)
        assert cb.is_open(1)
        should_trip, timeout = cb.process_failure(1)
        assert should_trip is False
        assert timeout == 0

    def test_timeout_restarts_after_auto_recover(self):
        """auto_recover resets trip_count: the next trip restarts from the base timeout."""

        cb = CircuitBreakerManager()
        for _ in range(3):
            cb.process_failure(1)  # trip #1: 30s
        assert cb.get(1).trip_count == 1
        for _ in range(3):
            cb.process_probe_failure(1)  # within-episode extension
        assert cb.get(1).trip_count == 4
        assert cb.get(1).current_timeout == 240

        recovered = cb.auto_recover(1)
        assert recovered is True
        assert cb.get(1).trip_count == 0
        assert cb.get(1).current_timeout == 0

        # Next trip starts fresh from the base 30s.
        for _ in range(2):
            cb.process_failure(1)
        should_trip, timeout = cb.process_failure(1)
        assert should_trip is True
        assert timeout == 30

    def test_success_resets_counters(self):
        """Success on a closed circuit resets failure_count and trip_count."""
        cb = CircuitBreakerManager()
        cb.process_failure(1)
        cb.process_failure(1)
        recovered = cb.process_success(1)
        assert recovered is False
        # After reset, a new failure starts at 1
        assert cb.process_failure(1) == (False, 0)

    def test_success_early_recovery_from_open(self):
        """Success on an open circuit triggers early-recovery (OPEN→CLOSED)."""
        cb = CircuitBreakerManager()
        for _ in range(3):
            cb.process_failure(1)
        assert cb.is_open(1)
        recovered = cb.process_success(1)
        assert recovered is True
        assert cb.is_closed(1)
        assert cb.process_failure(1) == (False, 0)  # counter reset

    def test_probe_failure_extends_timeout(self):
        """Probe failure on an open circuit doubles the recovery timeout."""
        cb = CircuitBreakerManager()
        for _ in range(3):
            cb.process_failure(1)
        assert cb.is_open(1)
        assert cb.get(1).trip_count == 1

        timeout = cb.process_probe_failure(1)

        assert timeout == 60  # 30 * 2^1
        assert cb.is_open(1)  # stays blocked
        assert cb.get(1).trip_count == 2
        assert cb.get(1).current_timeout == 60

    def test_probe_failure_exponential_until_cap(self):
        """Repeated probe failures keep doubling until capped at 300s."""
        cb = CircuitBreakerManager()
        for _ in range(3):
            cb.process_failure(1)
        timeouts = [cb.process_probe_failure(1) for _ in range(5)]
        assert timeouts == [60, 120, 240, 300, 300]

    def test_probe_failure_when_closed_returns_none(self):
        """Probe failure on a closed or unknown instance is a no-op."""
        cb = CircuitBreakerManager()
        assert cb.process_probe_failure(1) is None  # unknown
        for _ in range(3):
            cb.process_failure(1)
        cb.auto_recover(1)
        assert cb.process_probe_failure(1) is None  # closed after recovery

    def test_auto_recover_closes_circuit(self):
        """auto_recover transitions OPEN→CLOSED and resets all counters."""
        cb = CircuitBreakerManager()
        for _ in range(3):
            cb.process_failure(1)
        assert cb.is_open(1)
        recovered = cb.auto_recover(1)
        assert recovered is True
        assert cb.is_closed(1)
        # failure_count and trip_count are both reset, starts fresh
        assert cb.get(1).trip_count == 0
        assert cb.get(1).current_timeout == 0
        assert cb.process_failure(1) == (False, 0)

    def test_auto_recover_when_closed_returns_false(self):
        """auto_recover on a closed circuit is a no-op."""
        cb = CircuitBreakerManager()
        assert cb.auto_recover(1) is False

    def test_clear_instance(self):
        """clear_instance removes the CB record for a specific instance."""
        cb = CircuitBreakerManager()
        cb.process_failure(1)
        assert cb.get(1) is not None
        cb.clear_instance(1)
        assert cb.get(1) is None

    def test_clear_all(self):
        """clear_all removes all CB records."""
        cb = CircuitBreakerManager()
        cb.process_failure(1)
        cb.process_failure(2)
        count = cb.clear_all()
        assert count == 2
        assert cb.get(1) is None
        assert cb.get(2) is None

    def test_clear_all_empty_returns_zero(self):
        """clear_all on an empty pool returns 0."""
        cb = CircuitBreakerManager()
        assert cb.clear_all() == 0
