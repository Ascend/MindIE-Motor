# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""Unit tests for the pre-recovery probe (_probe_instance / _auto_recover)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.circuit_breaker import CircuitBreakerManager
from motor.coordinator.scheduler.runtime.scheduler_server import (
    _SchedulerRequestDispatcher,
)


def _make_dispatcher(cb_manager=None):
    """Dispatcher with mocked instance_manager; no pub_socket."""
    config = CoordinatorConfig()
    instance_manager = MagicMock()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        MagicMock(),  # scheduler, unused here
        config,
        circuit_breaker_manager=cb_manager or CircuitBreakerManager(),
        pub_socket=None,
    )
    return dispatcher, instance_manager


def _mock_instance(instance_manager, instance_id=1, endpoints=None):
    """Available pool with one instance exposing optional endpoints."""
    instance = MagicMock()
    instance.get_all_endpoints.return_value = list(endpoints or [])
    instance_manager.get_available_instances.return_value = {instance_id: instance}
    return instance


def _tripped_cb(cb_manager, instance_id=1):
    """Trip the circuit for an instance (3 consecutive failures)."""
    for _ in range(3):
        cb_manager.process_failure(instance_id)
    return cb_manager


def _http_conn(reader=None, writer=None):
    """open_connection-style (reader, writer) pair for a fake vLLM engine.

    Mirrors asyncio.StreamWriter's real interface: write/close are sync,
    drain is async.
    """
    reader = reader if reader is not None else AsyncMock()
    if writer is None:
        writer = MagicMock()
        writer.drain = AsyncMock()
    return reader, writer


def _health_ok_conn():
    """Engine answering GET /health with HTTP 200."""
    reader, writer = _http_conn()
    reader.readline.return_value = b"HTTP/1.1 200 OK\r\n"
    return reader, writer


def _endpoint(port="8080"):
    return MagicMock(ip="127.0.0.1", business_port=port)


class TestProbeInstance:
    """_probe_instance HTTP health check before closing a circuit."""

    async def test_probe_success_returns_true(self):
        dispatcher, instance_manager = _make_dispatcher()
        _mock_instance(instance_manager, 1, [_endpoint()])
        with patch("asyncio.open_connection", AsyncMock(return_value=_health_ok_conn())):
            assert await dispatcher._probe_instance(1) is True

    async def test_probe_non_200_returns_false(self):
        dispatcher, instance_manager = _make_dispatcher()
        _mock_instance(instance_manager, 1, [_endpoint()])
        # vLLM returns 503 while the engine is still loading / not ready.
        reader, writer = _http_conn()
        reader.readline.return_value = b"HTTP/1.1 503 Service Unavailable\r\n"
        with patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))):
            assert await dispatcher._probe_instance(1) is False

    async def test_probe_empty_response_returns_false(self):
        dispatcher, instance_manager = _make_dispatcher()
        _mock_instance(instance_manager, 1, [_endpoint()])
        reader, writer = _http_conn()
        reader.readline.return_value = b""
        with patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))):
            assert await dispatcher._probe_instance(1) is False

    async def test_probe_response_timeout_returns_false(self):
        dispatcher, instance_manager = _make_dispatcher()
        _mock_instance(instance_manager, 1, [_endpoint()])
        reader, writer = _http_conn()
        reader.readline.side_effect = asyncio.TimeoutError()
        with patch("asyncio.open_connection", AsyncMock(return_value=(reader, writer))):
            assert await dispatcher._probe_instance(1) is False

    async def test_probe_connection_error_returns_false(self):
        dispatcher, instance_manager = _make_dispatcher()
        _mock_instance(instance_manager, 1, [_endpoint()])
        with patch("asyncio.open_connection", AsyncMock(side_effect=OSError("no route"))):
            assert await dispatcher._probe_instance(1) is False

    async def test_probe_timeout_returns_false(self):
        dispatcher, instance_manager = _make_dispatcher()
        _mock_instance(instance_manager, 1, [_endpoint()])
        with patch("asyncio.open_connection", AsyncMock(side_effect=asyncio.TimeoutError())):
            assert await dispatcher._probe_instance(1) is False

    async def test_probe_instance_not_in_available_pool_drops_recovery(self):
        """Instance outside the available pool is not probed; its circuit record is dropped."""
        cb = _tripped_cb(CircuitBreakerManager())
        dispatcher, instance_manager = _make_dispatcher(cb)
        instance_manager.get_available_instances.return_value = {}
        with patch("asyncio.open_connection", AsyncMock()) as mock_conn:
            assert await dispatcher._probe_instance(1) is False
            mock_conn.assert_not_called()  # no probe attempt on a paused instance
        assert cb.get(1) is None  # circuit record dropped

    async def test_probe_instance_without_endpoint_returns_false(self):
        dispatcher, instance_manager = _make_dispatcher()
        _mock_instance(instance_manager, 1)  # no endpoint
        assert await dispatcher._probe_instance(1) is False

    async def test_probe_all_endpoints_must_200(self):
        """Multi-endpoint instance: any broken endpoint keeps the circuit open."""
        dispatcher, instance_manager = _make_dispatcher()
        _mock_instance(instance_manager, 1, [_endpoint(), _endpoint("8081")])
        bad_reader, bad_writer = _http_conn()
        bad_reader.readline.return_value = b"HTTP/1.1 503 Service Unavailable\r\n"
        with patch(
            "asyncio.open_connection",
            AsyncMock(side_effect=[_health_ok_conn(), (bad_reader, bad_writer)]),
        ):
            assert await dispatcher._probe_instance(1) is False

    async def test_probe_all_endpoints_ok_closes(self):
        """Multi-endpoint instance: all endpoints healthy -> probe passes."""
        dispatcher, instance_manager = _make_dispatcher()
        _mock_instance(instance_manager, 1, [_endpoint(), _endpoint("8081")])
        with patch(
            "asyncio.open_connection",
            AsyncMock(side_effect=[_health_ok_conn(), _health_ok_conn()]),
        ):
            assert await dispatcher._probe_instance(1) is True


class TestAutoRecover:
    """_auto_recover probes first; probe failure keeps the circuit open + retries."""

    async def test_probe_failure_keeps_open_and_reschedules(self):
        cb = _tripped_cb(CircuitBreakerManager())
        dispatcher, instance_manager = _make_dispatcher(cb)
        _mock_instance(
            instance_manager,
            1,
            [_endpoint()],
        )
        with patch("asyncio.open_connection", AsyncMock(side_effect=OSError("refused"))):
            await dispatcher._auto_recover(1, 0.001)

        assert cb.is_open(1)
        assert cb.get(1).trip_count == 2  # timeout extended
        task = dispatcher._recovery_timers.get(1)
        assert task is not None and not task.done()  # retry scheduled

        dispatcher._cancel_recovery(1)
        await asyncio.gather(task, return_exceptions=True)

    async def test_probe_success_closes_circuit(self):
        cb = _tripped_cb(CircuitBreakerManager())
        dispatcher, instance_manager = _make_dispatcher(cb)
        _mock_instance(instance_manager, 1, [_endpoint()])
        with patch("asyncio.open_connection", AsyncMock(return_value=_health_ok_conn())):
            await dispatcher._auto_recover(1, 0.001)

        assert cb.is_closed(1)
        assert 1 not in dispatcher._recovery_timers

    async def test_probe_failure_when_closed_does_not_reschedule(self):
        cb = CircuitBreakerManager()  # never tripped
        dispatcher, instance_manager = _make_dispatcher(cb)
        _mock_instance(
            instance_manager,
            1,
            [_endpoint()],
        )
        with patch("asyncio.open_connection", AsyncMock(side_effect=OSError("refused"))):
            await dispatcher._auto_recover(1, 0.001)

        assert cb.is_closed(1)
        assert 1 not in dispatcher._recovery_timers

    async def test_probe_not_available_drops_recovery(self):
        """Instance outside the available pool: recovery is dropped (no
        reschedule, circuit record cleared) instead of retrying forever.
        """
        cb = _tripped_cb(CircuitBreakerManager())
        dispatcher, instance_manager = _make_dispatcher(cb)
        instance_manager.get_available_instances.return_value = {}

        await dispatcher._auto_recover(1, 0.001)

        assert cb.get(1) is None  # circuit record cleared
        assert 1 not in dispatcher._recovery_timers  # no retry scheduled

    async def test_probe_success_resets_trip_count(self):
        """Successful probe resets trip_count; the next trip restarts from the base timeout."""
        cb = _tripped_cb(CircuitBreakerManager())
        cb.process_probe_failure(1)  # extend within the episode
        assert cb.get(1).trip_count == 2
        dispatcher, instance_manager = _make_dispatcher(cb)
        _mock_instance(instance_manager, 1, [_endpoint()])
        with patch("asyncio.open_connection", AsyncMock(return_value=_health_ok_conn())):
            await dispatcher._auto_recover(1, 0.001)

        assert cb.is_closed(1)
        assert cb.get(1).trip_count == 0
