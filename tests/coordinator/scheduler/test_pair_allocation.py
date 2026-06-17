from unittest.mock import AsyncMock, Mock

import pytest

from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources.instance import Instance, InsStatus, ParallelConfig, PDRole
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.scheduler import Scheduler
from motor.coordinator.scheduler.runtime.scheduler_client import (
    AsyncSchedulerClient,
    SchedulerClientConfig,
)
from motor.coordinator.scheduler.runtime.scheduler_server import _SchedulerRequestDispatcher
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    SchedulerRequest,
    SchedulerRequestType,
    SchedulerResponse,
    SchedulerResponseType,
)


def _instance(
    instance_id: int,
    role: PDRole,
    capability: str = DispatchPlan.CONCURRENT_ENGINE_SYNC.value,
) -> Instance:
    endpoint = Endpoint(
        id=instance_id,
        ip="127.0.0.1",
        business_port=str(8200 + instance_id),
        mgmt_port=str(9200 + instance_id),
        status=EndpointStatus.NORMAL,
    )
    return Instance(
        job_name=f"job-{instance_id}",
        model_name="model",
        engine_type="vllm",
        dispatch_capabilities=[capability],
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
        endpoints={endpoint.ip: {endpoint.id: endpoint}},
    )


class _Policy:
    def __init__(self, fail_decode_allocation=False):
        self.p = _instance(1, PDRole.ROLE_P)
        self.d = _instance(2, PDRole.ROLE_D)
        allocation_results = [True, False, True, True] if fail_decode_allocation else [True, True]
        self.update_workload = AsyncMock(side_effect=allocation_results)

    def select_instance_and_endpoint(self, role):
        instance = self.p if role == PDRole.ROLE_P else self.d
        endpoint = next(iter(next(iter(instance.endpoints.values())).values()))
        return instance, endpoint


class _Provider:
    def __init__(self, *instances):
        self.instances = instances

    def get_available_instances(self, role):
        return {instance.id: instance for instance in self.instances if PDRole(instance.role) == role}


class _Manager:
    def __init__(self):
        self.p = _instance(1, PDRole.ROLE_P)
        self.d = _instance(2, PDRole.ROLE_D)

    def get_available_instances(self, role):
        if role == PDRole.ROLE_P:
            return {self.p.id: self.p}
        if role == PDRole.ROLE_D:
            return {self.d.id: self.d}
        return {}

    async def has_instance_endpoint(self, instance_id, endpoint_id):
        return instance_id in (self.p.id, self.d.id) and instance_id == endpoint_id


class _Transport:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def send_request(self, request):
        self.requests.append(request)
        return self.response


def _req_info() -> RequestInfo:
    return RequestInfo(
        req_id="req",
        req_data={"model": "m", "prompt": "hi"},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )


@pytest.mark.asyncio
async def test_scheduler_select_pair_and_allocate_success():
    policy = _Policy()
    scheduler = Scheduler(_Provider(policy.p, policy.d), CoordinatorConfig())
    scheduler._scheduling_policy = policy

    pair = await scheduler.select_pair_and_allocate(_req_info())

    assert pair is not None
    assert pair.prefill.instance.role == PDRole.ROLE_P
    assert pair.decode.instance.role == PDRole.ROLE_D
    assert policy.update_workload.await_count == 2


@pytest.mark.asyncio
async def test_scheduler_select_pair_and_allocate_compensates_prefill_when_decode_fails():
    policy = _Policy(fail_decode_allocation=True)
    scheduler = Scheduler(_Provider(policy.p, policy.d), CoordinatorConfig())
    scheduler._scheduling_policy = policy

    pair = await scheduler.select_pair_and_allocate(_req_info())

    assert pair is None
    # P allocation + failed D allocation + release tokens + release kv.
    assert policy.update_workload.await_count == 4


@pytest.mark.asyncio
async def test_async_scheduler_client_allocates_pair_with_one_rpc():
    p = _instance(1, PDRole.ROLE_P)
    d = _instance(2, PDRole.ROLE_D)
    p_endpoint = next(iter(next(iter(p.endpoints.values())).values()))
    d_endpoint = next(iter(next(iter(d.endpoints.values())).values()))
    response = SchedulerResponse(
        response_type=SchedulerResponseType.SUCCESS,
        request_id="r",
        data={
            "prefill_instance": p.model_dump(mode="json"),
            "prefill_endpoint": p_endpoint.model_dump(mode="json"),
            "decode_instance": d.model_dump(mode="json"),
            "decode_endpoint": d_endpoint.model_dump(mode="json"),
        },
    )
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._cached_or_fetch_instances = AsyncMock(side_effect=[[p], [d]])
    transport = _Transport(response)
    client._transport = transport

    pair = await client.select_pair_and_allocate(_req_info())

    assert pair is not None
    assert len(transport.requests) == 1
    assert transport.requests[0].request_type == SchedulerRequestType.ALLOCATE_PAIR
    assert pair.prefill.instance.engine_type == "vllm"
    assert pair.prefill.instance.dispatch_capabilities == ["concurrent_engine_sync"]


@pytest.mark.asyncio
async def test_async_scheduler_client_selects_compatible_pair_from_mixed_pool():
    incompatible_p = _instance(1, PDRole.ROLE_P, DispatchPlan.CONCURRENT_ENGINE_SYNC.value)
    compatible_p = _instance(2, PDRole.ROLE_P, DispatchPlan.PREFILL_HANDOFF_DECODE.value)
    compatible_d = _instance(3, PDRole.ROLE_D, DispatchPlan.PREFILL_HANDOFF_DECODE.value)
    p_endpoint = compatible_p.get_all_endpoints()[0]
    d_endpoint = compatible_d.get_all_endpoints()[0]
    response = SchedulerResponse(
        response_type=SchedulerResponseType.SUCCESS,
        request_id="r",
        data={
            "prefill_instance": compatible_p.model_dump(mode="json"),
            "prefill_endpoint": p_endpoint.model_dump(mode="json"),
            "decode_instance": compatible_d.model_dump(mode="json"),
            "decode_endpoint": d_endpoint.model_dump(mode="json"),
        },
    )
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._cached_or_fetch_instances = AsyncMock(side_effect=[[incompatible_p, compatible_p], [compatible_d]])
    transport = _Transport(response)
    client._transport = transport

    pair = await client.select_pair_and_allocate(_req_info())

    assert pair is not None
    request = transport.requests[0]
    assert request.data["prefill"]["instance_id"] == compatible_p.id
    assert request.data["decode"]["instance_id"] == compatible_d.id


@pytest.mark.asyncio
async def test_scheduler_server_rejects_incompatible_pair_before_allocation():
    manager = _Manager()
    manager.d.dispatch_capabilities = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    scheduler = AsyncMock()
    scheduler.update_workload = AsyncMock(return_value=True)
    dispatcher = _SchedulerRequestDispatcher(manager, scheduler, CoordinatorConfig())

    response = await dispatcher.dispatch(
        SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_PAIR,
            request_id="pair",
            data={
                "req_id": "req",
                "prefill": {
                    "instance_id": 1,
                    "endpoint_id": 1,
                    "workload": Workload(active_tokens=10).model_dump(mode="json"),
                },
                "decode": {
                    "instance_id": 2,
                    "endpoint_id": 2,
                    "workload": Workload(active_kv_cache=5).model_dump(mode="json"),
                },
            },
        )
    )

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["prefill_instance"] is None
    scheduler.update_workload.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_server_allocate_pair_compensates_prefill_when_decode_fails():
    manager = _Manager()
    scheduler = AsyncMock()
    scheduler.update_workload = AsyncMock(side_effect=[True, False, True, True])
    dispatcher = _SchedulerRequestDispatcher(manager, scheduler, CoordinatorConfig())

    response = await dispatcher.dispatch(
        SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_PAIR,
            request_id="pair",
            data={
                "req_id": "req",
                "prefill": {
                    "instance_id": 1,
                    "endpoint_id": 1,
                    "workload": Workload(active_tokens=10).model_dump(mode="json"),
                },
                "decode": {
                    "instance_id": 2,
                    "endpoint_id": 2,
                    "workload": Workload(active_kv_cache=5).model_dump(mode="json"),
                },
            },
        )
    )

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["prefill_instance"] is None
    assert scheduler.update_workload.await_count == 4


@pytest.mark.asyncio
async def test_scheduler_server_allocate_pair_preserves_engine_metadata():
    manager = _Manager()
    scheduler = AsyncMock()
    scheduler.update_workload = AsyncMock(return_value=True)
    dispatcher = _SchedulerRequestDispatcher(manager, scheduler, CoordinatorConfig())

    response = await dispatcher.dispatch(
        SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_PAIR,
            request_id="pair",
            data={
                "req_id": "req",
                "prefill": {
                    "instance_id": 1,
                    "endpoint_id": 1,
                    "workload": Workload(active_tokens=10).model_dump(mode="json"),
                },
                "decode": {
                    "instance_id": 2,
                    "endpoint_id": 2,
                    "workload": Workload(active_kv_cache=5).model_dump(mode="json"),
                },
            },
        )
    )

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["prefill_instance"]["engine_type"] == "vllm"
    assert response.data["prefill_instance"]["dispatch_capabilities"] == ["concurrent_engine_sync"]
    assert response.data["decode_instance"]["engine_type"] == "vllm"
    assert response.data["decode_instance"]["dispatch_capabilities"] == ["concurrent_engine_sync"]


# --- Fix #1: P/D pair path refreshes live workload / instance membership before selecting ---


@pytest.mark.asyncio
async def test_select_pair_and_allocate_refreshes_cache_before_selection():
    """The P/D pair path must run the same cache refresh as the single-role path."""
    p = _instance(1, PDRole.ROLE_P)
    d = _instance(2, PDRole.ROLE_D)
    p_endpoint = next(iter(next(iter(p.endpoints.values())).values()))
    d_endpoint = next(iter(next(iter(d.endpoints.values())).values()))
    response = SchedulerResponse(
        response_type=SchedulerResponseType.SUCCESS,
        request_id="r",
        data={
            "prefill_instance": p.model_dump(mode="json"),
            "prefill_endpoint": p_endpoint.model_dump(mode="json"),
            "decode_instance": d.model_dump(mode="json"),
            "decode_endpoint": d_endpoint.model_dump(mode="json"),
        },
    )
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client._refresh_cache_from_workload_reader = AsyncMock()
    client._cached_or_fetch_instances = AsyncMock(side_effect=[[p], [d]])
    client._transport = _Transport(response)

    pair = await client.select_pair_and_allocate(_req_info())

    assert pair is not None
    client._refresh_cache_from_workload_reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_cache_pulls_instances_on_version_change():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    reader = Mock()
    reader.read_and_patch_cache = Mock(return_value=(7, False))  # new version, heartbeat fresh
    client._workload_reader = reader
    client._last_instance_version = 6
    client._on_instance_refreshed = None
    client.get_available_instances = AsyncMock(return_value={})

    await client._refresh_cache_from_workload_reader()

    reader.read_and_patch_cache.assert_called_once()  # live workload patched into cache
    client.get_available_instances.assert_awaited_once()  # membership pulled
    assert client._last_instance_version == 7


@pytest.mark.asyncio
async def test_refresh_cache_patches_without_pull_when_version_unchanged():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    reader = Mock()
    reader.read_and_patch_cache = Mock(return_value=(6, False))
    client._workload_reader = reader
    client._last_instance_version = 6
    client.get_available_instances = AsyncMock(return_value={})

    await client._refresh_cache_from_workload_reader()

    reader.read_and_patch_cache.assert_called_once()  # still patches live workload
    client.get_available_instances.assert_not_awaited()  # but no redundant membership pull


# --- Fix #2: router cold-start warm-up so a cold cache pulls instead of 503 ---


@pytest.mark.asyncio
async def test_get_available_instance_roles_warms_up_when_cache_cold():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client.get_available_instances = AsyncMock(return_value={})
    # First read sees an empty cache, second read (after warm-up) sees populated roles.
    client._roles_from_cache = Mock(side_effect=[set(), {PDRole.ROLE_P, PDRole.ROLE_D}])

    roles = await client.get_available_instance_roles()

    client.get_available_instances.assert_awaited_once()
    assert roles == {PDRole.ROLE_P, PDRole.ROLE_D}


@pytest.mark.asyncio
async def test_get_available_instance_roles_skips_warmup_when_cache_warm():
    client = AsyncSchedulerClient(SchedulerClientConfig())
    client.get_available_instances = AsyncMock(return_value={})
    client._roles_from_cache = Mock(return_value={PDRole.ROLE_U})

    roles = await client.get_available_instance_roles()

    client.get_available_instances.assert_not_awaited()
    assert roles == {PDRole.ROLE_U}
