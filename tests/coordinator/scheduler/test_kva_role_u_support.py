# -*- coding: utf-8 -*-
"""Regression tests for ROLE_U support in KVA register/select flows."""

from unittest.mock import Mock, patch

from motor.common.resources.instance import PDRole
from motor.coordinator.api_client.conductor_api_client import (
    ConductorApiClient,
    conductor_instance_id,
)
from motor.config.coordinator import KV_AFFINITY_MODE_LOAD_GATED
from motor.coordinator.scheduler.runtime.scheduler_client import (
    AsyncSchedulerClient,
    SchedulerClientConfig,
)


def _build_instance(role: PDRole) -> Mock:
    instance = Mock()
    instance.role = role
    endpoint = Mock()
    instance.endpoints = {"pod-0": {0: endpoint}}
    instance.get_all_endpoints.return_value = (endpoint,)
    return instance


def _build_kv_client() -> AsyncSchedulerClient:
    return AsyncSchedulerClient(
        SchedulerClientConfig(
            scheduler_type="kv_cache_affinity",
        )
    )


def test_conductor_instance_id_role_u() -> None:
    instance = _build_instance(PDRole.ROLE_U)
    instance.id = 7
    assert conductor_instance_id(instance) == "vllm-union-7"


def test_conductor_instance_id_role_p() -> None:
    instance = _build_instance(PDRole.ROLE_P)
    instance.id = 3
    assert conductor_instance_id(instance) == "vllm-prefill-3"


def test_register_post_uses_union_conductor_id_for_role_u() -> None:
    instance = _build_instance(PDRole.ROLE_U)
    instance.id = 2
    instance.model_name = "qwen3-8B"
    endpoint = Mock()
    endpoint.id = 0
    endpoint.ip = "10.0.0.1"

    mock_config = Mock()
    mock_config.prefill_kv_event_config.endpoint = "tcp://*:5557"
    mock_config.prefill_kv_event_config.replay_endpoint = ""
    mock_config.prefill_kv_event_config.engine_type = "vLLM"
    mock_config.prefill_kv_event_config.block_size = 128
    mock_config.prefill_kv_event_config.conductor_service = "kv-conductor"
    mock_config.prefill_kv_event_config.http_server_port = 13333

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
    mock_config.prefill_kv_event_config.endpoint = "tcp://*:5557"
    mock_config.prefill_kv_event_config.replay_endpoint = "tcp://*:6667"
    mock_config.prefill_kv_event_config.engine_type = "vLLM"
    mock_config.prefill_kv_event_config.block_size = 128
    mock_config.prefill_kv_event_config.conductor_service = "2001:db8::10"
    mock_config.prefill_kv_event_config.http_server_port = 13333

    with (
        patch.object(ConductorApiClient, "coordinator_config", mock_config),
        patch("motor.coordinator.api_client.conductor_api_client.SafeHTTPSClient") as mock_http_client,
    ):
        mock_http_client.return_value.__enter__.return_value.post.return_value = None
        ConductorApiClient.register_post(instance, endpoint)

    mock_http_client.assert_called_once()
    assert mock_http_client.call_args.kwargs["address"] == "[2001:db8::10]:13333"
    register_payload = mock_http_client.return_value.__enter__.return_value.post.call_args[0][1]
    assert register_payload["endpoint"] == "tcp://[2001:db8::1]:5559"
    assert register_payload["replay_endpoint"] == "tcp://[2001:db8::1]:6669"


def test_register_kv_instance_supports_role_u() -> None:
    instances = [
        _build_instance(PDRole.ROLE_P),
        _build_instance(PDRole.ROLE_U),
        _build_instance(PDRole.ROLE_D),
    ]
    with patch.object(ConductorApiClient, "register_post") as mock_register_post:
        ConductorApiClient.register_kv_instance(instances)

    assert mock_register_post.call_count == 2
    called_roles = {call.args[0].role for call in mock_register_post.call_args_list}
    assert called_roles == {PDRole.ROLE_P, PDRole.ROLE_U}


def test_unregister_kv_instance_supports_role_u() -> None:
    instances = [
        _build_instance(PDRole.ROLE_P),
        _build_instance(PDRole.ROLE_U),
        _build_instance(PDRole.ROLE_D),
    ]
    with patch.object(ConductorApiClient, "unregister_post") as mock_unregister_post:
        ConductorApiClient.unregister_kv_instance(instances)

    assert mock_unregister_post.call_count == 2
    called_roles = {call.args[0].role for call in mock_unregister_post.call_args_list}
    assert called_roles == {PDRole.ROLE_P, PDRole.ROLE_U}


def test_kv_cache_affinity_uses_kva_for_role_u() -> None:
    client = _build_kv_client()
    instance = Mock()
    endpoint = Mock()
    req_info = Mock()
    ranked = [(instance, endpoint, 0.0)]

    with (
        patch(
            "motor.coordinator.scheduler.runtime.scheduler_client."
            "KvCacheAffinityPolicy.select_endpoint_candidates_from_list",
            return_value=ranked,
        ) as mock_kva,
        patch.object(
            client,
            "_select_endpoint_candidates_by_load_balance",
        ) as mock_load_balance,
    ):
        candidates, candidate_policy = client._select_endpoint_candidates_from_list_with_policy(
            [instance], PDRole.ROLE_U, req_info, top_k=1
        )

    assert candidates == ranked
    assert candidate_policy == "kv_cache_affinity"
    mock_kva.assert_called_once()
    mock_load_balance.assert_not_called()


def test_kv_cache_affinity_falls_back_to_load_balance_for_role_u() -> None:
    client = _build_kv_client()
    instance = Mock()
    endpoint = Mock()
    req_info = Mock()
    lb_instance = Mock()
    lb_candidates = [(lb_instance, endpoint, 0.42)]

    with (
        patch(
            "motor.coordinator.scheduler.runtime.scheduler_client."
            "KvCacheAffinityPolicy.select_endpoint_candidates_from_list",
            return_value=[],
        ) as mock_kva,
        patch.object(
            client,
            "_select_endpoint_candidates_by_load_balance",
            return_value=lb_candidates,
        ) as mock_load_balance,
    ):
        candidates, candidate_policy = client._select_endpoint_candidates_from_list_with_policy(
            [instance], PDRole.ROLE_U, req_info, top_k=1
        )

    assert candidates == lb_candidates
    assert candidate_policy == "load_balance"
    mock_kva.assert_called_once()
    mock_load_balance.assert_called_once_with([instance], PDRole.ROLE_U, 1)


async def test_select_and_allocate_role_u_unified_forwards_top1() -> None:
    """Unified affinity forwards every endpoint (with prefill_cost) to the scheduler for a global
    re-rank, so the worker only needs its own top-1 locally.
    """
    client = _build_kv_client()  # default kv_affinity_mode is unified
    req_info = Mock()
    req_info.req_id = "req-1"
    req_info.req_data = {}
    req_info.req_len = 0

    with patch.object(
        client,
        "_select_endpoint_candidates_with_policy",
        return_value=([], "kv_cache_affinity"),
    ) as mock_select:
        await client.select_and_allocate(PDRole.ROLE_U, req_info)

    mock_select.assert_awaited_once()
    assert mock_select.await_args.kwargs["top_k"] == 1


async def test_select_and_allocate_role_u_load_gated_uses_affinity_top_k() -> None:
    """load_gated still proposes a fixed ranked alternate set the scheduler picks among, so it
    keeps the affinity topK.
    """
    client = _build_kv_client()
    client._kv_affinity_mode = KV_AFFINITY_MODE_LOAD_GATED
    req_info = Mock()
    req_info.req_id = "req-1"
    req_info.req_data = {}
    req_info.req_len = 0

    with patch.object(
        client,
        "_select_endpoint_candidates_with_policy",
        return_value=([], "kv_cache_affinity"),
    ) as mock_select:
        await client.select_and_allocate(PDRole.ROLE_U, req_info)

    mock_select.assert_awaited_once()
    assert mock_select.await_args.kwargs["top_k"] == 3


def test_kv_cache_affinity_skips_kva_for_non_kva_roles() -> None:
    client = _build_kv_client()
    instance = Mock()
    endpoint = Mock()
    req_info = Mock()
    lb_instance = Mock()
    lb_candidates = [(lb_instance, endpoint, 0.24)]

    with (
        patch(
            "motor.coordinator.scheduler.runtime.scheduler_client."
            "KvCacheAffinityPolicy.select_endpoint_candidates_from_list"
        ) as mock_kva,
        patch.object(
            client,
            "_select_endpoint_candidates_by_load_balance",
            return_value=lb_candidates,
        ) as mock_load_balance,
    ):
        candidates, candidate_policy = client._select_endpoint_candidates_from_list_with_policy(
            [instance], PDRole.ROLE_D, req_info, top_k=1
        )

    assert candidates == lb_candidates
    assert candidate_policy == "load_balance"
    mock_kva.assert_not_called()
    mock_load_balance.assert_called_once_with([instance], PDRole.ROLE_D, 1)
