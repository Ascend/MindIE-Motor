# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN AS IS BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import patch

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

    response = client.get('/controller/fault_tolerance/status')

    assert response.status_code == 200
    assert response.json()['data']['instances'][0]['instance_id'] == 42


def test_get_fault_tolerance_status_one_and_not_found(client):
    get_ft_runtime_store().put(FtRuntime(instance_id=9, phase=FtPhase.SCALED_DOWN_RUNNING, dead_committed=[1]))

    response = client.get('/controller/fault_tolerance/status?instance_id=9')
    missing = client.get('/controller/fault_tolerance/status?instance_id=10')

    assert response.json()['data']['dead_committed'] == [1]
    assert missing.status_code == 404
