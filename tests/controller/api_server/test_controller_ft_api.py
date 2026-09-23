# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN AS IS BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import motor.controller.api_server.controller_api as controller_api
from motor.config.controller import ControllerConfig
from motor.controller.fault_tolerance.dp_scale_down import FtPhase, FtRuntime, get_ft_runtime_store


@pytest.fixture(autouse=True)
def clear_runtime_store():
    store = get_ft_runtime_store()
    store.clear()
    yield
    store.clear()


@pytest.fixture
def client():
    config = ControllerConfig()
    config.fault_tolerance_config.enable_dp_scale_down = True
    return TestClient(controller_api.ControllerAPI(config).app)


def test_get_fault_tolerance_status_is_disabled_when_scale_down_is_disabled():
    config = ControllerConfig()
    config.fault_tolerance_config.enable_dp_scale_down = False
    api_instance = controller_api.ControllerAPI(config)
    with patch('motor.controller.api_server.controller_api.get_ft_runtime_store') as get_store:
        response = TestClient(api_instance.app).get('/controller/fault_tolerance/status')

    assert response.status_code == 200
    assert response.json()['data'] == {'phase': FtPhase.DISABLED.value, 'instances': []}
    get_store.assert_not_called()


def test_get_fault_tolerance_status_all(client):
    get_ft_runtime_store().put(FtRuntime(instance_id=42, phase=FtPhase.SCALING_DOWN, request_id='ft-42'))
    active = SimpleNamespace(
        id=7,
        get_all_endpoints=lambda **_kwargs: [SimpleNamespace(id=0), SimpleNamespace(id=1)],
    )
    instance_manager = MagicMock()
    instance_manager.get_active_instances.return_value = [active]

    with patch("motor.controller.api_server.controller_api.InstanceManager", return_value=instance_manager):
        response = client.get('/controller/fault_tolerance/status')

    assert response.status_code == 200
    statuses = {status["instance_id"]: status for status in response.json()["data"]["instances"]}
    assert statuses[7]["phase"] == FtPhase.NORMAL.value
    assert statuses[7]["original_dp_ranks"] == [0, 1]
    assert statuses[7]["can_serve"] is True
    assert statuses[42]["phase"] == FtPhase.SCALING_DOWN.value


def test_get_fault_tolerance_status_one_and_not_found(client):
    get_ft_runtime_store().put(FtRuntime(instance_id=9, phase=FtPhase.SCALED_DOWN_RUNNING, dead_committed=[1]))
    active = SimpleNamespace(id=10, get_all_endpoints=lambda **_kwargs: [SimpleNamespace(id=0)])
    instance_manager = MagicMock()
    instance_manager.get_active_instances.return_value = [active]

    with patch("motor.controller.api_server.controller_api.InstanceManager", return_value=instance_manager):
        response = client.get('/controller/fault_tolerance/status?instance_id=9')
        normal = client.get('/controller/fault_tolerance/status?instance_id=10')
        missing = client.get('/controller/fault_tolerance/status?instance_id=11')

    assert response.json()['data']['dead_committed'] == [1]
    assert normal.json()["data"]["phase"] == FtPhase.NORMAL.value
    assert missing.status_code == 404
