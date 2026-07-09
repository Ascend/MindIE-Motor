# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Circuit breaker state per Instance, managed by SchedulerServer."""

from __future__ import annotations
from dataclasses import dataclass
from motor.common.logger import get_logger

logger = get_logger(__name__)

_CB_STATE_OPEN = "open"  # circuit tripped, instance blocked from scheduling
_CB_STATE_CLOSED = "closed"  # normal, instance can be scheduled


@dataclass
class CircuitBreakerState:
    """Per-instance circuit breaker state.

    ``state``: ``"closed"`` (normal, schedulable) or ``"open"`` (tripped, blocked).
    When any endpoint of an instance fails, the whole instance is blocked.
    """

    state: str = _CB_STATE_CLOSED
    trip_count: int = 0
    failure_count: int = 0  # consecutive failures; reset on success or auto-recovery
    current_timeout: float = 0.0

    def is_open(self) -> bool:
        return self.state == _CB_STATE_OPEN

    def is_closed(self) -> bool:
        return self.state == _CB_STATE_CLOSED


# Circuit breaker defaults (built-in, not configurable via config file)
_CB_BASE_TIMEOUT: float = 30.0  # first trip timeout (seconds)
_CB_MAX_TIMEOUT: float = 300.0  # cap timeout (seconds) = 5 min


class CircuitBreakerManager:
    """Global circuit breaker pool + state machine, owned by SchedulerServer."""

    def __init__(self) -> None:
        self._pool: dict[int, CircuitBreakerState] = {}

    def get(self, instance_id: int) -> CircuitBreakerState | None:
        return self._pool.get(instance_id)

    def is_open(self, instance_id: int) -> bool:
        state = self._pool.get(instance_id)
        return state is not None and state.is_open()

    def is_closed(self, instance_id: int) -> bool:
        state = self._pool.get(instance_id)
        return state is None or state.is_closed()

    def _get_or_create(self, instance_id: int) -> CircuitBreakerState:
        if instance_id not in self._pool:
            self._pool[instance_id] = CircuitBreakerState()
        return self._pool[instance_id]

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def process_failure(self, instance_id: int) -> tuple[bool, float]:
        """Report a failure for an instance (any endpoint failure blocks the instance).

        Failures are counted consecutively.  On the third consecutive failure the
        circuit trips.  success() or auto_recover() resets failure_count to zero.

        Returns:
            (should_trip, timeout) — caller should trip if ``should_trip`` is True,
            using ``timeout`` as the trip timeout.
        """
        state = self._get_or_create(instance_id)

        if state.is_open():
            # Already tripped; ignore extra failure reports (race window).
            return (False, 0)

        state.failure_count += 1

        if state.failure_count < 3:
            logger.warning(
                "CircuitBreaker failure #%d: instance_id=%d",
                state.failure_count,
                instance_id,
            )
            return (False, 0)

        # Three consecutive failures → trip.
        state.trip_count += 1
        timeout = min(
            _CB_BASE_TIMEOUT * (2 ** (state.trip_count - 1)),
            _CB_MAX_TIMEOUT,
        )
        self._trip_state(state, timeout)
        logger.error(
            "CircuitBreaker failure #%d tripped: instance_id=%d trip_count=%d timeout=%.0fs",
            state.failure_count,
            instance_id,
            state.trip_count,
            timeout,
        )
        return (True, timeout)

    def process_success(self, instance_id: int) -> bool:
        """Report a successful request to an instance.

        Returns True if the success triggered an early recovery from OPEN → CLOSED
        (caller should cancel the recovery timer and publish the state change).
        Returns False otherwise (no state change, or key not found).
        """

        state = self._pool.get(instance_id)
        if state is None:
            return False

        prev_trip = state.trip_count
        prev_timeout = state.current_timeout
        state.trip_count = 0
        state.failure_count = 0
        state.current_timeout = 0

        if state.is_open():
            # Early recovery: a worker that has not yet received the PUB "open"
            # notification reports a success.  Trust the success and re-close
            # the circuit so the instance becomes schedulable immediately.
            state.state = _CB_STATE_CLOSED
            logger.info(
                "CircuitBreaker early-recovered via success: instance_id=%d prev_trip_count=%d prev_timeout=%.0fs",
                instance_id,
                prev_trip,
                prev_timeout,
            )
            return True

        if prev_trip > 0:
            logger.info(
                "CircuitBreaker success reported, resetting: instance_id=%d prev_trip_count=%d",
                instance_id,
                prev_trip,
            )
        return False

    # ------------------------------------------------------------------
    # Auto-recovery
    # ------------------------------------------------------------------

    def auto_recover(self, instance_id: int) -> bool:
        """Called by recovery timer. Returns True if state was actually recovered."""
        state = self._pool.get(instance_id)
        if state is None or not state.is_open():
            return False

        timeout = state.current_timeout
        state.state = _CB_STATE_CLOSED
        state.failure_count = 0
        logger.info(
            "CircuitBreaker auto-recovered: instance_id=%d timeout=%.0fs",
            instance_id,
            timeout,
        )
        return True

    def clear_instance(self, instance_id: int):
        """Remove circuit breaker record for an instance. Returns 1 if cleared, 0 otherwise."""
        existed = instance_id in self._pool
        if existed:
            del self._pool[instance_id]
            logger.info(
                "CircuitBreaker cleared by Controller sync: instance_id=%d",
                instance_id,
            )

    def clear_all(self) -> int:
        """Remove all circuit breaker records. Returns count cleared."""
        count = len(self._pool)
        self._pool.clear()
        if count:
            logger.info("CircuitBreaker cleared all: count=%d", count)
        return count

    def get_open_instance_ids(self) -> list[int]:
        """Return IDs of all currently-open (tripped) instances."""
        return [iid for iid, state in self._pool.items() if state.is_open()]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _trip_state(state: CircuitBreakerState, timeout: float) -> None:
        state.state = _CB_STATE_OPEN
        state.current_timeout = timeout
