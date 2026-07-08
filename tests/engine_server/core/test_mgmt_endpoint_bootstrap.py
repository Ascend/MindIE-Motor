# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from motor.engine_server.constants.constants import INIT_STATUS, STATUS_KEY
from motor.engine_server.core.mgmt_endpoint import MgmtEndpoint
from tests.engine_server.core.test_vllm_config import _make_endpoint_config


@pytest.fixture(autouse=True)
def _skip_metrics_router():
    with patch("motor.engine_server.core.mgmt_endpoint.attach_metrics_router"):
        yield


def test_status_returns_initial_before_attach_engine():
    endpoint_config = _make_endpoint_config()
    mgmt = MgmtEndpoint(endpoint_config)

    response = TestClient(mgmt.app).get("/status")

    assert response.status_code == 200
    assert response.json() == {STATUS_KEY: INIT_STATUS}
    assert mgmt.sim_inference is None


@patch("motor.engine_server.core.sim_inference.SimInference.from_config")
def test_attach_engine_enables_sim_inference(mock_from_config):
    endpoint_config = _make_endpoint_config()
    mgmt = MgmtEndpoint(endpoint_config)
    mock_sim = MagicMock()
    mock_from_config.return_value = mock_sim

    mock_config = MagicMock()
    mock_config.get_endpoint_config.return_value = endpoint_config
    mock_config.get_args.return_value = MagicMock(headless=False)

    mgmt.attach_engine(mock_config)

    assert mgmt.sim_inference is mock_sim
    assert mgmt._engine_attached is True
    mock_from_config.assert_called_once_with(mock_config)


@patch("motor.engine_server.core.sim_inference.SimInference.from_config")
def test_attach_engine_is_idempotent(mock_from_config):
    endpoint_config = _make_endpoint_config()
    mgmt = MgmtEndpoint(endpoint_config)
    mock_sim = MagicMock()
    mock_from_config.return_value = mock_sim

    mock_config = MagicMock()
    mock_config.get_endpoint_config.return_value = endpoint_config
    mock_config.get_args.return_value = MagicMock(headless=False)

    mgmt.attach_engine(mock_config)
    mgmt.attach_engine(mock_config)

    mock_from_config.assert_called_once()


def test_shutdown_without_attach_engine_does_not_raise():
    endpoint_config = _make_endpoint_config()
    mgmt = MgmtEndpoint(endpoint_config)
    mgmt._server = MagicMock()
    mgmt._server_thread = MagicMock()
    mgmt._server_thread.is_alive.return_value = False

    mgmt.shutdown()
