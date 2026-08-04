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


from motor.common.resources.instance import Instance, Endpoint, PDRole
from motor.coordinator.api_client.conductor_api_client import (
    TENANT_ID,
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
    and legacy prefill_kv_event_config.
    """
    from motor.config.coordinator import KvConductorConfig, SchedulerConfig

    reg = KvConductorConfig(
        store_backend=overrides.get("store_backend", "Mooncake"),
        npu_endpoint=overrides.get("npu_endpoint", "tcp://*:5557"),
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
        cfg = _mock_config(npu_endpoint="", endpoint="", replay_endpoint="")
        inst = _make_instance(inst_id=1, role=PDRole.ROLE_P)
        ep = _make_endpoint(ep_id=0, ip="10.0.0.1")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        assert payload == {}

    def test_basic_payload_with_npu_endpoint(self):
        """Standard payload with medium_endpoints via npu_endpoint (no fallback)."""
        cfg = _mock_config(npu_endpoint="tcp://*:5557", endpoint="")
        inst = _make_instance(inst_id=1, role=PDRole.ROLE_P, model_name="qwen")
        ep = _make_endpoint(ep_id=0, ip="10.0.0.1")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        assert payload["instance_id"] == "vllm-prefill-1"
        assert payload["dp_rank"] == 0
        assert payload["medium_endpoints"] == {"npu": "tcp://10.0.0.1:5557"}
        assert payload["type"] == "vLLM"
        assert payload["modelname"] == "qwen"
        assert payload["block_size"] == 128

    def test_payload_with_replay_endpoint(self):
        """Payload includes replay_endpoint when configured."""
        cfg = _mock_config(
            npu_endpoint="tcp://*:5557",
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
        cfg = _mock_config(npu_endpoint="tcp://*:5557")
        inst = _make_instance(inst_id=3, role=PDRole.ROLE_P)
        ep = _make_endpoint(ep_id=5, ip="10.0.0.3")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        assert payload["dp_rank"] == 5
        assert payload["medium_endpoints"]["npu"] == "tcp://10.0.0.3:5562"

    def test_payload_with_fallback_endpoint(self):
        """Legacy 'endpoint' fallback pattern used when npu_endpoint empty."""
        cfg = _mock_config(npu_endpoint="", endpoint="tcp://*:15557")
        inst = _make_instance(inst_id=4, role=PDRole.ROLE_P)
        ep = _make_endpoint(ep_id=0, ip="10.0.0.4")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        # Fallback endpoint fills gpu, cpu, disk
        meps = payload["medium_endpoints"]
        assert "npu" in meps

    def test_replay_endpoint_malformed_skipped(self):
        """replay_endpoint without '*:' → replay_endpoint absent in payload."""
        cfg = _mock_config(
            npu_endpoint="tcp://*:5557",
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
                    "medium_endpoints": {"npu": "tcp://10.0.0.1:5557"},
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
                "0": {"medium_endpoints": {"npu": "tcp://10.0.0.1:5557"}},
                "1": {"medium_endpoints": {"npu": "tcp://10.0.0.1:5558"}},
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
                "abc": {"medium_endpoints": {"npu": "tcp://x:1"}},
                "0": {"medium_endpoints": {"npu": "tcp://x:2"}},
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
                {"workers": []},  # /workers (empty → fallback)
                mooncake_response,  # /services
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
                mooncake_response,  # /services
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
        cfg = _mock_config(npu_endpoint="", endpoint="")

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

        cfg = _mock_config(npu_endpoint="tcp://*:5557")

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

        cfg = _mock_config(npu_endpoint="tcp://*:5557")

        # Same (instance_id, dp_rank) already registered
        registered = [
            {"instance_id": "vllm-prefill-1", "endpoints": {"0": {"medium_endpoints": {"npu": "tcp://10.0.0.1:5557"}}}}
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

        cfg = _mock_config(npu_endpoint="tcp://*:5557")

        # ep0 (dp_rank=0) already registered; ep1 (dp_rank=1) missing
        registered = [
            {"instance_id": "vllm-prefill-1", "endpoints": {"0": {"medium_endpoints": {"npu": "tcp://10.0.0.1:5557"}}}}
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

        cfg = _mock_config(npu_endpoint="tcp://*:5557")

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

        cfg = _mock_config(npu_endpoint="tcp://*:5557")

        # Different instance registered
        registered = [{"InstanceID": "vllm-prefill-99", "DPRank": 0, "Endpoint": "tcp://10.0.0.99:5557"}]

        with (
            patch.object(ConductorApiClient, "coordinator_config", cfg),
            patch.object(ConductorApiClient, "get_registered_services", return_value=registered),
            patch.object(ConductorApiClient, "register_post") as mock_register,
        ):
            ConductorApiClient.re_register_kv_instances([inst])

        mock_register.assert_called_once_with(inst, ep)


# ── Registration dispatch tests ──────────────────────────────────────


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
    mock_http.return_value.__enter__.return_value = mock_client


def _mock_failed_query(mock_http):
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


def _setup_reg_config(
    store_backend, pool_endpoint="", npu_endpoint="", cpu_endpoint="", disk_endpoint="", replay_endpoint=""
):
    """Patch ConductorApiClient's config for registration testing."""
    from motor.config.coordinator import KvConductorConfig, SchedulerConfig

    reg = KvConductorConfig(
        store_backend=store_backend,
        pool_endpoint=pool_endpoint,
        npu_endpoint=npu_endpoint,
        cpu_endpoint=cpu_endpoint,
        disk_endpoint=disk_endpoint,
        replay_endpoint=replay_endpoint,
    )
    sched = SchedulerConfig(kv_conductor_config=reg)
    return patch.object(
        ConductorApiClient,
        "coordinator_config",
        scheduler_config=sched,
        prefill_kv_event_config=Mock(
            engine_type="vLLM",
            block_size=128,
            conductor_service="kv-conductor",
            http_server_port=13333,
            model_path="",
            replay_endpoint="",
            endpoint="",
        ),
    )


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_yuanrong_registration_dispatches_per_dp(mock_http):
    """YuanRong: per-DP multi-port, not pool."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)
    ConductorApiClient._pool_registered = False

    with _setup_reg_config(
        "YuanRong", npu_endpoint="tcp://*:15557", cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"
    ):
        ConductorApiClient.register_kv_instance([instance])

    calls = mock_client.post.call_args_list
    assert len(calls) == 1  # one DP = one call
    payload = calls[0][0][1]
    assert "medium_endpoints" in payload
    assert payload["store_backend"] == "YuanRong"
    assert "npu" in str(payload["medium_endpoints"])
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

    with _setup_reg_config("Mooncake", pool_endpoint="tcp://kvp-master:5557", npu_endpoint="tcp://*:50090"):
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
    assert "npu" in str(hbm_payload["medium_endpoints"])


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_mooncake_pool_only_registered_once(mock_http):
    """Pool is registered only once across multiple register_kv_instance calls."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)
    ConductorApiClient._pool_registered = False

    with _setup_reg_config("Mooncake", pool_endpoint="tcp://kvp-master:5557", npu_endpoint="tcp://*:50090"):
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

    with _setup_reg_config("Memcache", pool_endpoint="tcp://kvp-master:5557", npu_endpoint="tcp://*:50090"):
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

    with _setup_reg_config(
        "YuanRong",
        npu_endpoint="tcp://*:15557",
        cpu_endpoint="tcp://*:15558",
        disk_endpoint="tcp://*:15558",
        replay_endpoint="tcp://*:6667",
    ):
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

    with _setup_reg_config(
        "YuanRong", npu_endpoint="tcp://*:15557", cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"
    ):
        ConductorApiClient.register_kv_instance([instance])

    payload = mock_client.post.call_args_list[0][0][1]
    meps = payload["medium_endpoints"]
    assert meps["npu"] == "tcp://10.0.0.1:15559"  # 15557 + 2
    assert meps["cpu"] == "tcp://10.0.0.1:15560"  # 15558 + 2
    assert payload["dp_rank"] == 2


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_unknown_backend_falls_back_to_per_dp(mock_http):
    """Unknown backend → per_dp mode (treats as YuanRong)."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)

    with _setup_reg_config("SomeUnknownBackend", npu_endpoint="tcp://*:15557"):
        ConductorApiClient.register_kv_instance([instance])

    assert len(mock_client.post.call_args_list) >= 1
    payload = mock_client.post.call_args_list[0][0][1]
    assert "medium_endpoints" in payload  # YuanRong-style
    assert "pool" not in payload.get("instance_id", "")  # NOT pool registration
