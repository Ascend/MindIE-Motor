from unittest.mock import AsyncMock

import pytest

from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
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


def _instance(instance_id: int, role: PDRole) -> Instance:
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
        dispatch_capabilities=["concurrent_engine_sync"],
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
        endpoints={endpoint.ip: {endpoint.id: endpoint}},
    )


class _Policy:
    def __init__(self, fail_decode=False):
        self.fail_decode = fail_decode
        self.p = _instance(1, PDRole.ROLE_P)
        self.d = _instance(2, PDRole.ROLE_D)
        self.update_workload = AsyncMock(return_value=True)

    def select_instance_and_endpoint(self, role):
        if role == PDRole.ROLE_D and self.fail_decode:
            return None
        instance = self.p if role == PDRole.ROLE_P else self.d
        endpoint = next(iter(next(iter(instance.endpoints.values())).values()))
        return instance, endpoint


class _Provider:
    pass


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
    scheduler = Scheduler(_Provider(), CoordinatorConfig())
    policy = _Policy()
    scheduler._scheduling_policy = policy

    pair = await scheduler.select_pair_and_allocate(_req_info())

    assert pair is not None
    assert pair.prefill.instance.role == PDRole.ROLE_P
    assert pair.decode.instance.role == PDRole.ROLE_D
    assert policy.update_workload.await_count == 2


@pytest.mark.asyncio
async def test_scheduler_select_pair_and_allocate_compensates_prefill_when_decode_fails():
    scheduler = Scheduler(_Provider(), CoordinatorConfig())
    policy = _Policy(fail_decode=True)
    scheduler._scheduling_policy = policy

    pair = await scheduler.select_pair_and_allocate(_req_info())

    assert pair is None
    # P allocation + release tokens + release kv.
    assert policy.update_workload.await_count == 3


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
    client.select_instance_and_endpoint = AsyncMock(side_effect=[(p, p_endpoint), (d, d_endpoint)])
    transport = _Transport(response)
    client._transport = transport

    pair = await client.select_pair_and_allocate(_req_info())

    assert pair is not None
    assert len(transport.requests) == 1
    assert transport.requests[0].request_type == SchedulerRequestType.ALLOCATE_PAIR
    assert pair.prefill.instance.engine_type == "vllm"
    assert pair.prefill.instance.dispatch_capabilities == ["concurrent_engine_sync"]


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
