# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for the NodeManager API server routes (engine-restart, stop, ...)."""

import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

os.environ["USER_CONFIG_PATH"] = "tests/jsons/useruser_config.json"
os.environ["ROLE"] = "both"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from motor.node_manager.api_server.node_manager_api import app

# pylint: disable=redefined-outer-name


@pytest.fixture
def client():
    return TestClient(app)


def _ft_config(enabled=True):
    return SimpleNamespace(
        fault_tolerance_config=SimpleNamespace(
            enable_dp_scale_down_proxy=enabled,
            poll_timeout_sec=3,
            engine_restart_freeze_sec=30,
        ),
        basic_config=SimpleNamespace(
            parallel_config=SimpleNamespace(dp_master_port=29500),
        ),
    )


def test_engine_ft_status_routes_explicit_endpoints(client):
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config()
        query = daemon.return_value.engine_ft_manager.query
        query.return_value = {1: {"status": "unhealthy"}}
        response = client.post("/node-manager/fault-tolerance/status", json={"endpoint_ids": [1]})

    assert response.status_code == 200
    query.assert_called_once_with([1], 3)


def test_engine_ft_proxy_is_unavailable_when_controller_switch_is_off(client):
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config(False)
        query = daemon.return_value.engine_ft_manager.query
        response = client.post("/node-manager/fault-tolerance/status", json={"endpoint_ids": [1]})

    assert response.status_code == 404
    query.assert_not_called()


def test_disabled_engine_ft_guard_rejects_before_parsing_payload(client):
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config(False)
        response = client.post(
            "/node-manager/fault-tolerance/guard",
            content=b"{",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 404
    daemon.return_value.engine_ft_manager.guard.assert_not_called()


def test_engine_ft_apply_guard_and_finalize_routes(client):
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config()
        manager = daemon.return_value.engine_ft_manager
        apply, guard, finalize = manager.apply, manager.guard, manager.finalize
        apply_response = client.post(
            "/node-manager/fault-tolerance/apply",
            json={
                "endpoint_ids": [0],
                "instruction": "scale_down",
                "params": {"removed_dp_ranks": [1]},
                "request_id": "request",
            },
        )
        guard_response = client.post(
            "/node-manager/fault-tolerance/guard",
            json={"request_id": "request", "lease_sec": 45},
        )
        finalize_response = client.post(
            "/node-manager/fault-tolerance/finalize",
            json={"request_id": "request", "retired_endpoint_ids": [], "commit": True, "dp_master_rank": 2},
        )

    assert apply_response.status_code == 200
    apply.assert_called_once_with([0], "scale_down", {"removed_dp_ranks": [1]}, "request", 3, 29500)
    assert guard_response.status_code == 200
    guard.assert_called_once_with("request", 45)
    assert finalize_response.json() == {"status": "committed"}
    finalize.assert_called_once_with("request", [], True, 2)


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("status", []),
        ("apply", {"endpoint_ids": [0]}),
        ("finalize", {"request_id": "request", "retired_endpoint_ids": "1", "commit": True}),
    ],
)
def test_engine_ft_routes_reject_malformed_payload_with_400(client, path, payload):
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config()
        response = client.post("/node-manager/fault-tolerance/%s" % path, json=payload)

    assert response.status_code == 400


@pytest.fixture
def route_mocks():
    """Patch the singleton components the route talks to."""
    with (
        patch("motor.node_manager.api_server.node_manager_api.HeartbeatManager") as mock_hb_cls,
        patch("motor.node_manager.api_server.node_manager_api.Daemon") as mock_daemon_cls,
    ):
        yield mock_hb_cls, mock_daemon_cls


@pytest.mark.parametrize(
    "payload",
    ["nope", {"action": "explode"}, {"action": "shutdown"}],
    ids=["invalid-json", "unknown-action", "removed-shutdown-action"],
)
def test_engine_restart_rejects_invalid_requests(client, route_mocks, payload):
    resp = client.post("/node-manager/engine-restart", json=payload)
    assert resp.status_code == 400


def test_engine_restart_abort_unfreezes(client, route_mocks):
    _, mock_daemon_cls = route_mocks
    resp = client.post("/node-manager/engine-restart", json={"action": "abort"})
    assert resp.status_code == 200
    mock_daemon_cls.return_value.unfreeze_suicide.assert_called_once()


@pytest.mark.parametrize(
    "instance_id,started_after_restore,expected",
    [(7, True, 7), (None, False, None)],
    ids=["with-instance-id", "no-instance-id"],
)
def test_engine_restart_dispatch(client, route_mocks, instance_id, started_after_restore, expected):
    """restart delegates the whole relaunch to the Daemon (params resolved there)."""
    mock_hb_cls, mock_daemon_cls = route_mocks
    mock_hb_cls.return_value.is_started_after_restore.return_value = started_after_restore

    resp = client.post("/node-manager/engine-restart", json={"action": "restart", "instance_id": instance_id})

    assert resp.status_code == 200
    mock_daemon_cls.return_value.restart_engine.assert_called_once_with(expected)


def test_engine_restart_no_start_recorded(client, route_mocks):
    """No launch params recorded: the Daemon raises and the route maps it to 400."""
    from motor.node_manager.core.daemon import EngineRestartParamError

    mock_hb_cls, mock_daemon_cls = route_mocks
    mock_hb_cls.return_value.is_started_after_restore.return_value = True
    mock_daemon_cls.return_value.restart_engine.side_effect = EngineRestartParamError()

    resp = client.post("/node-manager/engine-restart", json={"action": "restart"})
    assert resp.status_code == 400


def test_engine_restart_rejects_snapshot_restore_in_progress(client, route_mocks):
    """409 only during an actual snapshot restore — normal deploys must relaunch."""
    mock_hb_cls, mock_daemon_cls = route_mocks
    mock_hb_cls.return_value.is_started_after_restore.return_value = False

    with patch("motor.node_manager.api_server.node_manager_api.is_restored_from_host_side_snapshot", return_value=True):
        resp = client.post("/node-manager/engine-restart", json={"action": "restart"})

    assert resp.status_code == 409
    mock_daemon_cls.return_value.restart_engine.assert_not_called()


def test_engine_restart_concurrent_rejected(client, route_mocks):
    """A second restart while one is in progress is rejected with 409."""
    from motor.node_manager.core.daemon import EngineRestartInProgressError

    mock_hb_cls, mock_daemon_cls = route_mocks
    mock_hb_cls.return_value.is_started_after_restore.return_value = True
    mock_daemon_cls.return_value.restart_engine.side_effect = EngineRestartInProgressError()

    resp = client.post("/node-manager/engine-restart", json={"action": "restart"})
    assert resp.status_code == 409


def test_engine_restart_pull_failure_returns_500(client, route_mocks):
    """A failed engine pull surfaces as 500 (suicide unfreeze is the Daemon's job)."""
    mock_hb_cls, mock_daemon_cls = route_mocks
    mock_hb_cls.return_value.is_started_after_restore.return_value = True
    mock_daemon_cls.return_value.restart_engine.side_effect = RuntimeError("pull failed")

    resp = client.post("/node-manager/engine-restart", json={"action": "restart"})

    assert resp.status_code == 500


def test_node_manager_stop_sigterms_self(client, route_mocks):
    """stop kills the engines and schedules a delayed self-termination (pod restart)."""
    _, mock_daemon_cls = route_mocks
    with (
        patch("motor.node_manager.api_server.node_manager_api.threading.Timer") as mock_timer_cls,
        patch("motor.node_manager.api_server.node_manager_api._self_terminate") as mock_terminate,
    ):
        resp = client.post("/node-manager/stop", json={})

    assert resp.status_code == 200
    mock_daemon_cls.return_value.stop.assert_called_once()
    mock_timer_cls.assert_called_once_with(0.5, mock_terminate)
    mock_timer_cls.return_value.start.assert_called_once()
