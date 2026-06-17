# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for motor.node_manager.core.fault_reporter."""

import os
import sys

import pytest
from unittest.mock import patch, MagicMock

os.environ["USER_CONFIG_PATH"] = "tests/jsons/useruser_config.json"
os.environ["ROLE"] = "both"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from motor.node_manager.core.fault_reporter import FaultReporter
from motor.config.node_manager import NodeManagerConfig
from motor.common.resources.endpoint import Endpoint

# pylint: disable=redefined-outer-name,duplicate-code


@pytest.fixture
def config():
    cfg = NodeManagerConfig()
    cfg.api_config.pod_ip = "192.168.1.1"
    cfg.fault_tolerance_config.enable_fault_tolerance = True
    cfg.fault_tolerance_config.zmq_pub_port = 0
    return cfg


@pytest.fixture
def endpoints():
    return [
        Endpoint(id=0, ip="192.168.1.1", business_port="8000", mgmt_port="9000"),
        Endpoint(id=1, ip="192.168.1.1", business_port="8001", mgmt_port="9001"),
    ]


@pytest.fixture
def reporter(config):
    return FaultReporter(config)


# -- public API ----------------------------------------------------------------


def test_start_creates_thread(reporter, endpoints):
    reporter.start(endpoints)
    assert reporter._thread is not None
    reporter.stop()


def test_start_disabled_no_thread(config, endpoints):
    config.fault_tolerance_config.enable_fault_tolerance = False
    r = FaultReporter(config)
    r.start(endpoints)
    assert r._thread is None


def test_start_idempotent(reporter, endpoints):
    reporter.start(endpoints)
    t1 = reporter._thread
    reporter.start(endpoints)
    assert reporter._thread is t1
    reporter.stop()


def test_update_config_enables(config, endpoints):
    config.fault_tolerance_config.enable_fault_tolerance = False
    r = FaultReporter(config)
    config.fault_tolerance_config.enable_fault_tolerance = True
    r.update_config(config, endpoints)
    assert r._enabled is True
    assert r._thread is not None
    r.stop()


def test_update_config_disables(reporter, endpoints):
    reporter.start(endpoints)
    cfg = reporter._config
    cfg.fault_tolerance_config.enable_fault_tolerance = False
    reporter.update_config(cfg, endpoints)
    assert reporter._enabled is False
    assert reporter._thread is None


def test_stop_joins_thread(reporter, endpoints):
    reporter.start(endpoints)
    reporter.stop()
    assert reporter._thread is None


# -- ZMQ setup -----------------------------------------------------------------


@patch("motor.node_manager.core.fault_reporter.zmq")
def test_setup_zmq_multi(mock_zmq, config, endpoints):
    import zmq as real_zmq

    config.fault_tolerance_config.zmq_pub_port = 5555
    r = FaultReporter(config)
    r._endpoints = endpoints

    mock_ctx_cls = MagicMock()
    mock_ctx_instance = mock_ctx_cls.return_value
    mock_sub = MagicMock()
    mock_ctx_instance.socket.return_value = mock_sub
    mock_zmq.Context = mock_ctx_cls
    mock_zmq.SUB = real_zmq.SUB
    mock_zmq.Poller.return_value = MagicMock()

    sub_sockets, poller, _ = r._setup_zmq_sub_sockets()

    mock_ctx_cls.assert_called_once()
    assert mock_ctx_instance.socket.call_count == 2
    mock_sub.connect.assert_any_call("tcp://192.168.1.1:5555")
    mock_sub.connect.assert_any_call("tcp://192.168.1.1:5556")
    assert len(sub_sockets) == 2
    assert poller is not None


@patch("motor.node_manager.core.fault_reporter.zmq")
def test_setup_zmq_no_port(mock_zmq, config, endpoints):
    r = FaultReporter(config)
    r._endpoints = endpoints
    sub_sockets, poller, zmq_ctx = r._setup_zmq_sub_sockets()
    assert len(sub_sockets) == 0
    assert poller is None
    assert zmq_ctx is None


@patch("motor.node_manager.core.fault_reporter.zmq")
def test_setup_zmq_no_endpoints(mock_zmq, config):
    config.fault_tolerance_config.zmq_pub_port = 5555
    r = FaultReporter(config)
    sub_sockets, poller, _ = r._setup_zmq_sub_sockets()
    assert len(sub_sockets) == 0


# -- ZMQ processing ------------------------------------------------------------


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_zmq_dead(mock_report, reporter):
    import msgspec.msgpack

    msg = {
        "schema_version": 1,
        "total_engines": 2,
        "engines": [{"id": 0, "status": "dead"}, {"id": 1, "status": "healthy"}],
    }
    raw = msgspec.msgpack.encode(msg)
    known = {}
    reporter._process_zmq_engine_status(raw, known)
    mock_report.assert_called_once()
    called = mock_report.call_args[0][0]
    assert called["engine_id"] == 0
    assert called["engine_status"] == 1
    assert known == {0: "dead", 1: "healthy"}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_zmq_dedup(mock_report, reporter):
    import msgspec.msgpack

    msg = {"schema_version": 1, "total_engines": 1, "engines": [{"id": 0, "status": "dead"}]}
    raw = msgspec.msgpack.encode(msg)
    known = {0: "dead"}
    reporter._process_zmq_engine_status(raw, known)
    mock_report.assert_not_called()


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_zmq_healthy(mock_report, reporter):
    import msgspec.msgpack

    msg = {"schema_version": 1, "total_engines": 1, "engines": [{"id": 0, "status": "healthy"}]}
    raw = msgspec.msgpack.encode(msg)
    known = {}
    reporter._process_zmq_engine_status(raw, known)
    mock_report.assert_not_called()
    assert known == {0: "healthy"}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_send_fault_injects_pod_ip(mock_report, reporter):
    fault = {"exception_type": "KeyError", "engine_id": 1, "engine_status": 2}
    reporter._send_fault_to_controller(fault)
    mock_report.assert_called_once()
    assert mock_report.call_args[0][0]["pod_ip"] == "192.168.1.1"


# -- Main Loop ----------------------------------------------------------------------


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
@patch("motor.node_manager.core.fault_reporter.zmq")
def test_main_loop_multi_socket(mock_zmq, mock_report, config, endpoints):
    import zmq as real_zmq
    import msgspec.msgpack

    config.fault_tolerance_config.zmq_pub_port = 5555
    r = FaultReporter(config)
    r._endpoints = endpoints

    msg_dead = msgspec.msgpack.encode(
        {
            "schema_version": 1,
            "total_engines": 1,
            "engines": [{"id": 0, "status": "dead"}],
        }
    )
    msg_uh = msgspec.msgpack.encode(
        {
            "schema_version": 1,
            "total_engines": 1,
            "engines": [{"id": 1, "status": "unhealthy"}],
        }
    )

    sub0 = MagicMock()
    sub0.recv_multipart.return_value = (b"vllm_fault", msg_dead)
    sub1 = MagicMock()
    sub1.recv_multipart.return_value = (b"vllm_fault", msg_uh)

    mock_ctx_inst = MagicMock()
    mock_ctx_inst.socket.side_effect = [sub0, sub1]
    mock_zmq.Context.return_value = mock_ctx_inst
    mock_zmq.SUB = real_zmq.SUB

    mock_poller = MagicMock()
    mock_zmq.Poller.return_value = mock_poller

    cnt = [0]

    def stop_after():
        def side_effect(*a, **kw):
            cnt[0] += 1
            if cnt[0] >= 2:
                r._stop_event.set()
            return [{sub0: real_zmq.POLLIN}, {sub1: real_zmq.POLLIN}][cnt[0] - 1]

        return side_effect

    mock_poller.poll.side_effect = stop_after()

    r._main_loop()

    assert mock_poller.register.call_count == 2
    assert mock_report.call_count == 2
    assert mock_report.call_args_list[0][0][0]["engine_id"] == 0
    assert mock_report.call_args_list[0][0][0]["engine_status"] == 1
    assert mock_report.call_args_list[1][0][0]["engine_id"] == 1
    assert mock_report.call_args_list[1][0][0]["engine_status"] == 2


# -- ZMQ retry on error  --------------------------------------------------------


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
@patch("motor.node_manager.core.fault_reporter.zmq")
def test_main_loop_retry_after_zmq_error(mock_zmq, mock_report, config, endpoints):
    """When ZMQError occurs during poll, the loop tears down old sockets,
    reconnects, and continues processing — instead of exiting."""
    import zmq as real_zmq
    import msgspec.msgpack

    config.fault_tolerance_config.zmq_pub_port = 5555
    r = FaultReporter(config)
    r._endpoints = endpoints
    r._ZMQ_RECONNECT_DELAY = 0.0  # skip wait in test

    msg_dead = msgspec.msgpack.encode(
        {"schema_version": 1, "total_engines": 1, "engines": [{"id": 0, "status": "dead"}]}
    )

    # First-round mocks: poller raises ZMQError
    old_poller = MagicMock()
    old_poller.poll.side_effect = real_zmq.ZMQError("connection lost")
    old_sub = MagicMock()
    old_ctx = MagicMock()

    # Second-round mocks: poller processes one message, then stops
    new_poller = MagicMock()
    new_sub = MagicMock()
    new_sub.recv_multipart.return_value = (b"vllm_fault", msg_dead)
    new_ctx = MagicMock()

    call_count = [0]

    def poll_side_effect(*a, **kw):
        call_count[0] += 1
        if call_count[0] >= 2:
            r._stop_event.set()
        return {new_sub: real_zmq.POLLIN}

    new_poller.poll.side_effect = poll_side_effect

    # _setup_zmq_sub_sockets → first returns old mocks, then new mocks
    mock_zmq.Context.return_value = old_ctx
    old_ctx.socket.return_value = old_sub
    mock_zmq.SUB = real_zmq.SUB
    mock_zmq.ZMQError = real_zmq.error.ZMQError  # pin to real exception class
    mock_zmq.Poller.return_value = old_poller

    # After first teardown + retry, switch to new mocks
    orig_setup = r._setup_zmq_sub_sockets
    setup_count = [0]

    def setup_side_effect():
        setup_count[0] += 1
        if setup_count[0] == 1:
            # First call: return old mocks (already configured via mock_zmq)
            sub_sockets, poller, ctx = orig_setup()
            poller.poll.side_effect = real_zmq.ZMQError("connection lost")
            return sub_sockets, poller, ctx
        else:
            # Retry call: return new mocks
            mock_zmq.Context.return_value = new_ctx
            new_ctx.socket.return_value = new_sub
            mock_zmq.Poller.return_value = new_poller
            return orig_setup()

    with patch.object(r, "_setup_zmq_sub_sockets", side_effect=setup_side_effect):
        r._main_loop()

    # First setup was called, then teardown, then retry
    assert setup_count[0] == 2
    # Old sockets were closed
    old_sub.close.assert_called()
    old_ctx.term.assert_called()
    # New sockets processed a message
    mock_report.assert_called_once()
    assert mock_report.call_args[0][0]["engine_id"] == 0
    assert mock_report.call_args[0][0]["engine_status"] == 1


# -- Dedup after delivery (retry on failure) ----------------------------------


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_zmq_failed_report_not_deduped(mock_report, reporter):
    """When Controller is unreachable (report returns False), the status must
    NOT be marked as known so it will be retried on the next ZMQ message."""
    import msgspec.msgpack

    mock_report.return_value = False
    msg = {"schema_version": 1, "total_engines": 1, "engines": [{"id": 0, "status": "dead"}]}
    raw = msgspec.msgpack.encode(msg)
    known: dict[int, str] = {}
    reporter._process_zmq_engine_status(raw, known)

    # Report was attempted
    mock_report.assert_called_once()
    # But on failure, known_statuses must NOT contain the engine
    assert 0 not in known


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_zmq_successful_report_marked_as_known(mock_report, reporter):
    """When Controller confirms delivery (report returns True), the status
    IS marked as known so subsequent identical messages are deduplicated."""
    import msgspec.msgpack

    mock_report.return_value = True
    msg = {"schema_version": 1, "total_engines": 1, "engines": [{"id": 0, "status": "dead"}]}
    raw = msgspec.msgpack.encode(msg)
    known: dict[int, str] = {}
    reporter._process_zmq_engine_status(raw, known)

    mock_report.assert_called_once()
    assert known == {0: "dead"}


# -- update_config restart conditions ------------------------------------------


def test_update_config_restart_on_pod_ip_change(config, endpoints):
    """When pod_ip changes while enabled, restart to rebuild ZMQ sockets."""
    config.fault_tolerance_config.zmq_pub_port = 5555
    r = FaultReporter(config)
    r._endpoints = endpoints
    r.start()

    new_config = NodeManagerConfig()
    new_config.fault_tolerance_config.enable_fault_tolerance = True
    new_config.fault_tolerance_config.zmq_pub_port = 5555
    new_config.api_config.pod_ip = "10.0.0.99"  # changed

    r.update_config(new_config, endpoints)

    assert r._enabled is True
    assert r._thread is not None
    r.stop()


def test_update_config_restart_on_zmq_port_change(config, endpoints):
    """When zmq_pub_port changes while enabled, restart to rebuild ZMQ sockets."""
    config.fault_tolerance_config.zmq_pub_port = 5555
    r = FaultReporter(config)
    r._endpoints = endpoints
    r.start()

    new_config = NodeManagerConfig()
    new_config.fault_tolerance_config.enable_fault_tolerance = True
    new_config.fault_tolerance_config.zmq_pub_port = 6666  # changed
    new_config.api_config.pod_ip = "192.168.1.1"

    r.update_config(new_config, endpoints)

    assert r._enabled is True
    assert r._thread is not None
    r.stop()


def test_update_config_no_restart_when_nothing_changed(reporter, config, endpoints):
    """When pod_ip, zmq_port, and endpoints are all unchanged, no restart."""
    config.fault_tolerance_config.zmq_pub_port = 5555
    reporter._endpoints = endpoints
    reporter.start()

    t1 = reporter._thread
    reporter.update_config(config, endpoints)
    assert reporter._thread is t1  # Same thread object = no restart
    reporter.stop()
