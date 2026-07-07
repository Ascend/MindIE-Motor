# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace
from unittest import mock

from motor.engine_server.core.mgmt_endpoint import MgmtEndpoint


def _make_config(dp_rank: int, enable_virtual_inference: bool = True):
    health_check_config = SimpleNamespace(
        enable_virtual_inference=enable_virtual_inference,
        npu_usage_threshold=3,
        max_failure_count=6,
    )
    deploy_config = SimpleNamespace(
        mgmt_tls_config=None,
        infer_tls_config=None,
        health_check_config=health_check_config,
    )
    endpoint_config = SimpleNamespace(
        host="127.0.0.1",
        mgmt_port=9001,
        role="decode",
        snapshot_metadata=None,
        dp_rank=dp_rank,
        deploy_config=deploy_config,
    )
    args = SimpleNamespace(headless=False)

    config = mock.MagicMock()
    config.get_endpoint_config.return_value = endpoint_config
    config.get_args.return_value = args
    return config, health_check_config


@mock.patch("motor.engine_server.core.mgmt_endpoint.attach_metrics_router")
@mock.patch("motor.engine_server.core.mgmt_endpoint.SimInference")
@mock.patch("motor.engine_server.core.mgmt_endpoint.HealthCollector")
def test_mgmt_endpoint_disables_virtual_inference_on_non_dp0(
    _mock_health_collector,
    _mock_sim_inference,
    _mock_attach_metrics,
):
    config, health_check_config = _make_config(dp_rank=2, enable_virtual_inference=True)

    MgmtEndpoint(config)

    assert health_check_config.enable_virtual_inference is False


@mock.patch("motor.engine_server.core.mgmt_endpoint.attach_metrics_router")
@mock.patch("motor.engine_server.core.mgmt_endpoint.SimInference")
@mock.patch("motor.engine_server.core.mgmt_endpoint.HealthCollector")
def test_mgmt_endpoint_keeps_virtual_inference_on_dp0(
    _mock_health_collector,
    _mock_sim_inference,
    _mock_attach_metrics,
):
    config, health_check_config = _make_config(dp_rank=0, enable_virtual_inference=True)

    MgmtEndpoint(config)

    assert health_check_config.enable_virtual_inference is True
