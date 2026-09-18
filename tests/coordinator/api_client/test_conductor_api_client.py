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

import msgspec

from motor.common.resources.instance import Instance, Endpoint, PDRole
from motor.coordinator.api_client.conductor_api_client import (
    MSGPACK_CONTENT_TYPE,
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
) -> Endpoint:
    return Endpoint(
        id=ep_id,
        ip=ip,
        business_port=business_port,
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
        cpu_endpoint=overrides.get("cpu_endpoint", ""),
        disk_endpoint=overrides.get("disk_endpoint", ""),
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

    def test_yuanrong_payload_is_npu_only(self):
        """YuanRong DP payload omits CPU/Disk (those are registered per node)."""
        cfg = _mock_config(
            store_backend="YuanRong",
            npu_endpoint="tcp://*:15557",
            cpu_endpoint="tcp://*:15558",
            disk_endpoint="tcp://*:15558",
            endpoint="",
        )
        inst = _make_instance(inst_id=4, role=PDRole.ROLE_P)
        ep = _make_endpoint(ep_id=0, ip="10.0.0.1")

        with patch.object(ConductorApiClient, "coordinator_config", cfg):
            payload = ConductorApiClient._build_register_payload(inst, ep)

        assert payload["medium_endpoints"] == {"npu": "tcp://10.0.0.1:15557"}

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


def _mock_successful_query(
    mock_http,
    response=None,
    *,
    encoding="msgpack",
    response_content_type=None,
):
    """Mock a successful /query round trip.

    ``encoding`` selects which client-side path the mock wires up
    (``post_bytes`` for msgpack, ``do_post`` for JSON). The fake response
    carries the given ``Content-Type`` (defaults to the request encoding) so
    the client-side response parsing is exercised end to end.
    """
    if response is None:
        response = {TENANT_ID: {}}
    if response_content_type is None:
        response_content_type = MSGPACK_CONTENT_TYPE if encoding == "msgpack" else "application/json"

    fake_resp = Mock()
    fake_resp.headers.get.side_effect = lambda key, default=None: (
        response_content_type if key.lower() == "content-type" else default
    )
    fake_resp.content = (
        msgspec.msgpack.encode(response) if response_content_type.startswith(MSGPACK_CONTENT_TYPE) else b""
    )
    fake_resp.json.return_value = response

    mock_client = Mock()
    mock_client.post_bytes.return_value = fake_resp
    mock_client.do_post.return_value = fake_resp
    mock_http.return_value.__enter__.return_value = mock_client


def _mock_failed_query(mock_http):
    mock_http.return_value.__enter__.side_effect = ConnectionError("connection refused")


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_return_value_on_success(mock_http):
    """On success (msgpack default), query_conductor returns the response dict."""
    expected = {TENANT_ID: {"vllm-prefill-1": {"longest_matched": 100, "DP": {"0": 50}}}}
    _mock_successful_query(mock_http, response=expected)
    instances = [_make_mock_instance(1)]

    result = ConductorApiClient.query_conductor(instances, [1, 2, 3])
    assert result == expected


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_query_msgpack_wire_format(mock_http):
    """The msgpack path sends a MessagePack body with the right Content-Type
    and decodes the MessagePack response.
    """
    expected = {TENANT_ID: {"vllm-prefill-1": {"longest_matched": 384, "DP": {"0": 3}}}}
    _mock_successful_query(mock_http, response=expected)
    instances = [_make_mock_instance(1)]
    token_ids = list(range(1000))

    result = ConductorApiClient.query_conductor(instances, token_ids)
    assert result == expected

    mock_client = mock_http.return_value.__enter__.return_value
    body = mock_client.post_bytes.call_args[0][1]
    content_type = mock_client.post_bytes.call_args[1]["content_type"]
    assert content_type == MSGPACK_CONTENT_TYPE
    # The request body must decode back to the exact query data.
    decoded = msgspec.msgpack.decode(body)
    assert decoded["model"] == "test-model"
    assert decoded["block_size"] == 128
    assert decoded["token_ids"] == token_ids
    # tenant_id is omitted on the wire when it equals the default.
    assert decoded.get("tenant_id", TENANT_ID) == TENANT_ID


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_query_json_encoding_config(mock_http):
    """query_encoding='json' keeps the legacy JSON wire path."""
    expected = {TENANT_ID: {"vllm-prefill-1": {"longest_matched": 100}}}
    _mock_successful_query(mock_http, response=expected, encoding="json")
    instances = [_make_mock_instance(1)]

    with _setup_reg_config("Mooncake", query_encoding="json"):
        result = ConductorApiClient.query_conductor(instances, [1, 2, 3])
    assert result == expected

    mock_client = mock_http.return_value.__enter__.return_value
    assert mock_client.post_bytes.call_count == 0
    json_data = mock_client.do_post.call_args[1]["data"]
    assert json_data["token_ids"] == [1, 2, 3]


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_query_legacy_json_response_fallback(mock_http):
    """A msgpack request answered by a legacy JSON-only conductor still
    parses correctly (response Content-Type fallback).
    """
    expected = {TENANT_ID: {"vllm-prefill-1": {"longest_matched": 100}}}
    _mock_successful_query(
        mock_http,
        response=expected,
        encoding="msgpack",
        response_content_type="application/json",
    )
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
    store_backend,
    pool_endpoint="",
    npu_endpoint="",
    cpu_endpoint="",
    disk_endpoint="",
    replay_endpoint="",
    query_encoding="msgpack",
):
    """Patch ConductorApiClient's config for registration testing."""
    from motor.config.coordinator import KvConductorConfig, SchedulerConfig

    reg = KvConductorConfig(
        conductor_service="kv-conductor",
        store_backend=store_backend,
        pool_endpoint=pool_endpoint,
        npu_endpoint=npu_endpoint,
        cpu_endpoint=cpu_endpoint,
        disk_endpoint=disk_endpoint,
        replay_endpoint=replay_endpoint,
        query_encoding=query_encoding,
    )
    sched = SchedulerConfig(kv_conductor_config=reg)
    ConductorApiClient._pool_registered = False
    ConductorApiClient._yuanrong_nodes_registered = set()
    ConductorApiClient._yuanrong_node_refs = {}
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
def test_yuanrong_registration_npu_per_dp_cpu_disk_per_node(mock_http):
    """YuanRong: NPU per DP; CPU/Disk once per node (no dp_rank port offset)."""
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
    assert len(calls) == 2  # NPU DP + node CPU/Disk
    payloads = [c[0][1] for c in calls]
    npu_payload = next(p for p in payloads if "npu" in p.get("medium_endpoints", {}))
    pool_payload = next(p for p in payloads if "pool" in p.get("instance_id", ""))

    assert npu_payload["store_backend"] == "YuanRong"
    assert npu_payload["medium_endpoints"] == {"npu": "tcp://127.0.0.1:15557"}
    assert "cpu" not in npu_payload["medium_endpoints"]
    # The instance_id carries the model too: kv-conductor keys registrations by
    # (instance_id, dp_rank) and tears down the whole key on re-registration, so
    # an ip-only id would let a second model evict the first model's pool.
    assert pool_payload["instance_id"] == "yuanrong-pool-127.0.0.1-test-model"
    assert pool_payload["medium_endpoints"]["cpu"] == "tcp://127.0.0.1:15558"
    assert pool_payload["medium_endpoints"]["disk"] == "tcp://127.0.0.1:15558"
    assert pool_payload["dp_rank"] == 0


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
def test_memcache_store_backend_matches_case_insensitively(mock_http):
    """Lowercase / odd-cased store_backend still resolves to pool mode.

    kv-conductor's StoreBackend::parse is case-insensitive; the Python
    client must accept the same spellings (e.g. "memcache") instead of
    silently falling back to per_node mode.
    """
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    for store_backend in ("memcache", "MEMCACHE", "mEmCaChE"):
        instance = _make_mock_instance(1)
        ConductorApiClient._pool_registered = False
        with _setup_reg_config(store_backend, pool_endpoint="tcp://kvp-master:5557", npu_endpoint="tcp://*:50090"):
            ConductorApiClient.register_kv_instance([instance])

        calls = mock_client.post.call_args_list
        # pool + HBM DP; pool payload normalized to canonical "Memcache"
        assert len(calls) == 2, f"store_backend={store_backend} must register pool + HBM"
        assert calls[0][0][1]["store_backend"] == "Memcache"
        assert calls[0][0][1]["instance_id"] == "memcache-pool"
        mock_client.post.reset_mock()


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_pool_endpoint_star_resolved_with_kvs_master_service(mock_http, monkeypatch):
    """'*' in pool_endpoint is replaced with the KVS master service domain."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    monkeypatch.setenv("KVS_MASTER_SERVICE", "mindie-motor-kvs-master.mindie.svc.cluster.local")

    instance = _make_mock_instance(1)
    ConductorApiClient._pool_registered = False
    with _setup_reg_config("Memcache", pool_endpoint="tcp://*:5557", npu_endpoint="tcp://*:50090"):
        ConductorApiClient.register_kv_instance([instance])

    calls = mock_client.post.call_args_list
    assert len(calls) == 2
    assert calls[0][0][1]["endpoint"] == "tcp://mindie-motor-kvs-master.mindie.svc.cluster.local:5557"


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_pool_endpoint_star_without_kvs_master_service_skips_pool(mock_http, monkeypatch):
    """Unset KVS_MASTER_SERVICE: '*' pool_endpoint cannot resolve, pool registration skipped."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    monkeypatch.delenv("KVS_MASTER_SERVICE", raising=False)

    instance = _make_mock_instance(1)
    ConductorApiClient._pool_registered = False
    with _setup_reg_config("Memcache", pool_endpoint="tcp://*:5557", npu_endpoint="tcp://*:50090"):
        ConductorApiClient.register_kv_instance([instance])

    calls = mock_client.post.call_args_list
    assert len(calls) == 1  # HBM DP only
    assert "pool" not in calls[0][0][1].get("instance_id", "")


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
    """NPU uses dp_rank port offset; YuanRong CPU/Disk use the node base port."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_mock_instance(1)
    instance.endpoints["pod-0"][0].ip = "10.0.0.1"
    instance.endpoints["pod-0"][0].id = 2  # dp_rank=2
    instance.get_all_endpoints.return_value = (instance.endpoints["pod-0"][0],)

    with _setup_reg_config(
        "YuanRong", npu_endpoint="tcp://*:15557", cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"
    ):
        ConductorApiClient.register_kv_instance([instance])

    payloads = [c[0][1] for c in mock_client.post.call_args_list]
    npu_payload = next(p for p in payloads if "npu" in p.get("medium_endpoints", {}))
    pool_payload = next(p for p in payloads if "pool" in p.get("instance_id", ""))
    assert npu_payload["medium_endpoints"]["npu"] == "tcp://10.0.0.1:15559"  # 15557 + 2
    assert npu_payload["dp_rank"] == 2
    assert pool_payload["medium_endpoints"]["cpu"] == "tcp://10.0.0.1:15558"
    assert pool_payload["medium_endpoints"]["disk"] == "tcp://10.0.0.1:15558"
    assert pool_payload["instance_id"] == "yuanrong-pool-10.0.0.1-test-model"


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_yuanrong_cpu_disk_registered_once_per_node(mock_http):
    """Two DPs on the same node share one CPU/Disk registration."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    ep0 = Mock()
    ep0.id = 0
    ep0.ip = "10.0.0.1"
    ep1 = Mock()
    ep1.id = 1
    ep1.ip = "10.0.0.1"
    instance = Mock()
    instance.id = 1
    instance.model_name = "test-model"
    instance.role = "prefill"
    instance.endpoints = {"pod-0": {0: ep0, 1: ep1}}
    instance.get_all_endpoints.return_value = (ep0, ep1)

    with _setup_reg_config(
        "YuanRong", npu_endpoint="tcp://*:15557", cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"
    ):
        ConductorApiClient.register_kv_instance([instance])

    payloads = [c[0][1] for c in mock_client.post.call_args_list]
    npu_payloads = [p for p in payloads if "npu" in p.get("medium_endpoints", {})]
    pool_payloads = [p for p in payloads if "pool" in p.get("instance_id", "")]
    assert len(npu_payloads) == 2
    assert len(pool_payloads) == 1
    assert {p["dp_rank"] for p in npu_payloads} == {0, 1}


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_unknown_backend_falls_back_to_per_node(mock_http):
    """Unknown backend → per_node mode (treats as YuanRong)."""
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


# ── YuanRong node pool lifecycle ─────────────────────────────────────


def _make_node_instance(instance_id, ip, dp_ranks, model_name="test-model"):
    """Mock instance whose DPs all sit on one node IP."""
    eps = []
    for dp_rank in dp_ranks:
        ep = Mock()
        ep.id = dp_rank
        ep.ip = ip
        eps.append(ep)
    instance = Mock()
    instance.id = instance_id
    instance.model_name = model_name
    instance.role = "prefill"
    instance.endpoints = {"pod-0": {ep.id: ep for ep in eps}}
    instance.get_all_endpoints.return_value = tuple(eps)
    return instance


def _pool_payloads(mock_client, path="/register"):
    """Node-pool payloads sent to `path` (DP payloads excluded)."""
    return [
        c[0][1] for c in mock_client.post.call_args_list if c[0][0] == path and "pool" in c[0][1].get("instance_id", "")
    ]


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_yuanrong_node_pool_unregistered_with_last_dp(mock_http):
    """The per-node CPU/Disk pool outlives the DPs until the last one leaves."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    # Two engine instances share node 10.0.0.1; each contributes one DP.
    inst_a = _make_node_instance(1, "10.0.0.1", [0])
    inst_b = _make_node_instance(2, "10.0.0.1", [0])

    with _setup_reg_config(
        "YuanRong", npu_endpoint="tcp://*:15557", cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"
    ):
        ConductorApiClient.register_kv_instance([inst_a, inst_b])
        assert len(_pool_payloads(mock_client)) == 1  # one pool for the node
        mock_client.post.reset_mock()

        # First DP leaves: the pool is still serving the second one.
        ConductorApiClient.unregister_kv_instance([inst_a])
        assert len(mock_client.post.call_args_list) == 1  # the DP alone
        assert not _pool_payloads(mock_client, "/unregister"), "pool torn down while a DP is still registered"

        # Last DP leaves: the pool goes with it.
        ConductorApiClient.unregister_kv_instance([inst_b])
        pool_unregisters = _pool_payloads(mock_client, "/unregister")
        assert len(pool_unregisters) == 1
        assert pool_unregisters[0]["instance_id"] == "yuanrong-pool-10.0.0.1-test-model"
        assert pool_unregisters[0]["dp_rank"] == 0

    # Cache cleared, so the node can be registered again later.
    assert ConductorApiClient._yuanrong_nodes_registered == set()
    assert ConductorApiClient._yuanrong_node_refs == {}


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_yuanrong_node_pool_is_per_model(mock_http):
    """Two models on one node get distinct pool ids instead of evicting each other."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    inst_a = _make_node_instance(1, "10.0.0.1", [0], model_name="model-a")
    inst_b = _make_node_instance(2, "10.0.0.1", [0], model_name="model-b")

    with _setup_reg_config(
        "YuanRong", npu_endpoint="tcp://*:15557", cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"
    ):
        ConductorApiClient.register_kv_instance([inst_a, inst_b])

    assert sorted(p["instance_id"] for p in _pool_payloads(mock_client)) == [
        "yuanrong-pool-10.0.0.1-model-a",
        "yuanrong-pool-10.0.0.1-model-b",
    ]


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_re_register_restores_node_pool_after_conductor_restart(mock_http):
    """A conductor that lost its state gets the node pool back.

    The HBM DP is restored by the (instance_id, dp_rank) comparison; the pool
    has no such entry, so it is only rebuilt because the local cache is
    re-synced against GET /workers.
    """
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_node_instance(1, "10.0.0.1", [0])
    with _setup_reg_config(
        "YuanRong", npu_endpoint="tcp://*:15557", cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"
    ):
        ConductorApiClient.register_kv_instance([instance])
        assert ConductorApiClient._yuanrong_nodes_registered  # cached as registered
        mock_client.post.reset_mock()

        with patch.object(ConductorApiClient, "get_registered_services", return_value=[]):
            ConductorApiClient.re_register_kv_instances([instance])

    pools = _pool_payloads(mock_client)
    assert len(pools) == 1
    assert pools[0]["instance_id"] == "yuanrong-pool-10.0.0.1-test-model"


@patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient")
def test_re_register_node_pool_only_leaves_live_dps_alone(mock_http):
    """Pool missing while its DPs are still registered → restore the pool only."""
    mock_client = Mock()
    mock_client.post.return_value = {"status": "ok"}
    mock_http.return_value.__enter__.return_value = mock_client

    instance = _make_node_instance(1, "10.0.0.1", [0, 1])
    with _setup_reg_config(
        "YuanRong", npu_endpoint="tcp://*:15557", cpu_endpoint="tcp://*:15558", disk_endpoint="tcp://*:15558"
    ):
        ConductorApiClient.register_kv_instance([instance])
        mock_client.post.reset_mock()

        # The conductor still holds both DPs but no longer the pool entry.
        workers = [{"instance_id": "vllm-prefill-1", "endpoints": {"0": {}, "1": {}}}]
        with patch.object(ConductorApiClient, "get_registered_services", return_value=workers):
            ConductorApiClient.re_register_kv_instances([instance])

    # Both DPs are registered, so only the pool is re-sent — and only once for
    # the whole node rather than once per DP.
    payloads = [c[0][1] for c in mock_client.post.call_args_list]
    assert len(payloads) == 1
    assert payloads[0]["instance_id"] == "yuanrong-pool-10.0.0.1-test-model"
