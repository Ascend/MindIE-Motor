# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for ConductorApiClient — re-register flow."""

from unittest.mock import Mock, patch

import pytest

from motor.common.resources.instance import Instance, Endpoint, PDRole
from motor.coordinator.api_client.conductor_api_client import (
    ConductorApiClient,
    conductor_instance_id,
)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_endpoint(
    ep_id: int = 0,
    ip: str = "127.0.0.1",
    business_port: str = "8000",
    mgmt_port: str = "8001",
) -> Endpoint:
    return Endpoint(
        id=ep_id,
        ip=ip,
        business_port=business_port,
        mgmt_port=mgmt_port,
    )


def _make_instance(
    inst_id: int = 1,
    role: PDRole = PDRole.ROLE_P,
    model_name: str = "test-model",
    job_name: str = "test-job",
    endpoints: dict | None = None,
) -> Instance:
    if endpoints is None:
        ep = _make_endpoint(ep_id=0)
        endpoints = {"pod-0": {0: ep}}
    return Instance(
        id=inst_id,
        role=role,
        model_name=model_name,
        job_name=job_name,
        endpoints=endpoints,
    )


def _mock_config(**overrides) -> Mock:
    """Build a mocked coordinator_config with both new kv_conductor_config
    and legacy prefill_kv_event_config."""
    from motor.config.coordinator import KvConductorConfig, SchedulerConfig

    reg = KvConductorConfig(
        store_backend=overrides.get("store_backend", "Mooncake"),
        xpu_endpoint=overrides.get("xpu_endpoint", "tcp://*:5557"),
        endpoint=overrides.get("endpoint", "tcp://*:5557"),
        replay_endpoint=overrides.get("replay_endpoint", ""),
        engine_type=overrides.get("engine_type", "vLLM"),
        block_size=overrides.get("block_size", 128),
        conductor_service=overrides.get("conductor_service", "kv-conductor"),
        http_server_port=overrides.get("http_server_port", 13333),
    )
    sched = SchedulerConfig(kv_conductor_config=reg)
    cfg = Mock()
    cfg.scheduler_config = sched
    # Legacy config for backward compatibility
    legacy = Mock()
    legacy.endpoint = overrides.get("endpoint", "tcp://*:5557")
    legacy.replay_endpoint = overrides.get("replay_endpoint", "")
    legacy.engine_type = overrides.get("engine_type", "vLLM")
    legacy.block_size = overrides.get("block_size", 128)
    legacy.conductor_service = overrides.get("conductor_service", "kv-conductor")
    legacy.http_server_port = overrides.get("http_server_port", 13333)
    cfg.prefill_kv_event_config = legacy
    return cfg


# ------------------------------------------------------------------
# conductor_instance_id
# ------------------------------------------------------------------


class TestConductorInstanceId:
    def test_role_u_returns_union_prefix(self):
        inst = _make_instance(inst_id=7, role=PDRole.ROLE_U)
        assert conductor_instance_id(inst) == "vllm-union-7"

    def test_role_p_returns_prefill_prefix(self):
        inst = _make_instance(inst_id=3, role=PDRole.ROLE_P)
        assert conductor_instance_id(inst) == "vllm-prefill-3"

    def test_role_e_falls_to_prefill_prefix(self):
        inst = _make_instance(inst_id=5, role=PDRole.ROLE_E)
        assert conductor_instance_id(inst) == "vllm-prefill-5"

    def test_role_d_falls_to_prefill_prefix(self):
        inst = _make_instance(inst_id=9, role=PDRole.ROLE_D)
        assert conductor_instance_id(inst) == "vllm-prefill-9"


# ------------------------------------------------------------------
# _build_register_payload
# ------------------------------------------------------------------


class TestBuildRegisterPayload:
    """Cover branches of _build_register_payload (uses kv_conductor_config)."""

    def test_returns_empty_dict_when_no_endpoints_configured(self):
        """No endpoint patterns configured → empty dict."""
        cfg = _mock_config(xpu_endpoint="", endpoint="", replay_endpoint="")
        inst = _make_instance(inst_id=1, role=PDRole.ROLE_P)
        ep = _make_endpoint(ep_id=0, ip="10.0.0.1")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        assert payload == {}

    def test_basic_payload_with_xpu_endpoint(self):
        """Standard payload with medium_endpoints via xpu_endpoint (no fallback)."""
        cfg = _mock_config(xpu_endpoint="tcp://*:5557", endpoint="")
        inst = _make_instance(inst_id=1, role=PDRole.ROLE_P, model_name="qwen")
        ep = _make_endpoint(ep_id=0, ip="10.0.0.1")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        assert payload["instance_id"] == "vllm-prefill-1"
        assert payload["dp_rank"] == 0
        assert payload["medium_endpoints"] == {"xpu": "tcp://10.0.0.1:5557"}
        assert payload["type"] == "vLLM"
        assert payload["modelname"] == "qwen"
        assert payload["block_size"] == 128

    def test_payload_with_replay_endpoint(self):
        """Payload includes replay_endpoint when configured."""
        cfg = _mock_config(
            xpu_endpoint="tcp://*:5557",
            replay_endpoint="tcp://*:6667",
        )
        inst = _make_instance(inst_id=2, role=PDRole.ROLE_U, model_name="qwen")
        ep = _make_endpoint(ep_id=1, ip="10.0.0.2")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        assert payload["replay_endpoint"] == "tcp://10.0.0.2:6668"
        assert payload["instance_id"] == "vllm-union-2"
        assert payload["dp_rank"] == 1

    def test_payload_dp_rank_uses_endpoint_id(self):
        """dp_rank is taken from endpoint.id."""
        cfg = _mock_config(xpu_endpoint="tcp://*:5557")
        inst = _make_instance(inst_id=3, role=PDRole.ROLE_P)
        ep = _make_endpoint(ep_id=5, ip="10.0.0.3")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        assert payload["dp_rank"] == 5
        assert payload["medium_endpoints"]["xpu"] == "tcp://10.0.0.3:5562"

    def test_payload_with_fallback_endpoint(self):
        """Legacy 'endpoint' fallback pattern used when xpu_endpoint empty."""
        cfg = _mock_config(xpu_endpoint="", endpoint="tcp://*:15557")
        inst = _make_instance(inst_id=4, role=PDRole.ROLE_P)
        ep = _make_endpoint(ep_id=0, ip="10.0.0.4")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        # Fallback endpoint fills xpu, cpu, disk
        meps = payload["medium_endpoints"]
        assert "xpu" in meps

    def test_replay_endpoint_malformed_skipped(self):
        """replay_endpoint without '*:' → replay_endpoint absent in payload."""
        cfg = _mock_config(
            xpu_endpoint="tcp://*:5557",
            replay_endpoint="tcp://127.0.0.1:6667",
        )
        inst = _make_instance(inst_id=5, role=PDRole.ROLE_P)
        ep = _make_endpoint(ep_id=0, ip="10.0.0.5")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        assert "replay_endpoint" not in payload


# ------------------------------------------------------------------
# _normalize_service_key
# ------------------------------------------------------------------


class TestNormalizeServiceKey:
    """Cover field extraction from both kv-conductor and Mooncake Master formats."""

    # ── kv-conductor format (WorkerSummary) ──────────────────────────

    def test_kv_conductor_single_dp(self):
        """WorkerSummary with one DP extracts correctly."""
        worker = {
            "instance_id": "vllm-prefill-1",
            "endpoints": {
                "0": {
                    "medium_endpoints": {"xpu": "tcp://10.0.0.1:5557"},
                    "dp_rank": 0,
                }
            },
        }
        keys = ConductorApiClient._normalize_service_key(worker)
        assert keys == {("vllm-prefill-1", 0)}

    def test_kv_conductor_multiple_dps(self):
        """WorkerSummary with multiple DPs extracts all."""
        worker = {
            "instance_id": "vllm-union-2",
            "endpoints": {
                "0": {"medium_endpoints": {"xpu": "tcp://10.0.0.1:5557"}},
                "1": {"medium_endpoints": {"xpu": "tcp://10.0.0.1:5558"}},
            },
        }
        keys = ConductorApiClient._normalize_service_key(worker)
        assert keys == {("vllm-union-2", 0), ("vllm-union-2", 1)}

    def test_kv_conductor_empty_endpoints(self):
        """No endpoints → empty set."""
        worker = {"instance_id": "vllm-prefill-1", "endpoints": {}}
        keys = ConductorApiClient._normalize_service_key(worker)
        assert keys == set()

    def test_kv_conductor_non_numeric_dp_rank_skipped(self):
        """Non-numeric dp_rank string → skipped."""
        worker = {
            "instance_id": "vllm-prefill-1",
            "endpoints": {
                "abc": {"medium_endpoints": {"xpu": "tcp://x:1"}},
                "0": {"medium_endpoints": {"xpu": "tcp://x:2"}},
            },
        }
        keys = ConductorApiClient._normalize_service_key(worker)
        assert keys == {("vllm-prefill-1", 0)}

    # ── Mooncake Master format (flat fields) ─────────────────────────

    def test_mooncake_master_basic(self):
        """Mooncake Master: InstanceID + DPRank."""
        service = {
            "InstanceID": "vllm-prefill-1",
            "DPRank": 0,
            "Endpoint": "tcp://10.0.0.1:5557",
            "ReplayEndpoint": "tcp://10.0.0.1:6667",
        }
        keys = ConductorApiClient._normalize_service_key(service)
        assert keys == {("vllm-prefill-1", 0)}

    def test_mooncake_master_dp_rank_zero(self):
        """dp_rank=0 must NOT be treated as falsy."""
        service = {"InstanceID": "vllm-prefill-1", "DPRank": 0}
        keys = ConductorApiClient._normalize_service_key(service)
        assert keys == {("vllm-prefill-1", 0)}

    def test_mooncake_master_dp_rank_missing_defaults_to_minus_one(self):
        """No DPRank key → defaults to -1."""
        service = {"InstanceID": "vllm-prefill-1"}
        keys = ConductorApiClient._normalize_service_key(service)
        assert keys == {("vllm-prefill-1", -1)}

    def test_mooncake_master_dp_rank_non_numeric(self):
        """DPRank is a non-numeric string → -1."""
        service = {"InstanceID": "vllm-prefill-1", "DPRank": "abc"}
        keys = ConductorApiClient._normalize_service_key(service)
        assert keys == {("vllm-prefill-1", -1)}

    def test_mooncake_master_instance_id_empty(self):
        """Missing InstanceID → empty set."""
        service = {"DPRank": 0}
        keys = ConductorApiClient._normalize_service_key(service)
        assert keys == set()


# ------------------------------------------------------------------
# get_registered_services
# ------------------------------------------------------------------


class TestGetRegisteredServices:
    """Cover both kv-conductor /workers and Mooncake Master /services fallback."""

    def test_returns_workers_list(self):
        """kv-conductor: GET /workers returns workers list."""
        cfg = _mock_config()
        response = {"workers": [{"instance_id": "vllm-prefill-1", "endpoints": {"0": {}}}]}

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient") as mock_http,
        ):
            mock_http.return_value.__enter__.return_value.get.return_value = response
            services = ConductorApiClient.get_registered_services()

        assert services == [{"instance_id": "vllm-prefill-1", "endpoints": {"0": {}}}]

    def test_falls_back_to_services_when_workers_empty(self):
        """When /workers returns no workers, fall back to /services (Mooncake Master)."""
        cfg = _mock_config()
        mooncake_response = {"services": [{"InstanceID": "vllm-prefill-1", "DPRank": 0}]}

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient") as mock_http,
        ):
            # /workers returns empty list → fallback to /services
            mock_http.return_value.__enter__.return_value.get.side_effect = [
                {"workers": []},           # /workers (empty → fallback)
                mooncake_response,          # /services
            ]
            services = ConductorApiClient.get_registered_services()

        assert services == [{"InstanceID": "vllm-prefill-1", "DPRank": 0}]

    def test_falls_back_to_services_when_workers_fails(self):
        """When /workers raises, fall back to /services."""
        cfg = _mock_config()
        mooncake_response = {"services": [{"InstanceID": "vllm-prefill-2", "DPRank": 1}]}

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient") as mock_http,
        ):
            # /workers raises ConnectionError → fallback to /services
            mock_http.return_value.__enter__.return_value.get.side_effect = [
                ConnectionError("conn refused"),  # /workers
                mooncake_response,                # /services
            ]
            services = ConductorApiClient.get_registered_services()

        assert services == [{"InstanceID": "vllm-prefill-2", "DPRank": 1}]

    def test_returns_empty_when_both_fail(self):
        """When both /workers and /services raise, return empty."""
        cfg = _mock_config()

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient") as mock_http,
        ):
            mock_http.return_value.__enter__.return_value.get.side_effect = ConnectionError("conn refused")
            services = ConductorApiClient.get_registered_services()

        assert services == []

    def test_returns_empty_when_response_not_dict(self):
        cfg = _mock_config()

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient") as mock_http,
        ):
            mock_http.return_value.__enter__.return_value.get.return_value = "not-a-dict"
            services = ConductorApiClient.get_registered_services()

        assert services == []


# ------------------------------------------------------------------
# re_register_kv_instances
# ------------------------------------------------------------------


class TestReRegisterKvInstances:
    """Cover the core re-register logic."""

    def test_skip_when_no_registered_services(self):
        """get_registered_services raises → info log and return."""
        inst = _make_instance(inst_id=1, role=PDRole.ROLE_P)
        cfg = _mock_config()

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch.object(ConductorApiClient, "get_registered_services", side_effect=RuntimeError("no conductor")),
            patch.object(ConductorApiClient, "register_post") as mock_register,
        ):
            ConductorApiClient.re_register_kv_instances([inst])

        mock_register.assert_not_called()

    def test_skip_non_kva_roles(self):
        """ROLE_D and ROLE_E are not in _KVA_ROLES → skipped."""
        inst_d = _make_instance(inst_id=1, role=PDRole.ROLE_D)
        inst_e = _make_instance(inst_id=2, role=PDRole.ROLE_E)
        cfg = _mock_config()

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch.object(ConductorApiClient, "get_registered_services", return_value=[]),
            patch.object(ConductorApiClient, "register_post") as mock_register,
        ):
            ConductorApiClient.re_register_kv_instances([inst_d, inst_e])

        mock_register.assert_not_called()

    def test_skip_when_no_endpoints_configured(self):
        """No endpoint patterns → _build_register_payload returns {} → skip."""
        inst = _make_instance(inst_id=1, role=PDRole.ROLE_P)
        cfg = _mock_config(xpu_endpoint="", endpoint="")

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch.object(ConductorApiClient, "get_registered_services", return_value=[]),
            patch.object(ConductorApiClient, "register_post") as mock_register,
        ):
            ConductorApiClient.re_register_kv_instances([inst])

        mock_register.assert_not_called()

    def test_re_registers_when_service_missing(self):
        """Instance in local but not in Conductor → register_post called."""
        ep = _make_endpoint(ep_id=0, ip="10.0.0.1")
        endpoints = {"pod-0": {0: ep}}
        inst = Instance(id=1, role=PDRole.ROLE_P, model_name="qwen", job_name="test-job", endpoints=endpoints)

        cfg = _mock_config(xpu_endpoint="tcp://*:5557")

        # Conductor has a DIFFERENT instance registered
        registered = [{"instance_id": "vllm-prefill-99", "endpoints": {"0": {}}}]

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch.object(ConductorApiClient, "get_registered_services", return_value=registered),
            patch.object(ConductorApiClient, "register_post") as mock_register,
        ):
            ConductorApiClient.re_register_kv_instances([inst])

        mock_register.assert_called_once_with(inst, ep)

    def test_skips_when_already_registered(self):
        """Instance already in Conductor → register_post NOT called."""
        ep = _make_endpoint(ep_id=0, ip="10.0.0.1")
        endpoints = {"pod-0": {0: ep}}
        inst = Instance(id=1, role=PDRole.ROLE_P, model_name="qwen", job_name="test-job", endpoints=endpoints)

        cfg = _mock_config(xpu_endpoint="tcp://*:5557")

        # Same (instance_id, dp_rank) already registered
        registered = [
            {"instance_id": "vllm-prefill-1", "endpoints": {"0": {"medium_endpoints": {"xpu": "tcp://10.0.0.1:5557"}}}}
        ]

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch.object(ConductorApiClient, "get_registered_services", return_value=registered),
            patch.object(ConductorApiClient, "register_post") as mock_register,
        ):
            ConductorApiClient.re_register_kv_instances([inst])

        mock_register.assert_not_called()

    def test_re_registers_only_missing_among_multiple(self):
        """Multiple endpoints: only the missing one is re-registered."""
        ep0 = _make_endpoint(ep_id=0, ip="10.0.0.1")
        ep1 = _make_endpoint(ep_id=1, ip="10.0.0.1")
        endpoints = {"pod-0": {0: ep0, 1: ep1}}
        inst = Instance(id=1, role=PDRole.ROLE_P, model_name="qwen", job_name="test-job", endpoints=endpoints)

        cfg = _mock_config(xpu_endpoint="tcp://*:5557")

        # ep0 (dp_rank=0) already registered; ep1 (dp_rank=1) missing
        registered = [
            {"instance_id": "vllm-prefill-1", "endpoints": {"0": {"medium_endpoints": {"xpu": "tcp://10.0.0.1:5557"}}}}
        ]

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch.object(ConductorApiClient, "get_registered_services", return_value=registered),
            patch.object(ConductorApiClient, "register_post") as mock_register,
        ):
            ConductorApiClient.re_register_kv_instances([inst])

        # Only ep1 (dp_rank=1) should trigger re-register
        assert mock_register.call_count == 1
        called_ep = mock_register.call_args[0][1]
        assert called_ep.id == 1

    def test_skips_when_already_registered_mooncake_format(self):
        """Instance already registered (Mooncake Master format) → skip."""
        ep = _make_endpoint(ep_id=0, ip="10.0.0.1")
        endpoints = {"pod-0": {0: ep}}
        inst = Instance(id=1, role=PDRole.ROLE_P, model_name="qwen", job_name="test-job", endpoints=endpoints)

        cfg = _mock_config(xpu_endpoint="tcp://*:5557")

        # Mooncake Master format: InstanceID + DPRank
        registered = [{"InstanceID": "vllm-prefill-1", "DPRank": 0, "Endpoint": "tcp://10.0.0.1:5557"}]

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch.object(ConductorApiClient, "get_registered_services", return_value=registered),
            patch.object(ConductorApiClient, "register_post") as mock_register,
        ):
            ConductorApiClient.re_register_kv_instances([inst])

        mock_register.assert_not_called()

    def test_re_registers_missing_mooncake_format(self):
        """Instance missing (Mooncake Master format) → register_post called."""
        ep = _make_endpoint(ep_id=0, ip="10.0.0.1")
        endpoints = {"pod-0": {0: ep}}
        inst = Instance(id=1, role=PDRole.ROLE_P, model_name="qwen", job_name="test-job", endpoints=endpoints)

        cfg = _mock_config(xpu_endpoint="tcp://*:5557")

        # Different instance registered
        registered = [{"InstanceID": "vllm-prefill-99", "DPRank": 0, "Endpoint": "tcp://10.0.0.99:5557"}]

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch.object(ConductorApiClient, "get_registered_services", return_value=registered),
            patch.object(ConductorApiClient, "register_post") as mock_register,
        ):
            ConductorApiClient.re_register_kv_instances([inst])

        mock_register.assert_called_once_with(inst, ep)
