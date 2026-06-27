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
async def test_scheduler_select_and_allocate_prefill_success():
    """select_and_allocate for a single role returns (instance, endpoint, workload) on success."""
    policy = _Policy()
    scheduler = Scheduler(_Provider(policy.p, policy.d), CoordinatorConfig())
    scheduler._scheduling_policy = policy

    result = await scheduler.select_and_allocate(PDRole.ROLE_P, _req_info())

    assert result is not None
    instance, endpoint, workload = result
    assert instance.role == PDRole.ROLE_P
    assert policy.update_workload.await_count == 1


@pytest.mark.asyncio
async def test_scheduler_select_and_allocate_returns_none_when_allocation_fails():
    """select_and_allocate returns None when update_workload fails for the selected role."""
    policy = _Policy(fail_decode_allocation=True)
    scheduler = Scheduler(_Provider(policy.p, policy.d), CoordinatorConfig())
    scheduler._scheduling_policy = policy

    # First call succeeds (prefill), second call fails (decode with side_effect=[True, False, ...])
    result_p = await scheduler.select_and_allocate(PDRole.ROLE_P, _req_info())
    assert result_p is not None
    assert policy.update_workload.await_count == 1

    result_d = await scheduler.select_and_allocate(PDRole.ROLE_D, _req_info())
    assert result_d is None
    assert policy.update_workload.await_count == 2


@pytest.mark.asyncio
async def test_async_scheduler_client_allocates_with_allocate_only_rpc():
    """select_and_allocate sends a single ALLOCATE_ONLY RPC for the given role."""
    p = _instance(1, PDRole.ROLE_P)
    p_endpoint = next(iter(next(iter(p.endpoints.values())).values()))
    response = SchedulerResponse(
        response_type=SchedulerResponseType.SUCCESS,
        request_id="r",
        data={
            "instance": p.model_dump(mode="json"),
            "endpoint": p_endpoint.model_dump(mode="json"),
        },
    )
    client = AsyncSchedulerClient(SchedulerClientConfig())
    await client._cache.replace_all(PDRole.ROLE_P, [p])
    transport = _Transport(response)
    client._transport = transport

    result = await client.select_and_allocate(PDRole.ROLE_P, _req_info())

    assert result is not None
    instance, endpoint, workload = result
    assert len(transport.requests) == 1
    assert transport.requests[0].request_type == SchedulerRequestType.ALLOCATE_ONLY
    assert instance.engine_type == "vllm"
    assert instance.dispatch_capabilities == ["concurrent_engine_sync"]


@pytest.mark.asyncio
async def test_async_scheduler_client_selects_compatible_instance_from_pool():
    """select_and_allocate selects from compatible instances for the given role."""
    compatible_p = _instance(2, PDRole.ROLE_P, DispatchPlan.PREFILL_HANDOFF_DECODE.value)
    p_endpoint = compatible_p.get_all_endpoints()[0]
    response = SchedulerResponse(
        response_type=SchedulerResponseType.SUCCESS,
        request_id="r",
        data={
            "instance": compatible_p.model_dump(mode="json"),
            "endpoint": p_endpoint.model_dump(mode="json"),
        },
    )
    client = AsyncSchedulerClient(SchedulerClientConfig())
    await client._cache.replace_all(PDRole.ROLE_P, [compatible_p])
    transport = _Transport(response)
    client._transport = transport

    result = await client.select_and_allocate(PDRole.ROLE_P, _req_info())

    assert result is not None
    instance, endpoint, workload = result
    assert instance.id == compatible_p.id


@pytest.mark.asyncio
async def test_scheduler_server_allocate_only_success():
    """ALLOCATE_ONLY with valid endpoint returns instance and endpoint data."""
    manager = _Manager()
    scheduler = AsyncMock()
    scheduler.update_workload = AsyncMock(return_value=True)
    dispatcher = _SchedulerRequestDispatcher(manager, scheduler, CoordinatorConfig())

    response = await dispatcher.dispatch(
        SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id="alloc",
            data={
                "instance_id": 1,
                "endpoint_id": 1,
                "role": "prefill",
                "req_id": "req",
                "workload": Workload(active_tokens=10).model_dump(mode="json"),
            },
        )
    )

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["instance"] is not None
    assert response.data["instance"]["id"] == 1
    scheduler.update_workload.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_server_allocate_only_returns_none_when_allocation_fails():
    """ALLOCATE_ONLY returns SUCCESS with instance=None when update_workload fails."""
    manager = _Manager()
    scheduler = AsyncMock()
    scheduler.update_workload = AsyncMock(return_value=False)
    dispatcher = _SchedulerRequestDispatcher(manager, scheduler, CoordinatorConfig())

    response = await dispatcher.dispatch(
        SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id="alloc",
            data={
                "instance_id": 1,
                "endpoint_id": 1,
                "role": "prefill",
                "req_id": "req",
                "workload": Workload(active_tokens=10).model_dump(mode="json"),
            },
        )
    )

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["instance"] is None
    scheduler.update_workload.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_server_allocate_only_preserves_engine_metadata():
    """ALLOCATE_ONLY response includes engine metadata from the selected instance."""
    manager = _Manager()
    scheduler = AsyncMock()
    scheduler.update_workload = AsyncMock(return_value=True)
    dispatcher = _SchedulerRequestDispatcher(manager, scheduler, CoordinatorConfig())

    response = await dispatcher.dispatch(
        SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id="alloc",
            data={
                "instance_id": 1,
                "endpoint_id": 1,
                "role": "prefill",
                "req_id": "req",
                "workload": Workload(active_tokens=10).model_dump(mode="json"),
            },
        )
    )

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["instance"]["engine_type"] == "vllm"
    assert response.data["instance"]["dispatch_capabilities"] == ["concurrent_engine_sync"]


# --- Fix #1: P/D pair path refreshes live workload / instance membership before selecting ---


@pytest.mark.asyncio
async def test_select_and_allocate_refreshes_cache_before_selection():
    """select_and_allocate must refresh the workload cache before selecting an endpoint."""
    p = _instance(1, PDRole.ROLE_P)
    p_endpoint = next(iter(next(iter(p.endpoints.values())).values()))
    response = SchedulerResponse(
        response_type=SchedulerResponseType.SUCCESS,
        request_id="r",
        data={
            "instance": p.model_dump(mode="json"),
            "endpoint": p_endpoint.model_dump(mode="json"),
        },
    )
    client = AsyncSchedulerClient(SchedulerClientConfig())
    await client._cache.replace_all(PDRole.ROLE_P, [p])
    client._refresh_cache_from_workload_reader = AsyncMock()
    client._transport = _Transport(response)

    result = await client.select_and_allocate(PDRole.ROLE_P, _req_info())

    assert result is not None
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
