# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for motor.node_manager.core.engine_ft_manager."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ["USER_CONFIG_PATH"] = "tests/jsons/useruser_config.json"
os.environ["ROLE"] = "both"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from motor.node_manager.core.engine_ft_manager import EngineFtManager
from motor.config.node_manager import NodeManagerConfig
from motor.common.resources.endpoint import Endpoint

# pylint: disable=redefined-outer-name,duplicate-code


@pytest.mark.parametrize(
    "local_engine_enabled,expected",
    [
        (True, True),
        (False, False),
    ],
)
def test_start_uses_engine_capability_only(
    local_engine_enabled,
    expected,
    endpoints,
    no_background_polling,
):
    """Engine FT capability is the only switch — no separate NodeManager flag."""
    cfg = NodeManagerConfig()
    cfg.api_config.pod_ip = "192.168.1.1"
    cfg.basic_config.ft_capability.enabled = local_engine_enabled
    r = EngineFtManager(cfg)
    r.start(endpoints)
    assert (r._thread is not None) is expected
    r.stop()


def test_node_manager_ft_switch_removed():
    """The NodeManager-side FT polling switch must not exist: the engine
    capability (normalized into basic_config.ft_capability) is the single
    source of truth.
    """
    cfg = NodeManagerConfig()
    assert not hasattr(cfg.fault_tolerance_config, "enable_fault_tolerance")


@pytest.fixture
def config():
    cfg = NodeManagerConfig()
    cfg.api_config.pod_ip = "192.168.1.1"
    cfg.basic_config.ft_capability.enabled = True
    cfg.fault_tolerance_config.poll_timeout_sec = 0.1
    return cfg


@pytest.fixture
def endpoints():
    return [
        Endpoint(id=0, ip="192.168.1.1", business_port="8000"),
        Endpoint(id=1, ip="192.168.1.1", business_port="8001"),
    ]


@pytest.fixture
def reporter(config):
    manager = EngineFtManager(config)
    # Simulate a started instance so reports carry a valid instance_id.
    manager._instance_id = 7
    return manager


@pytest.fixture
def no_background_polling():
    def wait_for_stop(manager):
        manager._stop_event.wait()

    with patch.object(EngineFtManager, "_main_loop", wait_for_stop):
        yield


# -- public API ----------------------------------------------------------------


def test_start_creates_thread(reporter, endpoints, no_background_polling):
    reporter.start(endpoints)
    assert reporter._thread is not None
    reporter.stop()


def test_start_idempotent(reporter, endpoints, no_background_polling):
    reporter.start(endpoints)
    t1 = reporter._thread
    reporter.start(endpoints)
    assert reporter._thread is t1
    reporter.stop()


def test_update_config_enables(config, endpoints, no_background_polling):
    config.basic_config.ft_capability.enabled = False
    r = EngineFtManager(config)
    config.basic_config.ft_capability.enabled = True
    r.update_config(config, endpoints)
    assert r._enabled is True
    assert r._thread is not None
    r.stop()


def test_update_config_disables(reporter, endpoints, no_background_polling):
    reporter.start(endpoints)
    cfg = reporter._config
    cfg.basic_config.ft_capability.enabled = False
    reporter.update_config(cfg, endpoints)
    assert reporter._enabled is False
    assert reporter._thread is None


def test_stop_joins_thread(reporter, endpoints, no_background_polling):
    reporter.start(endpoints)
    reporter.stop()
    assert reporter._thread is None


def test_query_refreshes_all_endpoints_without_treating_transport_error_as_dead(reporter, endpoints):
    reporter._endpoints = endpoints
    with (
        patch(
            "motor.node_manager.core.engine_ft_manager.query_engine_ft_entry",
            side_effect=[{"id": 0, "status": "unhealthy"}, TimeoutError("timed out")],
        ),
        patch.object(reporter, "_send_fault_to_controller", return_value=True) as send,
    ):
        result = reporter.query([0, 1], 3)

    assert result[0]["status"] == "unhealthy"
    assert result[1]["status"] == "unknown"
    assert result[1]["source"] == "poll_error"
    assert send.call_count == 1


def test_apply_retry_uses_same_ft_manager_without_scale_down_port(reporter, endpoints):
    reporter._endpoints = endpoints
    with patch("motor.node_manager.core.engine_ft_manager.apply_engine_ft_instructions") as apply:
        reporter.apply([0, 1], "retry", {}, "request", 3, 29500)

    assert apply.call_args.args[1] == "retry"
    assert "dp_store_port" not in apply.call_args.args[2]


def test_apply_scale_down_offsets_store_port_by_new_master_rank(reporter, endpoints):
    reporter._endpoints = endpoints
    with patch("motor.node_manager.core.engine_ft_manager.apply_engine_ft_instructions") as apply:
        reporter.apply(
            [0, 1],
            "scale_down",
            {"removed_dp_ranks": [0], "dp_master_ip": "192.168.1.1", "dp_master_rank": 1},
            "request",
            3,
            29500,
        )

    params = apply.call_args.args[2]
    assert params == {
        "removed_dp_ranks": [0],
        "dp_master_ip": "192.168.1.1",
        "dp_store_port": 29501,
    }


@pytest.mark.parametrize("dp_master_rank", [-1, True, "1"])
def test_apply_scale_down_rejects_invalid_master_rank(reporter, endpoints, dp_master_rank):
    reporter._endpoints = endpoints

    with pytest.raises(ValueError, match="dp_master_rank"):
        reporter.apply(
            [0, 1],
            "scale_down",
            {"dp_master_rank": dp_master_rank},
            "request",
            3,
            29500,
        )


def test_apply_scale_down_rejects_store_port_overflow(reporter, endpoints):
    reporter._endpoints = endpoints

    with pytest.raises(ValueError, match="exceeds 65535"):
        reporter.apply(
            [0, 1],
            "scale_down",
            {"dp_master_rank": 1},
            "request",
            3,
            65535,
        )


def test_guard_rejects_lease_longer_than_bounded_recovery_window(reporter):
    with pytest.raises(ValueError, match=r"\(0, 300\]"):
        reporter.guard("request", 301)


def test_guard_and_finalize_delegate_lifecycle_to_owner(config):
    guard_callback = MagicMock()
    finalize_callback = MagicMock()
    manager = EngineFtManager(config, guard_callback, finalize_callback)

    manager.guard("request", 30)
    manager.finalize("request", [2], True, 3)

    guard_callback.assert_called_once_with("request", 30)
    finalize_callback.assert_called_once_with("request", [2], True, 3)


# -- update_config restart conditions ------------------------------------------


def test_update_config_restart_on_endpoints_change(config, endpoints, no_background_polling):
    """When endpoints change while enabled, restart to poll the new engines."""
    r = EngineFtManager(config)
    r._endpoints = endpoints
    r.start()

    new_config = NodeManagerConfig()
    new_config.basic_config.ft_capability.enabled = True
    new_config.api_config.pod_ip = "192.168.1.1"
    new_endpoints = endpoints + [Endpoint(id=2, ip="192.168.1.1", business_port="8002")]

    r.update_config(new_config, new_endpoints)

    assert r._enabled is True
    assert r._thread is not None
    r.stop()


def test_update_config_no_restart_when_nothing_changed(reporter, config, endpoints, no_background_polling):
    """When endpoints and config are unchanged, no restart."""
    reporter._endpoints = endpoints
    reporter.start()

    t1 = reporter._thread
    reporter.update_config(config, endpoints)
    assert reporter._thread is t1  # Same thread object = no restart
    reporter.stop()


def test_update_config_no_restart_on_poll_interval_change(reporter, config, endpoints, no_background_polling):
    """Poll interval is read inside the loop, so changing it does not restart."""
    reporter._endpoints = endpoints
    reporter.start()

    config.fault_tolerance_config.poll_interval_sec = 1.0
    t1 = reporter._thread
    reporter.update_config(config, endpoints)
    assert reporter._thread is t1
    reporter.stop()


# -- engine status processing --------------------------------------------------


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_process_healthy_updates_known_no_report(mock_report, reporter):
    reporter._known_statuses = {}
    reporter._process_engine_status(0, {"id": 0, "status": "healthy"})
    mock_report.assert_not_called()
    assert reporter._known_statuses[0][0] == "healthy"


@pytest.mark.parametrize(
    ("status", "engine_status", "exception_type"),
    [
        ({"status": "unhealthy", "fault_info": "RuntimeError"}, 2, "RuntimeError"),
        ({"status": "unhealthy"}, 2, "EngineUnhealthyError"),
        ({"status": "dead"}, 1, "EngineDeadError"),
    ],
    ids=["unhealthy-with-fault", "unhealthy-default", "dead"],
)
def test_process_fault_status(status, engine_status, exception_type, reporter):
    status = {"id": 0, **status}
    with patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault") as report:
        reporter._process_engine_status(0, status)

    report.assert_called_once()
    payload = report.call_args.args[0]
    assert payload["engine_id"] == 0
    assert payload["engine_status"] == engine_status
    assert payload["exception_type"] == exception_type
    assert payload["additional_info"]["engine_ft_status"] == status
    assert reporter._known_statuses[0][0] == status["status"]


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_retired_endpoint_is_not_reported(mock_report, reporter):
    reporter.retire_endpoints([1])
    reporter._process_engine_status(1, {"id": 1, "status": "dead"})

    mock_report.assert_not_called()


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_process_dedup_same_status(mock_report, reporter):
    reporter._known_statuses = {0: "dead"}
    reporter._process_engine_status(0, {"id": 0, "status": "dead"})
    mock_report.assert_not_called()


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_process_unknown_status(mock_report, reporter):
    reporter._known_statuses = {}
    reporter._process_engine_status(0, {"id": 0, "status": "weird"})
    mock_report.assert_not_called()
    assert reporter._known_statuses == {}


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_process_recovered_then_faulted_again(mock_report, reporter):
    """After a healthy recovery resets the known status, a new fault is reported."""
    reporter._known_statuses = {0: "unhealthy"}
    reporter._process_engine_status(0, {"id": 0, "status": "healthy"})
    mock_report.assert_not_called()
    reporter._process_engine_status(0, {"id": 0, "status": "unhealthy"})
    mock_report.assert_called_once()
    assert reporter._known_statuses[0][0] == "unhealthy"


@pytest.mark.parametrize("delivered", [False, True])
def test_report_delivery_controls_status_dedup(delivered, reporter):
    with patch(
        "motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault",
        return_value=delivered,
    ) as report:
        reporter._process_engine_status(0, {"id": 0, "status": "dead"})

    report.assert_called_once()
    assert (0 in reporter._known_statuses) is delivered


def test_rejected_status_uses_signature_scoped_backoff(reporter):
    """A rejected unchanged status retries after cooldown, while a changed
    status is still sent immediately instead of being treated as delivered.
    """
    dead = {"id": 0, "status": "dead"}
    unhealthy = {"id": 0, "status": "unhealthy"}
    with (
        patch(
            "motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault",
            return_value=False,
        ) as report,
        patch("motor.node_manager.core.engine_ft_manager.time.monotonic", side_effect=[100.0, 101.0, 101.0, 101.0]),
    ):
        reporter._process_engine_status(0, dead)
        reporter._process_engine_status(0, dead)
        reporter._process_engine_status(0, unhealthy)

    assert report.call_count == 2
    assert {call.args[0]["engine_status"] for call in report.call_args_list} == {1, 2}


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_process_reports_ft_state_transition_without_health_change(mock_report, reporter):
    """UNHEALTHY recovering -> failed is a new event for Controller."""
    mock_report.return_value = True
    reporter._process_engine_status(
        0,
        {
            "id": 0,
            "status": "unhealthy",
            "ft_state": "recovering",
            "last_ft_request_id": "retry-1",
        },
    )
    reporter._process_engine_status(
        0,
        {
            "id": 0,
            "status": "unhealthy",
            "ft_state": "failed",
            "last_ft_request_id": "retry-1",
            "ft_error": "timeout",
        },
    )

    assert mock_report.call_count == 2
    second = mock_report.call_args_list[1].args[0]
    assert second["additional_info"]["ft_state"] == "failed"
    assert second["additional_info"]["ft_error"] == "timeout"


def test_query_engine_status_uses_matching_entry(config, endpoints):
    """Background polling uses the same strict global-rank contract as transactions."""
    config.fault_tolerance_config.poll_timeout_sec = 7.0
    r = EngineFtManager(config)
    entry = {"id": 0, "status": "healthy"}
    with patch(
        "motor.node_manager.core.engine_ft_manager.query_engine_ft_entry",
        return_value=entry,
    ) as query:
        assert r._query_engine_status(endpoints[0]) == entry
    query.assert_called_once_with(endpoints[0], 7.0)


def test_poll_engine_healthy_resets_failures(reporter, endpoints):
    """A successful poll clears the consecutive-failure counter and reports nothing."""
    ep = endpoints[0]
    reporter._known_statuses = {}
    reporter._consecutive_failures = {0: 2}

    with patch.object(
        reporter,
        "_query_engine_status",
        return_value={"id": 0, "status": "healthy"},
    ):
        reporter._poll_engine(ep)

    assert reporter._consecutive_failures == {0: 0}
    assert reporter._known_statuses[0][0] == "healthy"


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_poll_engine_unhealthy_reports(mock_report, reporter, endpoints):
    ep = endpoints[0]
    reporter._known_statuses = {}
    reporter._consecutive_failures = {}

    with patch.object(
        reporter,
        "_query_engine_status",
        return_value={"id": 0, "status": "unhealthy", "fault_info": "KeyError"},
    ):
        reporter._poll_engine(ep)

    mock_report.assert_called_once()
    assert reporter._known_statuses[0][0] == "unhealthy"
    assert reporter._consecutive_failures == {0: 0}


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault", return_value=True)
def test_runtime_poll_failure_reports_dead_only_at_configured_threshold(mock_report, reporter, endpoints):
    ep = endpoints[0]
    reporter._known_statuses = {}
    reporter._consecutive_failures = {}

    with patch.object(reporter, "_query_engine_status", side_effect=RuntimeError("boom")):
        reporter._poll_engine(ep)
        reporter._poll_engine(ep)
        mock_report.assert_not_called()
        reporter._poll_engine(ep)

    assert reporter._consecutive_failures == {0: 3}
    mock_report.assert_called_once()
    assert reporter._known_statuses[0][0] == "dead"


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_poll_failures_dedup_dead(mock_report, reporter, endpoints):
    """Continued failures after dead was reported do not re-report."""
    ep = endpoints[0]
    reporter._known_statuses = {0: "dead"}  # already reported
    reporter._consecutive_failures = {}

    with patch.object(reporter, "_query_engine_status", side_effect=RuntimeError("boom")):
        for _ in range(5):
            reporter._poll_engine(ep)

    mock_report.assert_not_called()


@patch(
    "motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault",
    return_value=False,
)
def test_poll_failures_then_recover(_report, reporter, endpoints):
    """After failures, a successful poll resets the counter; later failures
    restart the counting from zero.
    """
    ep = endpoints[0]
    reporter._known_statuses = {}
    reporter._consecutive_failures = {}

    side_effects = [
        RuntimeError("boom"),
        RuntimeError("boom"),
        {"id": 0, "status": "healthy"},
        RuntimeError("boom"),
    ]
    with patch.object(reporter, "_query_engine_status", side_effect=side_effects):
        for _ in range(4):
            reporter._poll_engine(ep)

    # 2 failures -> success (reset) -> 1 failure
    assert reporter._consecutive_failures == {0: 1}
    assert reporter._known_statuses[0][0] == "healthy"


def test_main_loop_polls_all_endpoints_then_stops(reporter, endpoints):
    """The loop polls every endpoint once per tick and exits on stop_event."""
    config = reporter._config
    config.fault_tolerance_config.poll_interval_sec = 0.01
    r = reporter
    r._endpoints = endpoints

    polled: list[int] = []

    def fake_poll(ep):
        polled.append(ep.id)
        r._stop_event.set()  # stop after the first full round

    with patch.object(r, "_poll_engine", side_effect=fake_poll):
        r._main_loop()

    assert polled == [0, 1]


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_main_loop_reports_via_sentinel(mock_report, config, endpoints):
    """End-to-end loop: one engine unhealthy -> reported once, then deduped."""
    config.fault_tolerance_config.poll_interval_sec = 0.01
    r = EngineFtManager(config)
    r._instance_id = 7
    r._endpoints = endpoints

    poll_count = [0]

    def fake_query(ep):
        poll_count[0] += 1
        if poll_count[0] >= 3:
            r._stop_event.set()
        return {"id": ep.id, "status": "unhealthy", "fault_info": "RuntimeError"}

    with patch.object(r, "_query_engine_status", side_effect=fake_query):
        r._main_loop()

    # Both endpoints report unhealthy (dedup keyed per endpoint); each is
    # reported once, then deduped on subsequent polls.
    assert mock_report.call_count == 2
    assert {c.args[0]["engine_id"] for c in mock_report.call_args_list} == {0, 1}


def test_poll_engine_malformed_payload_does_not_raise(reporter, endpoints):
    """A malformed FT status payload must not kill the polling thread."""
    ep = endpoints[0]
    reporter._known_statuses = {}
    reporter._consecutive_failures = {}

    for bad_payload in ([], None, "ok", {"id": 0}, {"id": 0, "status": 1}):
        with patch.object(reporter, "_query_engine_status", return_value=bad_payload):
            reporter._poll_engine(ep)  # must not raise


def test_process_engine_status_rejects_mismatched_global_rank(reporter):
    """A response for another global DP rank must not be attributed locally."""
    with pytest.raises(ValueError, match="rank mismatch"):
        reporter._process_engine_status(1, {"id": 0, "status": "healthy"})
    assert reporter._known_statuses == {}


def test_pause_suspends_and_resume_resets_state(reporter, endpoints):
    """pause() suspends polling; resume() clears all per-endpoint poll state."""
    ep = endpoints[0]
    reporter._consecutive_failures[ep.id] = 2
    reporter._known_statuses[ep.id] = "dead"

    reporter.pause()
    assert reporter._pause_event.is_set()

    reporter.resume()
    assert not reporter._pause_event.is_set()
    assert reporter._consecutive_failures == {}
    assert reporter._known_statuses == {}


# -- HTTP 404 = optional API unsupported ---------------------------------------


def test_poll_engine_404_marks_endpoint_unsupported_without_failure(reporter, endpoints):
    """Sustained HTTP 404 on the optional FT API is not a connectivity
    failure: no failure counting, no DEAD report, and the endpoint stops
    being polled after the configured consecutive-404 threshold.
    """
    from motor.common.http.engine_ft_client import EngineFtEndpointUnsupported

    ep = endpoints[0]
    reporter._known_statuses = {}
    reporter._consecutive_failures = {0: 1}

    with (
        patch.object(reporter, "_query_engine_status", side_effect=EngineFtEndpointUnsupported("no ft api")),
        patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault") as mock_report,
    ):
        for _ in range(5):
            reporter._poll_engine(ep)

    assert 0 in reporter._unsupported_endpoint_ids
    assert 0 not in reporter._consecutive_failures
    mock_report.assert_not_called()


def test_poll_engine_single_404_does_not_disable_polling(reporter, endpoints):
    """A transient 404 (startup routing, proxy glitch) must not permanently
    disable FT polling: below the consecutive threshold the endpoint keeps
    being polled, and a successful answer clears the 404 counter.
    """
    from motor.common.http.engine_ft_client import EngineFtEndpointUnsupported

    ep = endpoints[0]

    with patch.object(reporter, "_query_engine_status", side_effect=EngineFtEndpointUnsupported("no ft api")):
        for _ in range(2):  # one below the max_consecutive_404 threshold of 3
            reporter._poll_engine(ep)
    assert 0 not in reporter._unsupported_endpoint_ids
    assert reporter._consecutive_404[0] == 2

    healthy_engine = {"id": 0, "status": "healthy"}
    with patch.object(reporter, "_query_engine_status", return_value=healthy_engine):
        reporter._poll_engine(ep)
    assert reporter._consecutive_404 == {}
    assert 0 not in reporter._unsupported_endpoint_ids


def test_unsupported_endpoint_is_reprobed_after_interval(reporter, endpoints):
    """An FT-API-unsupported endpoint is re-probed on a long period so a
    recovered engine image regains polling without a relaunch or config
    refresh; a successful re-probe removes the classification.
    """
    from motor.common.http.engine_ft_client import EngineFtEndpointUnsupported

    ep = endpoints[0]
    reporter._unsupported_endpoint_ids.add(0)
    reporter._unsupported_since[0] = 100.0

    # Within the re-probe interval the endpoint is skipped entirely.
    with (
        patch("motor.node_manager.core.engine_ft_manager.time.monotonic", return_value=100.0 + 10),
        patch.object(reporter, "_query_engine_status") as mock_query,
    ):
        reporter._poll_engine(ep)
    mock_query.assert_not_called()

    # After the interval the endpoint is re-probed; still-404 refreshes the timer.
    with (
        patch("motor.node_manager.core.engine_ft_manager.time.monotonic", return_value=100.0 + 120),
        patch.object(reporter, "_query_engine_status", side_effect=EngineFtEndpointUnsupported("no ft api")),
    ):
        reporter._poll_engine(ep)
    assert 0 in reporter._unsupported_endpoint_ids
    assert reporter._unsupported_since[0] == 220.0

    # A successful re-probe removes the classification immediately.
    with (
        patch("motor.node_manager.core.engine_ft_manager.time.monotonic", return_value=100.0 + 300),
        patch.object(reporter, "_query_engine_status", return_value={"id": 0, "status": "healthy"}),
    ):
        reporter._poll_engine(ep)
    assert 0 not in reporter._unsupported_endpoint_ids
    assert reporter._unsupported_since == {}
    assert reporter._consecutive_404 == {}


def test_update_config_retries_unsupported_endpoints(reporter, endpoints, no_background_polling):
    """A refreshed endpoint set clears the unsupported marker so the FT API
    is retried (engine image may have changed).
    """
    reporter._unsupported_endpoint_ids.add(0)
    reporter._endpoints = endpoints
    reporter.start()
    reporter.update_config(reporter._config, endpoints + [Endpoint(id=2, ip="192.168.1.1", business_port="8002")])
    assert reporter._unsupported_endpoint_ids == set()
    reporter.stop()


def test_resume_retries_unsupported_endpoints(reporter, endpoints):
    """After an engine relaunch the new image is probed again for FT support."""
    reporter._unsupported_endpoint_ids.add(0)
    reporter.resume()
    assert reporter._unsupported_endpoint_ids == set()


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_send_fault_requires_instance_id(mock_report, config, endpoints):
    """Without an instance start command no report may be sent — the
    Controller cannot attribute the fault, so it must not count as delivered.
    """
    manager = EngineFtManager(config)  # _instance_id stays None
    assert manager._send_fault_to_controller({"engine_id": 0, "engine_status": 1}) is False
    mock_report.assert_not_called()


@patch("motor.node_manager.core.engine_ft_manager.ControllerApiClient.report_software_fault")
def test_send_fault_carries_reporter_identity(mock_report, reporter):
    """Reports must carry instance_id, pod_ip and node_manager_port so the
    Controller can attribute them at instance granularity.
    """
    mock_report.return_value = True
    assert reporter._send_fault_to_controller({"engine_id": 0, "engine_status": 1}) is True
    payload = mock_report.call_args.args[0]
    assert payload["instance_id"] == 7
    assert payload["pod_ip"] == "192.168.1.1"
    assert payload["node_manager_port"] == str(reporter._config.api_config.node_manager_port)
