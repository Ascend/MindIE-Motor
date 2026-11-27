# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN AS IS BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import motor.controller.api_server.controller_api as controller_api
from motor.config.controller import ControllerConfig
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, FtRuntime, get_ft_runtime_store


@pytest.fixture(autouse=True)
def clear_runtime_store():
    store = get_ft_runtime_store()
    store.clear()
    yield
    store.clear()
    controller_api.ServingOverlay.release(7)


@pytest.fixture
def client():
    config = ControllerConfig()
    config.fault_tolerance_config.enable_dp_scale_down = True
    return TestClient(controller_api.ControllerAPI(config).app)


@pytest.mark.parametrize("enabled", [True, False])
def test_exit_targets_uses_startup_migration_switch(enabled):
    config = ControllerConfig()
    config.fault_tolerance_config.enable_kv_migration = enabled
    api = controller_api.ControllerAPI(config)
    client = TestClient(api.app)
    config.fault_tolerance_config.enable_kv_migration = not enabled
    api.update_config(config)
    with patch("motor.controller.fault_tolerance.kv_migration_service.plan_provider_exit", return_value={}) as plan:
        response = client.post(
            "/controller/migration/exit-targets", json={"source_uid": "uid", "source_ip": "10.0.0.1"}
        )
        if enabled:
            with patch.object(controller_api, "time", monotonic=lambda: 100):
                budgeted = client.post(
                    "/controller/migration/exit-targets",
                    json={"source_uid": "uid", "source_ip": "10.0.0.1", "timeout_sec": 0.5},
                )
            assert budgeted.status_code == 200
            assert plan.call_args.kwargs["deadline"] == 100.5
    assert response.status_code == (200 if enabled else 404)
    for invalid in (
        {"scale_in": False},
        {"retiring_pod_ips": ["10.0.0.2"]},
        {"timeout_sec": 0},
        {"timeout_sec": True},
        {"timeout_sec": 601},
    ):
        invalid_scope = client.post(
            "/controller/migration/exit-targets", json={"source_uid": "uid", "source_ip": "10.0.0.1", **invalid}
        )
        assert invalid_scope.status_code == (400 if enabled else 404)
    assert plan.call_count == 2 * int(enabled)
    malformed = client.post("/controller/migration/exit-targets", content=b"{")
    assert malformed.status_code == (400 if enabled else 404)


@pytest.mark.parametrize("enabled", [False, True], ids=["disabled", "enabled"])
def test_recover_respects_scale_up_switch(enabled):
    config = ControllerConfig()
    config.fault_tolerance_config.enable_dp_scale_down = True
    config.fault_tolerance_config.enable_dp_scale_up = enabled
    api_client = TestClient(controller_api.ControllerAPI(config).app)
    accepted = [{"instance_id": 1, "action": "scale_up"}]
    with patch("motor.controller.api_server.controller_api.FaultManager") as manager:
        manager.return_value.start_user_recovery.return_value = accepted
        response = api_client.post("/controller/fault_tolerance/recover")
    assert response.status_code == (200 if enabled else 400)
    if enabled:
        assert response.json()["data"]["instances"] == accepted
        manager.return_value.start_user_recovery.assert_called_once_with()
    else:
        assert response.json()["detail"] == "DP scale-up is not enabled"
        manager.assert_not_called()


def test_status_disabled_skips_runtime_query():
    config = ControllerConfig()
    config.fault_tolerance_config.enable_fault_tolerance = False
    with patch.object(controller_api, "get_ft_runtime_store") as read_store:
        response = TestClient(controller_api.ControllerAPI(config).app).get("/controller/fault_tolerance/status")
    assert response.status_code == 200
    assert response.json()["data"] == {"phase": FtPhase.DISABLED.value, "instances": []}
    read_store.assert_not_called()


def test_get_fault_tolerance_status_all(client):
    get_ft_runtime_store().put(FtRuntime(instance_id=42, phase=FtPhase.SCALING_DOWN, request_id='ft-42'))
    active = _status_instance()
    instance_manager = MagicMock()
    instance_manager.get_active_instances.return_value = [active]
    instance_manager.get_instance.side_effect = lambda instance_id: active if instance_id == active.id else None

    with patch("motor.controller.api_server.controller_api.InstanceManager", return_value=instance_manager):
        response = client.get("/controller/fault_tolerance/status")
    assert response.status_code == 200
    statuses = {status["instance_id"]: status for status in response.json()["data"]["instances"]}
    assert statuses[7]["phase"] == FtPhase.NORMAL.value
    assert statuses[7]["alive_dp_ranks"] == [0, 1]
    assert statuses[7]["can_serve"] is True
    assert statuses[42]["phase"] == FtPhase.SCALING_DOWN.value


@pytest.mark.parametrize("active_state", ["ACTIVE", "INACTIVE"])
def test_get_fault_tolerance_status_one_and_not_found(client, active_state):
    get_ft_runtime_store().put(FtRuntime(instance_id=9, phase=FtPhase.SCALED_DOWN_RUNNING, dead_committed=[1]))
    active = _status_instance()
    active.id = 10
    active.status = active_state
    instance_manager = MagicMock()
    instance_manager.get_instances.return_value = [active]
    instance_manager.get_active_instances.return_value = [active] if active_state == "ACTIVE" else []
    instance_manager.get_instance.side_effect = lambda instance_id: active if instance_id == active.id else None

    with patch("motor.controller.api_server.controller_api.InstanceManager", return_value=instance_manager):
        summary = client.get("/controller/fault_tolerance/status?instance_id=9").json()["data"]
        normal = client.get('/controller/fault_tolerance/status?instance_id=10')
        listed = client.get("/controller/fault_tolerance/status").json()["data"]["instances"]
        missing = client.get('/controller/fault_tolerance/status?instance_id=11')

    assert summary["dead_dp_ranks"] == [1]
    assert summary["phase"] == FtPhase.SCALED_DOWN_RUNNING.value
    assert normal.json()["data"]["phase"] == FtPhase.NORMAL.value
    assert normal.json()["data"] in listed
    if active_state == "INACTIVE":
        assert not normal.json()["data"]["can_serve"]
    assert get_ft_runtime_store().get(10) is None
    assert missing.status_code == 404


def _status_instance():
    import time
    from motor.common.resources.instance import Instance, InsStatus, ParallelConfig
    from motor.common.resources.endpoint import Endpoint, EndpointStatus

    instance = Instance(
        id=7, job_name="status", model_name="model", role="decode", parallel_config=ParallelConfig(dp_size=2)
    )
    instance.status = InsStatus.ACTIVE
    instance.add_endpoints(
        "192.0.2.1",
        {
            rank: Endpoint(
                id=rank,
                ip="192.0.2.1",
                business_port=str(8000 + rank),
                status=EndpointStatus.NORMAL,
                hb_timestamp=time.time(),
            )
            for rank in (0, 1)
        },
    )
    return instance


@pytest.mark.parametrize(
    "condition,phase,retired,can_serve",
    [
        ("inactive", FtPhase.NORMAL, [], False),
        ("stale", FtPhase.NORMAL, [], False),
        ("unpublished", FtPhase.SCALED_DOWN_RUNNING, [1], False),
        ("healthy", FtPhase.NORMAL, [], True),
        ("retired-stale", FtPhase.SCALED_DOWN_RUNNING, [1], True),
        ("warming-up", FtPhase.WARMING_UP, [], True),
        ("held", FtPhase.NORMAL, [], False),
        ("fast", FtPhase.FAST_RECOVERING, [1], False),
    ],
)
def test_status_list_and_single_agree_on_topology_and_serving(client, condition, phase, retired, can_serve):
    from motor.common.resources.instance import InsStatus

    instance, store = _status_instance(), get_ft_runtime_store()
    store.transition(
        7,
        phase=phase,
        stage={"fast": "executing", "unpublished": "publication_pending"}.get(condition, "idle"),
        serving_published=condition != "unpublished",
        serving_dp_ranks=[0] if condition == "warming-up" else [],
        dead_committed=retired,
    )
    if condition == "inactive":
        instance.status = InsStatus.INACTIVE
    if condition == "stale":
        instance.get_all_endpoints()[0].hb_timestamp = 1
    if condition in {"retired-stale", "warming-up"}:
        instance.get_all_endpoints()[1].hb_timestamp = 1
    if condition == "held":
        controller_api.ServingOverlay.hold(7)
    instance.add_node_mgr("192.0.2.1", "1026")
    im = MagicMock()
    im.get_instances.return_value = [instance]
    im.get_instance.return_value = instance
    before = store.get(7)
    with patch.object(controller_api, "InstanceManager", return_value=im):
        response = client.get("/controller/fault_tolerance/status?instance_id=7")
        listed = client.get("/controller/fault_tolerance/status").json()["data"]["instances"][0]
    assert response.status_code == 200
    assert (
        listed
        == response.json()["data"]
        == {
            "instance_id": 7,
            "phase": phase,
            "can_serve": can_serve,
            "alive_dp_ranks": [0] if retired else [0, 1],
            "dead_dp_ranks": retired,
            "last_error": None,
        }
    )
    assert store.get(7) == before


def test_status_missing_instance_cannot_claim_serving_or_known_topology(client):
    store = get_ft_runtime_store()
    store.transition(7, phase=FtPhase.NORMAL, serving_published=True)
    im = MagicMock()
    im.get_instance.return_value = None
    im.get_active_instances.return_value = []
    with patch.object(controller_api, "InstanceManager", return_value=im):
        status = client.get("/controller/fault_tolerance/status?instance_id=7").json()["data"]
    assert status["alive_dp_ranks"] == []
    assert status["can_serve"] is False


def test_status_rejects_invalid_id_before_reading_instances(client):
    with patch.object(controller_api, "InstanceManager") as manager:
        response = client.get("/controller/fault_tolerance/status?instance_id=bad")
    assert response.status_code == 400
    manager.assert_not_called()
