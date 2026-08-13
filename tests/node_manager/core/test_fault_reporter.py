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

import json
import os
import sys

import pytest
from unittest.mock import patch

os.environ["USER_CONFIG_PATH"] = "tests/jsons/useruser_config.json"
os.environ["ROLE"] = "both"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from motor.node_manager.core.fault_reporter import FaultReporter, _engine_ft_enabled
from motor.config.node_manager import NodeManagerConfig
from motor.common.resources.endpoint import Endpoint

# pylint: disable=redefined-outer-name,duplicate-code


# -- engine FT auto-detection --------------------------------------------------


def _write_user_config(tmp_path, content: dict) -> str:
    path = tmp_path / "user_config.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    return str(path)


def test_engine_ft_enabled_none_path():
    assert _engine_ft_enabled(None) is False


def test_engine_ft_enabled_missing_file(tmp_path):
    assert _engine_ft_enabled(str(tmp_path / "nope.json")) is False


def test_engine_ft_enabled_no_ft_key(tmp_path):
    path = _write_user_config(tmp_path, {"motor_engine_prefill_config": {"engine_config": {}}})
    assert _engine_ft_enabled(path) is False


def test_engine_ft_enabled_snake_case_key(tmp_path):
    path = _write_user_config(
        tmp_path,
        {"motor_engine_prefill_config": {"engine_config": {"enable_fault_tolerance": True}}},
    )
    assert _engine_ft_enabled(path) is True


def test_engine_ft_enabled_hyphen_key(tmp_path):
    path = _write_user_config(
        tmp_path,
        {"motor_engine_decode_config": {"engine_config": {"enable-fault-tolerance": True}}},
    )
    assert _engine_ft_enabled(path) is True


def test_engine_ft_enabled_false_value(tmp_path):
    path = _write_user_config(
        tmp_path,
        {"motor_engine_prefill_config": {"engine_config": {"enable_fault_tolerance": False}}},
    )
    assert _engine_ft_enabled(path) is False


def test_engine_ft_enabled_broken_json(tmp_path):
    path = tmp_path / "user_config.json"
    path.write_text("{not json", encoding="utf-8")
    assert _engine_ft_enabled(str(path)) is False


# -- auto-enable without explicit config ---------------------------------------


def test_start_auto_enabled_via_engine_config(tmp_path, endpoints):
    """No explicit flag needed: FT in the engine user config enables reporting."""
    cfg = NodeManagerConfig()
    cfg.api_config.pod_ip = "192.168.1.1"
    cfg.fault_tolerance_config.enable_fault_tolerance = False
    cfg.config_path = _write_user_config(
        tmp_path,
        {"motor_engine_prefill_config": {"engine_config": {"enable-fault-tolerance": True}}},
    )
    r = FaultReporter(cfg)
    r.start(endpoints)
    assert r._thread is not None
    r.stop()


def test_start_not_enabled_without_engine_ft(tmp_path, endpoints):
    cfg = NodeManagerConfig()
    cfg.api_config.pod_ip = "192.168.1.1"
    cfg.fault_tolerance_config.enable_fault_tolerance = False
    cfg.config_path = _write_user_config(tmp_path, {"motor_engine_prefill_config": {"engine_config": {}}})
    r = FaultReporter(cfg)
    r.start(endpoints)
    assert r._thread is None


@pytest.fixture
def config():
    cfg = NodeManagerConfig()
    cfg.api_config.pod_ip = "192.168.1.1"
    cfg.fault_tolerance_config.enable_fault_tolerance = True
    # Endpoints are unreachable in the test env (connect timeout, not refused):
    # a short poll timeout keeps one loop round well under stop()'s join(5s).
    cfg.fault_tolerance_config.poll_timeout_sec = 0.1
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


# -- update_config restart conditions ------------------------------------------


def test_update_config_restart_on_endpoints_change(config, endpoints):
    """When endpoints change while enabled, restart to poll the new engines."""
    r = FaultReporter(config)
    r._endpoints = endpoints
    r.start()

    new_config = NodeManagerConfig()
    new_config.fault_tolerance_config.enable_fault_tolerance = True
    new_config.api_config.pod_ip = "192.168.1.1"
    new_endpoints = endpoints + [Endpoint(id=2, ip="192.168.1.1", business_port="8002", mgmt_port="9002")]

    r.update_config(new_config, new_endpoints)

    assert r._enabled is True
    assert r._thread is not None
    r.stop()


def test_update_config_no_restart_when_nothing_changed(reporter, config, endpoints):
    """When endpoints and config are unchanged, no restart."""
    reporter._endpoints = endpoints
    reporter.start()

    t1 = reporter._thread
    reporter.update_config(config, endpoints)
    assert reporter._thread is t1  # Same thread object = no restart
    reporter.stop()


def test_update_config_no_restart_on_poll_interval_change(reporter, config, endpoints):
    """Poll interval is read inside the loop, so changing it does not restart."""
    reporter._endpoints = endpoints
    reporter.start()

    config.fault_tolerance_config.poll_interval_sec = 1.0
    t1 = reporter._thread
    reporter.update_config(config, endpoints)
    assert reporter._thread is t1
    reporter.stop()


# -- engine status processing --------------------------------------------------


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_healthy_updates_known_no_report(mock_report, reporter):
    known = {}
    reporter._process_engine_status(0, {"id": 0, "status": "healthy"}, known)
    mock_report.assert_not_called()
    assert known == {0: "healthy"}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_unhealthy_with_fault_info(mock_report, reporter):
    known = {}
    reporter._process_engine_status(0, {"id": 0, "status": "unhealthy", "fault_info": "RuntimeError"}, known)
    mock_report.assert_called_once()
    called = mock_report.call_args[0][0]
    assert called["engine_id"] == 0
    assert called["engine_status"] == 2
    assert called["exception_type"] == "RuntimeError"
    assert known == {0: "unhealthy"}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_unhealthy_without_fault_info(mock_report, reporter):
    known = {}
    reporter._process_engine_status(0, {"id": 0, "status": "unhealthy"}, known)
    mock_report.assert_called_once()
    called = mock_report.call_args[0][0]
    assert called["exception_type"] == "EngineUnhealthyError"


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_dead(mock_report, reporter):
    known = {}
    reporter._process_engine_status(0, {"id": 0, "status": "dead"}, known)
    mock_report.assert_called_once()
    called = mock_report.call_args[0][0]
    assert called["engine_id"] == 0
    assert called["engine_status"] == 1
    assert called["exception_type"] == "EngineDeadError"
    assert known == {0: "dead"}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_dedup_same_status(mock_report, reporter):
    known = {0: "dead"}
    reporter._process_engine_status(0, {"id": 0, "status": "dead"}, known)
    mock_report.assert_not_called()


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_unknown_status(mock_report, reporter):
    known = {}
    reporter._process_engine_status(0, {"id": 0, "status": "weird"}, known)
    mock_report.assert_not_called()
    assert known == {}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_recovered_then_faulted_again(mock_report, reporter):
    """After a healthy recovery resets the known status, a new fault is reported."""
    known = {0: "unhealthy"}
    reporter._process_engine_status(0, {"id": 0, "status": "healthy"}, known)
    mock_report.assert_not_called()
    reporter._process_engine_status(0, {"id": 0, "status": "unhealthy"}, known)
    mock_report.assert_called_once()
    assert known == {0: "unhealthy"}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_failed_report_not_deduped(mock_report, reporter):
    """When Controller is unreachable (report returns False), the status must
    NOT be marked as known so it will be retried on the next poll.
    """
    mock_report.return_value = False
    known: dict[int, str] = {}
    reporter._process_engine_status(0, {"id": 0, "status": "dead"}, known)

    mock_report.assert_called_once()
    assert 0 not in known


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_process_successful_report_marked_as_known(mock_report, reporter):
    """When Controller confirms delivery (report returns True), the status
    IS marked as known so subsequent identical polls are deduplicated.
    """
    mock_report.return_value = True
    known: dict[int, str] = {}
    reporter._process_engine_status(0, {"id": 0, "status": "dead"}, known)

    mock_report.assert_called_once()
    assert known == {0: "dead"}


# -- status polling ------------------------------------------------------------


@patch("motor.common.http.engine_ft_client.SafeHTTPSClient")
def test_query_engine_status_uses_business_port(mock_client_cls, config, endpoints):
    """FT status is fetched from the engine's business (API) port."""
    config.fault_tolerance_config.poll_timeout_sec = 7.0
    r = FaultReporter(config)
    mock_client = mock_client_cls.return_value
    mock_client.__enter__.return_value = mock_client  # with-client pattern
    mock_client.get.return_value = {"engines": []}

    r._query_engine_status(endpoints[0])

    mock_client_cls.assert_called_once_with(address="192.168.1.1:8000", tls_config=None, timeout=7.0)
    mock_client.get.assert_called_once_with("/fault_tolerance/status")


def test_poll_engine_healthy_resets_failures(reporter, endpoints):
    """A successful poll clears the consecutive-failure counter and reports nothing."""
    ep = endpoints[0]
    known: dict[int, str] = {}
    failures: dict[int, int] = {0: 2}

    with patch.object(
        reporter,
        "_query_engine_status",
        return_value={"engines": [{"id": 0, "status": "healthy"}]},
    ):
        reporter._poll_engine(ep, known, failures, {ep.id: 0})

    assert failures == {0: 0}
    assert known == {0: "healthy"}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_poll_engine_unhealthy_reports(mock_report, reporter, endpoints):
    ep = endpoints[0]
    known: dict[int, str] = {}
    failures: dict[int, int] = {}

    with patch.object(
        reporter,
        "_query_engine_status",
        return_value={"engines": [{"id": 0, "status": "unhealthy", "fault_info": "KeyError"}]},
    ):
        reporter._poll_engine(ep, known, failures, {ep.id: 0})

    mock_report.assert_called_once()
    assert known == {0: "unhealthy"}
    assert failures == {0: 0}


def test_poll_failures_below_threshold_no_report(reporter, endpoints):
    """Fewer than max_poll_failures consecutive failures are not reported."""
    config = reporter._config
    config.fault_tolerance_config.max_poll_failures = 3
    ep = endpoints[0]
    known: dict[int, str] = {}
    failures: dict[int, int] = {}

    with patch.object(reporter, "_query_engine_status", side_effect=RuntimeError("boom")):
        reporter._poll_engine(ep, known, failures, {ep.id: 0})
        reporter._poll_engine(ep, known, failures, {ep.id: 0})

    assert failures == {0: 2}
    assert known == {}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_poll_failures_reach_threshold_reports_dead(mock_report, reporter, endpoints):
    """max_poll_failures consecutive failures are reported as dead."""
    config = reporter._config
    config.fault_tolerance_config.max_poll_failures = 3
    ep = endpoints[0]
    known: dict[int, str] = {}
    failures: dict[int, int] = {}

    with patch.object(reporter, "_query_engine_status", side_effect=RuntimeError("boom")):
        for _ in range(3):
            reporter._poll_engine(ep, known, failures, {ep.id: 0})

    mock_report.assert_called_once()
    called = mock_report.call_args[0][0]
    assert called["engine_id"] == 0
    assert called["engine_status"] == 1
    assert called["exception_type"] == "EngineDeadError"
    assert "unreachable" in called["exception_message"]
    assert known == {0: "dead"}


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_poll_failures_dedup_dead(mock_report, reporter, endpoints):
    """Continued failures after dead was reported do not re-report."""
    config = reporter._config
    config.fault_tolerance_config.max_poll_failures = 2
    ep = endpoints[0]
    known: dict[int, str] = {0: "dead"}  # already reported
    failures: dict[int, int] = {}

    with patch.object(reporter, "_query_engine_status", side_effect=RuntimeError("boom")):
        for _ in range(5):
            reporter._poll_engine(ep, known, failures, {ep.id: 0})

    mock_report.assert_not_called()


def test_poll_failures_then_recover(reporter, endpoints):
    """After failures, a successful poll resets the counter; later failures
    restart the counting from zero.
    """
    config = reporter._config
    config.fault_tolerance_config.max_poll_failures = 3
    ep = endpoints[0]
    known: dict[int, str] = {}
    failures: dict[int, int] = {}

    side_effects = [
        RuntimeError("boom"),
        RuntimeError("boom"),
        {"engines": [{"id": 0, "status": "healthy"}]},
        RuntimeError("boom"),
    ]
    with patch.object(reporter, "_query_engine_status", side_effect=side_effects):
        for _ in range(4):
            reporter._poll_engine(ep, known, failures, {ep.id: 0})

    # 2 failures -> success (reset) -> 1 failure
    assert failures == {0: 1}
    assert known == {0: "healthy"}


# -- main loop -----------------------------------------------------------------


def test_main_loop_polls_all_endpoints_then_stops(reporter, endpoints):
    """The loop polls every endpoint once per tick and exits on stop_event."""
    config = reporter._config
    config.fault_tolerance_config.poll_interval_sec = 0.01
    r = reporter
    r._endpoints = endpoints

    polled: list[int] = []

    def fake_poll(ep, known, failures, first_poll_time):
        polled.append(ep.id)
        r._stop_event.set()  # stop after the first full round

    with patch.object(r, "_poll_engine", side_effect=fake_poll):
        r._main_loop()

    assert polled == [0, 1]


@patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault")
def test_main_loop_reports_via_sentinel(mock_report, config, endpoints):
    """End-to-end loop: one engine unhealthy -> reported once, then deduped."""
    config.fault_tolerance_config.poll_interval_sec = 0.01
    r = FaultReporter(config)
    r._endpoints = endpoints

    payload = {"engines": [{"id": 0, "status": "unhealthy", "fault_info": "RuntimeError"}]}
    poll_count = [0]

    def fake_query(ep):
        poll_count[0] += 1
        if poll_count[0] >= 3:
            r._stop_event.set()
        return payload

    with patch.object(r, "_query_engine_status", side_effect=fake_query):
        r._main_loop()

    # Both endpoints report unhealthy (dedup keyed per endpoint); each is
    # reported once, then deduped on subsequent polls.
    assert mock_report.call_count == 2
    assert {c.args[0]["engine_id"] for c in mock_report.call_args_list} == {0, 1}


# -- robustness fixes (code review) -------------------------------------------


def test_poll_engine_malformed_payload_does_not_raise(reporter, endpoints):
    """A malformed FT status payload must not kill the polling thread."""
    ep = endpoints[0]
    known: dict[int, str] = {}
    failures: dict[int, int] = {}

    for bad_payload in ([], None, "ok", {"engines": [{"id": 0}]}, {"engines": ["x"]}):
        with patch.object(reporter, "_query_engine_status", return_value=bad_payload):
            reporter._poll_engine(ep, known, failures, {ep.id: 0})  # must not raise


def test_process_engine_status_uses_endpoint_id_key(reporter):
    """Dedup keys are endpoint ids, not payload ids — multi-endpoint safe."""
    known: dict[int, str] = {}
    # endpoint 0 dead, endpoint 1 healthy: payload ids collide on 0, keys must not.
    with patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault") as mock_report:
        mock_report.return_value = True
        reporter._process_engine_status(0, {"id": 0, "status": "dead"}, known)
        reporter._process_engine_status(1, {"id": 0, "status": "healthy"}, known)
        reporter._process_engine_status(0, {"id": 0, "status": "dead"}, known)

    assert known == {0: "dead", 1: "healthy"}
    mock_report.assert_called_once()  # dedup: second dead report suppressed


def test_report_unreachable_dead_within_grace_period_not_reported(reporter, endpoints):
    """Poll failures during engine startup (model load) are not reported dead."""
    ep = endpoints[0]
    known: dict[int, str] = {}
    first_poll_time = {ep.id: __import__("time").time()}

    with patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault") as mock_report:
        reporter._report_unreachable_dead(ep, 3, known, first_poll_time)

    mock_report.assert_not_called()
    assert known == {}


def test_report_unreachable_dead_after_grace_period_reported(reporter, endpoints):
    ep = endpoints[0]
    known: dict[int, str] = {}
    first_poll_time = {ep.id: 0}  # long ago, grace period over

    with patch("motor.node_manager.core.fault_reporter.ControllerApiClient.report_software_fault") as mock_report:
        mock_report.return_value = True
        reporter._report_unreachable_dead(ep, 3, known, first_poll_time)

    mock_report.assert_called_once()
    assert known == {0: "dead"}


def test_engine_ft_enabled_int_value(tmp_path):
    """Config value 1 (not just JSON true) enables FT auto-detection."""
    path = _write_user_config(
        tmp_path,
        {"motor_engine_prefill_config": {"engine_config": {"enable_fault_tolerance": 1}}},
    )
    assert _engine_ft_enabled(path) is True


def test_engine_ft_enabled_ignores_non_engine_sections(tmp_path):
    """A nested 'engine_config' outside the known engine sections is ignored."""
    path = _write_user_config(
        tmp_path,
        {"motor_deploy_config": {"engine_config": {"enable_fault_tolerance": True}}},
    )
    assert _engine_ft_enabled(path) is False
