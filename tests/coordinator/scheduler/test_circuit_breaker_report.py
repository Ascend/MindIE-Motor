# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Unit tests for circuit breaker report handler (_handle_circuit_breaker_report)."""

import asyncio

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.circuit_breaker import CircuitBreakerManager
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.runtime.scheduler_server import (
    _SchedulerRequestDispatcher,
)
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    SchedulerRequest,
    SchedulerRequestType,
    SchedulerResponseType,
)
from motor.coordinator.scheduler.scheduler import Scheduler


def _make_cb_dispatcher(cb_manager=None):
    """Create a dispatcher with circuit breaker, no pub_socket."""
    config = CoordinatorConfig()
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)
    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    cb = cb_manager or CircuitBreakerManager()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        circuit_breaker_manager=cb,
        pub_socket=None,
    )
    return dispatcher, cb


def _cb_request(instance_id, event):
    """Build a SchedulerRequest for circuit breaker report."""
    return SchedulerRequest(
        request_type=SchedulerRequestType.CIRCUIT_BREAKER_REPORT.value,
        request_id="req-cb",
        data={"instance_id": instance_id, "event": event},
    )


def _dispatch(dispatcher, request):
    """Helper: run async dispatcher.dispatch via asyncio.run."""
    return asyncio.run(dispatcher.dispatch(request))


class TestCircuitBreakerReport:
    """Tests for _handle_circuit_breaker_report in _SchedulerRequestDispatcher."""

    # ---- error paths ----

    def test_missing_instance_id_returns_error(self):
        dispatcher, _ = _make_cb_dispatcher()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.CIRCUIT_BREAKER_REPORT.value,
            request_id="req-1",
            data={},
        )
        response = _dispatch(dispatcher, request)
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Missing instance_id" in (response.error or "")

    def test_unknown_event_returns_error(self):
        dispatcher, _ = _make_cb_dispatcher()
        response = _dispatch(dispatcher, _cb_request(1, "bogus_event"))
        assert response.response_type == SchedulerResponseType.ERROR
        assert "Unknown circuit breaker event" in (response.error or "")

    # ---- failure path ----

    def test_third_failure_trips_circuit(self):
        dispatcher, cb = _make_cb_dispatcher()
        for _ in range(2):
            resp = _dispatch(dispatcher, _cb_request(1, "failure"))
            assert resp.response_type == SchedulerResponseType.SUCCESS
            assert not cb.is_open(1)
        resp = _dispatch(dispatcher, _cb_request(1, "failure"))
        assert resp.response_type == SchedulerResponseType.SUCCESS
        assert cb.is_open(1)

    def test_failure_while_open_is_ignored(self):
        """Extra failures after circuit is open do not change state."""
        dispatcher, cb = _make_cb_dispatcher()
        for _ in range(3):
            _dispatch(dispatcher, _cb_request(1, "failure"))
        assert cb.is_open(1)
        resp = _dispatch(dispatcher, _cb_request(1, "failure"))
        assert resp.response_type == SchedulerResponseType.SUCCESS
        assert cb.is_open(1)  # still open, extra failure ignored

    # ---- success path ----

    def test_success_early_recovery_from_open(self):
        """Success on an open circuit triggers early-recovery (OPEN->CLOSED)."""
        dispatcher, cb = _make_cb_dispatcher()
        for _ in range(3):
            _dispatch(dispatcher, _cb_request(1, "failure"))
        assert cb.is_open(1)
        resp = _dispatch(dispatcher, _cb_request(1, "success"))
        assert resp.response_type == SchedulerResponseType.SUCCESS
        assert not cb.is_open(1)
        assert cb.is_closed(1)

    def test_success_on_closed_resets_counters(self):
        """Success on a closed circuit resets failure_count (no trip on next)."""
        dispatcher, cb = _make_cb_dispatcher()
        _dispatch(dispatcher, _cb_request(1, "failure"))
        _dispatch(dispatcher, _cb_request(1, "failure"))
        # success resets counters
        resp = _dispatch(dispatcher, _cb_request(1, "success"))
        assert resp.response_type == SchedulerResponseType.SUCCESS
        # next failure is count 1, not 3
        resp = _dispatch(dispatcher, _cb_request(1, "failure"))
        assert resp.response_type == SchedulerResponseType.SUCCESS
        assert not cb.is_open(1)
