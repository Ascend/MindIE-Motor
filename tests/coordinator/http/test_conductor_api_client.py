# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for ConductorApiClient query and registration logic."""

import pytest
from unittest.mock import Mock, patch

from motor.coordinator.api_client import conductor_api_client as cac_module
from motor.coordinator.api_client.conductor_api_client import ConductorApiClient, TENANT_ID


def _make_mock_instance(instance_id: int):
    """Create a mock prefill instance with one endpoint for testing."""
    endpoint = Mock()
    endpoint.id = 0
    endpoint.ip = "127.0.0.1"

    instance = Mock()
    instance.id = instance_id
    instance.model_name = "test-model"
    instance.role = "prefill"
    instance.endpoints = {"pod-0": {0: endpoint}}
    instance.get_all_endpoints.return_value = (endpoint,)
    return instance


def _mock_successful_query(mock_http, response=None):
    if response is None:
        response = {TENANT_ID: {}}
    mock_client = Mock()
    mock_client.post.return_value = response
    mock_http.reset_mock(return_value=True, side_effect=True)
    mock_http.return_value.__enter__.return_value = mock_client


def _mock_failed_query(mock_http):
    mock_http.reset_mock(return_value=True, side_effect=True)
    mock_http.return_value.__enter__.side_effect = ConnectionError("connection refused")


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_return_value_on_success(mock_http):
    """On success, query_conductor returns the response dict."""
    expected = {TENANT_ID: {"vllm-prefill-1": {"longest_matched": 100, "DP": {"0": 50}}}}
    _mock_successful_query(mock_http, response=expected)
    instances = [_make_mock_instance(1)]

    result = ConductorApiClient.query_conductor(instances, [1, 2, 3])
    assert result == expected


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_return_value_on_failure(mock_http):
    """On failure, query_conductor returns an empty dict."""
    _mock_failed_query(mock_http)
    instances = [_make_mock_instance(1)]

    result = ConductorApiClient.query_conductor(instances, [1, 2, 3])
    assert result == {}


# ── Registration dispatch tests ──────────────────────────────────────


def _setup_reg_config(store_backend, pool_endpoint="", xpu_endpoint="",
                      cpu_endpoint="", disk_endpoint="", replay_endpoint=""):
    """Patch ConductorApiClient's config for registration testing."""
    from motor.config.coordinator import KvConductorConfig, SchedulerConfig

    reg = KvConductorConfig(
        store_backend=store_backend,
        pool_endpoint=pool_endpoint,
        xpu_endpoint=xpu_endpoint,
        cpu_endpoint=cpu_endpoint,
        disk_endpoint=disk_endpoint,
        replay_endpoint=replay_endpoint,
    )
    sched = SchedulerConfig(kv_conductor_config=reg)
    return patch.object(ConductorApiClient, "coordinator_config",
                        scheduler_config=sched,
                        prefill_kv_event_config=Mock(
                            engine_type="vLLM", block_size=128,
                            conductor_service="kv-conductor",
                            http_server_port=13333, model_path="",
                            replay_endpoint="", endpoint="",
                        ))


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_yuanrong_registration_dispatches_per_dp(mock_http):
    """YuanRong: per-DP multi-port, not pool."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)
    ConductorApiClient._pool_registered = False

    with _setup_reg_config("YuanRong", xpu_endpoint="tcp://*:15557",
                           cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"):
        ConductorApiClient.register_kv_instance([instance])

    calls = mock_client.post.call_args_list
    assert len(calls) == 1  # one DP = one call
    payload = calls[0][0][1]
    assert "medium_endpoints" in payload
    assert payload["store_backend"] == "YuanRong"
    assert "xpu" in str(payload["medium_endpoints"])
    assert "cpu" in str(payload["medium_endpoints"])
    assert "disk" in str(payload["medium_endpoints"])


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_mooncake_registration_includes_pool_plus_hbm(mock_http):
    """Mooncake: pool once + per-DP HBM."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)
    ConductorApiClient._pool_registered = False

    with _setup_reg_config("Mooncake", pool_endpoint="tcp://kvp-master:5557",
                           xpu_endpoint="tcp://*:50090"):
        ConductorApiClient.register_kv_instance([instance])

    calls = mock_client.post.call_args_list
    assert len(calls) == 2  # pool + HBM

    # First call: pool
    pool_payload = calls[0][0][1]
    assert "endpoint" in pool_payload
    assert pool_payload["endpoint"] == "tcp://kvp-master:5557"
    assert pool_payload["store_backend"] == "Mooncake"

    # Second call: HBM DP
    hbm_payload = calls[1][0][1]
    assert "medium_endpoints" in hbm_payload
    assert "xpu" in str(hbm_payload["medium_endpoints"])


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_mooncake_pool_only_registered_once(mock_http):
    """Pool is registered only once across multiple register_kv_instance calls."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)
    ConductorApiClient._pool_registered = False

    with _setup_reg_config("Mooncake", pool_endpoint="tcp://kvp-master:5557",
                           xpu_endpoint="tcp://*:50090"):
        ConductorApiClient.register_kv_instance([instance])
        ConductorApiClient.register_kv_instance([instance])

    calls = mock_client.post.call_args_list
    # First call: pool + HBM (2). Second call: only HBM (1). Total = 3
    assert len(calls) == 3
    pool_calls = [c for c in calls if "endpoint" in c[0][1] and "pool" in c[0][1].get("instance_id", "")]
    assert len(pool_calls) == 1


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_memcache_registration_same_as_mooncake_different_store_backend(mock_http):
    """Memcache uses pool mode like Mooncake."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)
    ConductorApiClient._pool_registered = False

    with _setup_reg_config("Memcache", pool_endpoint="tcp://kvp-master:5557",
                           xpu_endpoint="tcp://*:50090"):
        ConductorApiClient.register_kv_instance([instance])

    calls = mock_client.post.call_args_list
    assert len(calls) == 2
    assert calls[0][0][1]["store_backend"] == "Memcache"
    assert calls[1][0][1]["store_backend"] == "Memcache"


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_replay_endpoint_included_in_registration(mock_http):
    """replay_endpoint is resolved and included in registration payloads."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)
    ConductorApiClient._pool_registered = False

    with _setup_reg_config("YuanRong", xpu_endpoint="tcp://*:15557",
                           cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558",
                           replay_endpoint="tcp://*:6667"):
        ConductorApiClient.register_kv_instance([instance])

    payload = mock_client.post.call_args_list[0][0][1]
    assert "replay_endpoint" in payload
    assert "6667" in payload["replay_endpoint"]


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_endpoint_url_resolves_ip_and_dp_rank(mock_http):
    """Pattern 'tcp://*:15557' + IP 10.0.0.1 + dp_rank 2 → 'tcp://10.0.0.1:15559'."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)
    instance.endpoints["pod-0"][0].ip = "10.0.0.1"
    instance.endpoints["pod-0"][0].id = 2  # dp_rank=2

    with _setup_reg_config("YuanRong", xpu_endpoint="tcp://*:15557",
                           cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"):
        ConductorApiClient.register_kv_instance([instance])

    payload = mock_client.post.call_args_list[0][0][1]
    meps = payload["medium_endpoints"]
    assert meps["xpu"] == "tcp://10.0.0.1:15559"  # 15557 + 2
    assert meps["cpu"] == "tcp://10.0.0.1:15560"   # 15558 + 2
    assert payload["dp_rank"] == 2


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_unknown_backend_falls_back_to_per_dp(mock_http):
    """Unknown backend → per_dp mode (treats as YuanRong)."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)

    with _setup_reg_config("SomeUnknownBackend", xpu_endpoint="tcp://*:15557"):
        ConductorApiClient.register_kv_instance([instance])

    assert len(mock_client.post.call_args_list) >= 1
    payload = mock_client.post.call_args_list[0][0][1]
    assert "medium_endpoints" in payload  # YuanRong-style
    assert "pool" not in payload.get("instance_id", "")  # NOT pool registration
