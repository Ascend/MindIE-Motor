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

import logging
import os
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from unittest.mock import ANY, MagicMock, patch

os.environ["USER_CONFIG_PATH"] = "tests/jsons/useruser_config.json"
os.environ["ROLE"] = "both"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from motor.node_manager.api_server.node_manager_api import (
    FaultToleranceStatusAccessFilter,
    app,
    create_app,
)

# pylint: disable=redefined-outer-name


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def migration_client():
    return TestClient(create_app(enable_kv_migration=True))


def _ft_config(enabled=True, scale_up=False, fast_recovery=False):
    return SimpleNamespace(
        fault_tolerance_config=SimpleNamespace(
            enable_dp_scale_down_proxy=enabled,
            enable_fast_recovery_proxy=fast_recovery,
            enable_dp_scale_up=scale_up,
            poll_timeout_sec=3,
            engine_restart_freeze_sec=30,
        ),
        basic_config=SimpleNamespace(
            ft_capability=SimpleNamespace(enabled=False),
            parallel_config=SimpleNamespace(dp_master_port=29500),
            coord_store_port=29510,
        ),
    )


@pytest.mark.parametrize("proxy", [False, True])
@pytest.mark.parametrize("engine_ft", [False, True])
def test_engine_ft_status_routes_explicit_endpoints(client, proxy, engine_ft):
    config = _ft_config(proxy)
    config.basic_config.ft_capability.enabled = engine_ft
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = config
        manager = daemon.return_value.engine_ft_manager
        manager.query.return_value = {1: {"status": "unhealthy"}}
        response = client.post("/node-manager/fault-tolerance/status", json={"endpoint_ids": [1]})
        if not proxy:
            apply_response = client.post(
                "/node-manager/fault-tolerance/apply",
                json={"endpoint_ids": [1], "instruction": "retry", "params": {}, "request_id": "blocked"},
            )
            assert apply_response.status_code == 404
    assert response.status_code == (200 if proxy or engine_ft else 404)
    if proxy or engine_ft:
        manager.query.assert_called_once_with([1], 3)
    else:
        manager.query.assert_not_called()
    manager.apply.assert_not_called()


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


@pytest.mark.parametrize("instruction", ["retry", "scale_down"])
def test_engine_ft_apply_guard_and_finalize_routes(client, instruction):
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config()
        manager = daemon.return_value.engine_ft_manager
        apply, guard, finalize = manager.apply, manager.guard, manager.finalize
        apply_response = client.post(
            "/node-manager/fault-tolerance/apply",
            json={
                "endpoint_ids": [0],
                "instruction": instruction,
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
    apply.assert_called_once_with([0], instruction, {"removed_dp_ranks": [1]}, "request", 3, 29500, None)
    assert guard_response.status_code == 200
    guard.assert_called_once_with("request", 45)
    assert finalize_response.json() == {"status": "committed"}
    finalize.assert_called_once_with("request", [], True, 2)


@pytest.mark.parametrize("proxy_enabled", [False, True])
def test_fast_recovery_broadcast_does_not_require_scale_down_proxy(client, proxy_enabled):
    """HBM fast recovery reaches the broadcast handler through the registered HTTP route."""
    payload = {
        "instruction": "retry",
        "params": {"scope": "all_local_endpoints"},
        "request_id": "hbm-recovery",
    }
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config(proxy_enabled, fast_recovery=True)
        apply = daemon.return_value.apply_fault_tolerance
        apply.return_value = {"engines": [{"engine_id": 0, "success": True}]}
        response = client.post("/node-manager/fault-tolerance/apply", json=payload)

    assert response.status_code == 200
    assert response.json() == apply.return_value
    apply.assert_called_once_with(payload)
    daemon.return_value.engine_ft_manager.apply.assert_not_called()


@pytest.mark.parametrize("instruction", ["retry", "scale_down"])
def test_explicit_endpoint_apply_still_requires_scale_down_proxy(client, instruction):
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config(False)
        response = client.post(
            "/node-manager/fault-tolerance/apply",
            json={"instruction": instruction, "endpoint_ids": [0], "params": {}},
        )

    assert response.status_code == 404
    daemon.return_value.engine_ft_manager.apply.assert_not_called()
    daemon.return_value.apply_fault_tolerance.assert_not_called()


def test_engine_ft_apply_sends_coord_store_port_when_scale_up_enabled(client):
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config(scale_up=True)
        apply = daemon.return_value.engine_ft_manager.apply
        response = client.post(
            "/node-manager/fault-tolerance/apply",
            json={
                "endpoint_ids": [0],
                "instruction": "scale_down",
                "params": {"removed_dp_ranks": [1]},
                "request_id": "request",
            },
        )

    assert response.status_code == 200
    apply.assert_called_once_with([0], "scale_down", {"removed_dp_ranks": [1]}, "request", 3, 29500, 29510)


def test_engine_ft_finalize_commit_allows_empty_retired_ranks(client):
    """Survivor-only Pods finalize with no local ranks to retire."""
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config()
        finalize = daemon.return_value.engine_ft_manager.finalize
        response = client.post(
            "/node-manager/fault-tolerance/finalize",
            json={
                "request_id": "request",
                "retired_endpoint_ids": [],
                "commit": True,
                "dp_master_rank": 0,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "committed"}
    finalize.assert_called_once_with("request", [], True, 0)


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


@pytest.mark.parametrize(
    "method,path,status_code,visible",
    [
        ("GET", "/node-manager/fault-tolerance/status", 200, False),
        ("GET", "/node-manager/fault-tolerance/status?fresh=true", 200, False),
        ("GET", "/node-manager/fault-tolerance/status", 502, True),
        ("POST", "/node-manager/fault-tolerance/status", 200, True),
        ("POST", "/node-manager/fault-tolerance/apply", 200, True),
        ("GET", "/node-manager/fault-tolerance/status-extra", 200, True),
    ],
)
def test_ft_poll_access_filter_preserves_errors_and_control_requests(method, path, status_code, visible):
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", method, path, "1.1", status_code),
        None,
    )
    access_filter = FaultToleranceStatusAccessFilter()
    assert access_filter.filter(record) is visible
    record.levelno = logging.WARNING
    assert access_filter.filter(record)
    record.args = ()
    assert access_filter.filter(record)


@pytest.mark.parametrize("fast_recovery,scale_down", [(False, False), (False, True), (True, False), (True, True)])
def test_fast_recovery_and_status_feature_gates(client, fast_recovery, scale_down):
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = _ft_config(scale_down, fast_recovery=fast_recovery)
        daemon.return_value.query_fault_tolerance_status.return_value = {"engines": []}
        daemon.return_value.apply_fault_tolerance.return_value = {"engines": []}
        status = client.get("/node-manager/fault-tolerance/status")
        apply = client.post("/node-manager/fault-tolerance/apply", json={"instruction": "retry", "params": {}})
        assert status.status_code == (200 if fast_recovery or scale_down else 404)
        assert apply.status_code == (200 if fast_recovery else 404)
        if not fast_recovery:
            daemon.return_value.apply_fault_tolerance.assert_not_called()
        if not (fast_recovery or scale_down):
            daemon.return_value.query_fault_tolerance_status.assert_not_called()


@pytest.fixture
def migration_payload(monkeypatch):
    monkeypatch.setenv("POD_UID", "source")
    return {"source_uid": "source", "operation_id": "op-1", "target_node_labels": ["10.0.0.1"], "max_concurrency": 4}


@pytest.fixture
def migration_service():
    service = MagicMock()
    with patch("motor.node_manager.api_server.migration_api.Daemon") as daemon:
        daemon.return_value._services = {"kv_store": SimpleNamespace(migration=service)}
        yield service


@pytest.mark.parametrize("request_options", [{}, {"timeout_sec": 3}])
def test_migration_trigger_returns_memcache_uuid(
    migration_client, migration_service, migration_payload, request_options
):
    migration_service.submit_directed_drain.return_value = {
        "success": True,
        "status": "submitted",
        "drain_id": "uuid-api-1",
        "state": "migrating",
    }
    response = migration_client.post(
        "/node-manager/migration/trigger",
        json={**migration_payload, **request_options},
    )

    assert response.status_code == 200
    assert response.json()["drain_id"] == "uuid-api-1"
    deadline = migration_service.submit_directed_drain.call_args.kwargs["deadline"]
    if "timeout_sec" in request_options:
        assert 0 < deadline - time.monotonic() <= request_options["timeout_sec"]
    else:
        assert deadline is None
    migration_service.submit_directed_drain.assert_called_once_with("op-1", "source", ["10.0.0.1"], 4, deadline=ANY)


def test_default_api_preserves_legacy_routes(client):
    prefix = "/node-manager/migration/"
    for method, name in (
        ("get", "status"),
        ("get", "inventory"),
        ("post", "cancel"),
        ("post", "prepare-exit"),
        ("post", "trigger"),
        ("post", "stop"),
    ):
        assert getattr(client, method)(prefix + name).status_code == 404
    assert not any(path.startswith(prefix) for path in app.openapi()["paths"])
    with (
        patch("motor.node_manager.api_server.node_manager_api.HeartbeatManager") as heartbeat,
        patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon,
        patch("motor.node_manager.api_server.node_manager_api.threading.Timer"),
    ):
        heartbeat.return_value.get_engine_metrics_targets.return_value = []
        assert client.post("/node-manager/pause", content=b"{").status_code == 200
        assert client.post("/node-manager/stop", content=b"{").status_code == 200
        heartbeat.return_value.pause_all_endpoints.assert_called_once_with()
        daemon.return_value.stop.assert_called_once_with()


@pytest.mark.parametrize(
    "changes,expected_status",
    [
        ({"source_uid": None}, 400),
        ({"operation_id": None}, 400),
        ({"timeout_sec": 0}, 400),
        ({"timeout_sec": True}, 400),
        ({"target_node_labels": []}, 400),
        ({"source_uid": "old-source"}, 409),
    ],
)
def test_migration_trigger_requires_exact_operation_scope(
    migration_client, migration_payload, changes, expected_status
):
    with patch("motor.node_manager.api_server.migration_api.Daemon") as daemon:
        response = migration_client.post("/node-manager/migration/trigger", json={**migration_payload, **changes})
    assert response.status_code == expected_status
    daemon.assert_not_called()


def test_migration_status_queries_explicit_uuid(migration_client, migration_service):
    migration_service.query_drain.return_value = {
        "success": True,
        "status": "cancelled",
        "drain_id": "uuid-api-2",
        "state": "migration_cancelled",
    }
    response = migration_client.get("/node-manager/migration/status?drain_id=uuid-api-2")

    assert response.status_code == 200
    assert response.json()["state"] == "migration_cancelled"
    migration_service.query_drain.assert_called_once_with("uuid-api-2")


def test_migration_cancel_uses_explicit_uuid(migration_client, migration_service):
    migration_service.cancel_drain.return_value = {
        "success": True,
        "status": "cancelling",
        "drain_id": "uuid-api-3",
    }
    response = migration_client.post("/node-manager/migration/cancel", json={"drain_id": "uuid-api-3"})

    assert response.status_code == 200
    assert response.json()["status"] == "cancelling"
    migration_service.cancel_drain.assert_called_once_with("uuid-api-3")


def test_migration_cannot_cancel_the_latest_job_implicitly(migration_client):
    with patch("motor.node_manager.api_server.migration_api.Daemon") as daemon:
        response = migration_client.post("/node-manager/migration/cancel", json={})
    assert response.status_code == 400
    daemon.assert_not_called()


@pytest.mark.parametrize(
    "method,path,service_method",
    [
        ("post", "/node-manager/migration/trigger", "submit_directed_drain"),
        ("get", "/node-manager/migration/status?drain_id=known", "query_drain"),
        ("post", "/node-manager/migration/cancel", "cancel_drain"),
    ],
)
@pytest.mark.parametrize(
    "upstream_code,expected_code",
    [(404, 404), (409, 409), (503, 502), pytest.param(None, 503, id="missing-local-service")],
)
def test_migration_error_mapping(
    migration_client, migration_payload, method, path, service_method, upstream_code, expected_code
):
    import requests

    with patch("motor.node_manager.api_server.migration_api.Daemon") as daemon:
        daemon.return_value._services = {}
        if upstream_code is not None:
            upstream = requests.Response()
            upstream.status_code = upstream_code
            upstream._content = b'{"error":"UUID is not active"}'
            service = MagicMock()
            getattr(service, service_method).side_effect = requests.HTTPError(response=upstream)
            daemon.return_value._services["kv_store"] = SimpleNamespace(migration=service)
        data = migration_payload if path.endswith("/trigger") else {"drain_id": "known"}
        response = getattr(migration_client, method)(path, **({"json": data} if method == "post" else {}))

    assert response.status_code == expected_code
    if upstream_code in (404, 409):
        assert response.json()["detail"] == {"error": "UUID is not active"}


@pytest.mark.parametrize("concurrency", [True, False, 1.5, "1", 0, -1])
def test_migration_rejects_nonpositive_or_noninteger_concurrency(migration_client, migration_payload, concurrency):
    with patch("motor.node_manager.api_server.migration_api.Daemon") as daemon:
        response = migration_client.post(
            "/node-manager/migration/trigger", json={**migration_payload, "max_concurrency": concurrency}
        )
    assert response.status_code == 400
    daemon.assert_not_called()


def test_stop_rejects_a_reused_pod_ip(migration_client, monkeypatch):
    monkeypatch.setenv("POD_UID", "replacement")
    with patch("motor.node_manager.api_server.migration_api.Daemon") as daemon:
        assert migration_client.post("/node-manager/migration/stop", json={"source_uid": "old"}).status_code == 409
        daemon.assert_not_called()


@pytest.mark.parametrize(
    "invalid", [{"timeout": True}, {"timeout": "3"}, {"scale_in": False}, {"retiring_pod_ips": ["10.0.0.2"]}]
)
def test_provider_exit_rejects_invalid_budget_or_retired_scope(migration_client, invalid):
    response = migration_client.post("/node-manager/migration/prepare-exit", json={"source_uid": "uid-4", **invalid})
    assert response.status_code == 400


@pytest.mark.parametrize("engine_running", [True, False])
@pytest.mark.parametrize(
    "startup_switch, master, protected, management_tls",
    [
        (None, None, False, False),
        (None, True, False, True),
        (None, False, False, False),
        ("true", True, True, False),
        ("true", True, True, True),
        ("false", True, False, False),
        ("true", False, False, False),
        ("true", None, False, False),
    ],
)
def test_kubelet_hook_uses_local_pause_for_provider_exit(
    migration_client, monkeypatch, engine_running, startup_switch, master, protected, management_tls
):
    """Run the deployed hook against the API before and after engine shutdown."""
    import runpy
    from pathlib import Path
    from motor.node_manager.api_server import migration_api, node_manager_api

    source = Path(__file__).resolve().parents[3] / "examples/deployer/prestop/prestop.py"
    main = runpy.run_path(str(source))["main"]
    elapsed = [0.0]
    clock = SimpleNamespace(monotonic=lambda: elapsed[0], sleep=lambda sec: elapsed.__setitem__(0, elapsed[0] + sec))
    monkeypatch.setitem(main.__globals__, "time", clock)
    monkeypatch.setenv("POD_UID", "uid-4")
    monkeypatch.setenv("POD_IP", "127.0.0.1")
    monkeypatch.setenv("MMC_LOCAL_SERVICE_MODE", "standalone")
    if startup_switch is None:
        monkeypatch.delenv("MMC_ENABLE_KV_DRAIN_ON_SCALE_IN", raising=False)
    else:
        monkeypatch.setenv("MMC_ENABLE_KV_DRAIN_ON_SCALE_IN", startup_switch)
    monkeypatch.setattr(sys, "argv", [str(source)])
    tls = {"enable_tls": management_tls, "ca_file": "management-ca.pem"}
    controller_ft = {} if master is None else {"enable_kv_migration": master}
    monkeypatch.setitem(
        main.__globals__,
        "load_config",
        lambda: {
            "motor_deploy_config": {"tls_config": {"mgmt_tls_config": tls, "infer_tls_config": {"enable_tls": True}}},
            "motor_controller_config": {"fault_tolerance_config": controller_ft},
            "kv_cache_store_config": {"backend": "memcache", "enable_kv_drain_on_scale_in": True},
        },
    )
    heartbeat = MagicMock()
    heartbeat.get_engine_metrics_targets.return_value = ["http://127.0.0.1:8080/metrics"] if engine_running else []
    monkeypatch.setitem(main.__globals__, "wait_for_engine_drain", lambda *_, **__: True)
    monkeypatch.setattr(node_manager_api, "HeartbeatManager", lambda: heartbeat)
    service = MagicMock()
    service.prepare_local_exit.return_value = {"drain_id": "hw", "source_uid": "uid-4", "state": "migration_completed"}
    monkeypatch.setattr(migration_api, "_get_memcache_migration_service", lambda **_: service)

    def post(url, timeout, payload=None, **kwargs):
        path = "/node-manager/migration/prepare-exit" if payload else "/node-manager/pause"
        scheme = "https" if management_tls else "http"
        assert url == f"{scheme}://127.0.0.1:1026" + path
        assert kwargs["tls_config"] == tls
        response = migration_client.post(path, json=payload or {})
        return response.status_code, response.text

    monkeypatch.setitem(main.__globals__, "_http_post_json", post)
    with pytest.raises(SystemExit) as success:
        main()
    assert success.value.code == 0
    if not protected:
        heartbeat.pause_all_endpoints.assert_called_once_with()
        service.prepare_local_exit.assert_not_called()
        return
    service.prepare_local_exit.assert_called_once_with("uid-4", engine_running, 30)
    assert elapsed[0] == 0
    for state in ("migration_failed", "migration_cancelled"):
        service.prepare_local_exit.return_value = {"drain_id": "hw", "source_uid": "uid-4", "state": state}
        with pytest.raises(SystemExit) as failed:
            main()
        assert failed.value.code == 1 and elapsed[0] == 0
    for invalid_receipt in (
        {"source_uid": "uid-4", "drain_id": "hw"},
        {"source_uid": "wrong-pod", "drain_id": "hw", "state": "migration_completed"},
    ):
        before = elapsed[0]
        service.prepare_local_exit.return_value = invalid_receipt
        with pytest.raises(SystemExit) as invalid:
            main()
        assert invalid.value.code == 1 and elapsed[0] == before + 30
    service.prepare_local_exit.side_effect = RuntimeError("unverified source")
    with pytest.raises(SystemExit) as refused:
        main()
    assert refused.value.code == 1


def test_fast_only_proxy_uses_rank_scoped_retry_without_enabling_shrink(client):
    config = _ft_config(False)
    config.fault_tolerance_config.enable_fast_recovery_proxy = True
    with patch("motor.node_manager.api_server.node_manager_api.Daemon") as daemon:
        daemon.return_value.config = config
        response = client.post(
            "/node-manager/fault-tolerance/apply",
            json={
                "endpoint_ids": [1],
                "instruction": "retry",
                "params": {"abort": True},
                "request_id": "retry",
            },
        )
    assert response.status_code == 200
    daemon.return_value.engine_ft_manager.apply.assert_called_once()


@pytest.mark.parametrize("known_failure", [True, False])
def test_provider_exit_exposes_submission_certainty(migration_client, monkeypatch, known_failure):
    from motor.node_manager.api_server import migration_api
    from motor.node_manager.core.services.memcache.migration import DrainNotStarted

    monkeypatch.setenv("POD_UID", "uid-4")
    error = DrainNotStarted("No targets") if known_failure else RuntimeError("Receipt unknown")
    service = SimpleNamespace(prepare_local_exit=MagicMock(side_effect=error))
    monkeypatch.setattr(migration_api, "_get_memcache_migration_service", lambda: service)
    response = migration_client.post("/node-manager/migration/prepare-exit", json={"source_uid": "uid-4"})
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "drain_not_started" if known_failure else "drain_outcome_unknown",
        "source_uid": "uid-4",
        "reason": str(error),
    }


@pytest.mark.parametrize(
    "status, detail, waits",
    [
        (409, {"code": "drain_not_started", "source_uid": "uid-4"}, False),
        (409, {"code": "drain_outcome_unknown", "source_uid": "uid-4"}, True),
        (409, {"code": "drain_not_started", "source_uid": "other"}, True),
        (409, "legacy error", True),
        (503, {"code": "drain_not_started", "source_uid": "uid-4"}, True),
    ],
)
def test_prestop_ends_early_only_for_verified_non_submission(monkeypatch, status, detail, waits):
    import json
    import runpy
    from pathlib import Path

    source = Path(__file__).resolve().parents[3] / "examples/deployer/prestop/prestop.py"
    wait = runpy.run_path(str(source))["wait_for_scale_in_drain"]
    clock = SimpleNamespace(monotonic=lambda: 0, sleep=MagicMock())
    monkeypatch.setitem(wait.__globals__, "time", clock)
    monkeypatch.setitem(wait.__globals__, "_http_post_json", lambda *a, **k: (status, json.dumps({"detail": detail})))
    monkeypatch.setenv("POD_UID", "uid-4")
    assert wait("http://nm", True, 30) is False
    assert clock.sleep.call_count == int(waits)
