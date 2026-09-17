# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Regression tests for ROLE_U support in KVA register/select flows."""

from unittest.mock import Mock, patch

import pytest

from motor.common.resources.instance import PDRole
from motor.coordinator.api_client.conductor_api_client import (
    ConductorApiClient,
    conductor_instance_id,
)


def _build_instance(role: PDRole) -> Mock:
    instance = Mock()
    instance.role = role
    endpoint = Mock()
    instance.endpoints = {"pod-0": {0: endpoint}}
    instance.get_all_endpoints.return_value = (endpoint,)
    return instance


@pytest.mark.parametrize(
    ("role", "instance_id", "expected"),
    [
        (PDRole.ROLE_U, 7, "vllm-union-7"),
        (PDRole.ROLE_P, 3, "vllm-prefill-3"),
    ],
)
def test_conductor_instance_id(role: PDRole, instance_id: int, expected: str) -> None:
    instance = _build_instance(role)
    instance.id = instance_id
    assert conductor_instance_id(instance) == expected


def test_register_post_uses_union_conductor_id_for_role_u() -> None:
    instance = _build_instance(PDRole.ROLE_U)
    instance.id = 2
    instance.model_name = "qwen3-8B"
    endpoint = Mock()
    endpoint.id = 0
    endpoint.ip = "10.0.0.1"

    mock_config = Mock()
    # _kv_reg() — scheduler_config.kv_conductor_config: conductor addr, engine type, block size
    mock_config.scheduler_config.kv_conductor_config.engine_type = "vLLM"
    mock_config.scheduler_config.kv_conductor_config.block_size = 128
    mock_config.scheduler_config.kv_conductor_config.conductor_service = "kv-conductor"
    mock_config.scheduler_config.kv_conductor_config.http_server_port = 13333
    mock_config.scheduler_config.kv_conductor_config.endpoint = "tcp://*:5557"
    mock_config.scheduler_config.kv_conductor_config.npu_endpoint = ""
    mock_config.scheduler_config.kv_conductor_config.xpu_endpoint = ""
    mock_config.scheduler_config.kv_conductor_config.cpu_endpoint = ""
    mock_config.scheduler_config.kv_conductor_config.disk_endpoint = ""
    mock_config.scheduler_config.kv_conductor_config.replay_endpoint = ""
    # _resolve_store_backend uses _kv_reg().store_backend
    mock_config.scheduler_config.kv_conductor_config.store_backend = "Mooncake"

    with (
        patch.object(ConductorApiClient, "coordinator_config", mock_config),
        patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient") as mock_http_client,
    ):
        mock_http_client.return_value.__enter__.return_value.post.return_value = None
        ConductorApiClient.register_post(instance, endpoint)

    register_payload = mock_http_client.return_value.__enter__.return_value.post.call_args[0][1]
    assert register_payload["instance_id"] == "vllm-union-2"


def test_register_post_formats_ipv6_endpoint_and_conductor_address() -> None:
    instance = _build_instance(PDRole.ROLE_P)
    instance.id = 3
    instance.model_name = "qwen3-8B"
    endpoint = Mock()
    endpoint.id = 2
    endpoint.ip = "2001:db8::1"

    mock_config = Mock()
    # _kv_reg() — scheduler_config.kv_conductor_config: conductor addr, engine type, block size, endpoint patterns
    mock_config.scheduler_config.kv_conductor_config.engine_type = "vLLM"
    mock_config.scheduler_config.kv_conductor_config.block_size = 128
    mock_config.scheduler_config.kv_conductor_config.model_path = ""
    mock_config.scheduler_config.kv_conductor_config.conductor_service = "2001:db8::10"
    mock_config.scheduler_config.kv_conductor_config.http_server_port = 13333
    mock_config.scheduler_config.kv_conductor_config.npu_endpoint = ""
    mock_config.scheduler_config.kv_conductor_config.xpu_endpoint = ""
    mock_config.scheduler_config.kv_conductor_config.cpu_endpoint = ""
    mock_config.scheduler_config.kv_conductor_config.disk_endpoint = ""
    mock_config.scheduler_config.kv_conductor_config.endpoint = "tcp://*:5557"
    mock_config.scheduler_config.kv_conductor_config.replay_endpoint = "tcp://*:6667"
    mock_config.scheduler_config.kv_conductor_config.store_backend = "Mooncake"

    with (
        patch.object(ConductorApiClient, "coordinator_config", mock_config),
        patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient") as mock_http_client,
    ):
        mock_http_client.return_value.__enter__.return_value.post.return_value = None
        ConductorApiClient.register_post(instance, endpoint)

    mock_http_client.assert_called_once()
    assert mock_http_client.call_args.kwargs["address"] == "[2001:db8::10]:13333"
    register_payload = mock_http_client.return_value.__enter__.return_value.post.call_args[0][1]
    # Endpoints are now wrapped in medium_endpoints dict (npu/cpu/disk).
    # When per-medium fields are empty, all fall back to the legacy "endpoint".
    assert register_payload["medium_endpoints"]["npu"] == "tcp://[2001:db8::1]:5559"
    assert register_payload["replay_endpoint"] == "tcp://[2001:db8::1]:6669"


def _mock_enabled_kv_reg():
    """Return a KV conductor config with registration enabled for unit tests."""
    mock_reg = Mock()
    mock_reg.conductor_service = "kv-conductor"
    mock_reg.store_backend = "YuanRong"
    return mock_reg


def test_register_kv_instance_supports_role_u() -> None:
    instances = [
        _build_instance(PDRole.ROLE_P),
        _build_instance(PDRole.ROLE_U),
        _build_instance(PDRole.ROLE_D),
    ]
    with (
        patch.object(ConductorApiClient, "_kv_reg", return_value=_mock_enabled_kv_reg()),
        patch.object(ConductorApiClient, "_register_hbm_dp") as mock_hbm_dp,
        patch.object(ConductorApiClient, "_register_yuanrong_dp") as mock_yuanrong_dp,
        patch.object(ConductorApiClient, "_register_pool"),
    ):
        ConductorApiClient.register_kv_instance(instances)

    # Depending on backend mode, either _register_hbm_dp or _register_yuanrong_dp
    # is called for each KVA-eligible instance endpoint.
    # Signature: _register_*_dp(cls, reg, store_backend, instance, endpoint)
    # call.args excludes cls, so instance is at index 2.
    registered_method = mock_hbm_dp if mock_hbm_dp.call_count else mock_yuanrong_dp
    assert registered_method.call_count == 2
    called_roles = {call.args[2].role for call in registered_method.call_args_list}
    assert called_roles == {PDRole.ROLE_P, PDRole.ROLE_U}


def test_unregister_kv_instance_supports_role_u() -> None:
    instances = [
        _build_instance(PDRole.ROLE_P),
        _build_instance(PDRole.ROLE_U),
        _build_instance(PDRole.ROLE_D),
    ]
    with (
        patch.object(ConductorApiClient, "_kv_reg", return_value=_mock_enabled_kv_reg()),
        patch.object(ConductorApiClient, "unregister_post") as mock_unregister_post,
    ):
        ConductorApiClient.unregister_kv_instance(instances)

    assert mock_unregister_post.call_count == 2
    called_roles = {call.args[0].role for call in mock_unregister_post.call_args_list}
    assert called_roles == {PDRole.ROLE_P, PDRole.ROLE_U}
