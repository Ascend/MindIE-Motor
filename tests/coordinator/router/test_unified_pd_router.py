from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import httpx
import pytest

from motor.common.resources.dispatch import (
    DispatchPlan,
    MOTOR_DISPATCH_KEY,
    MOTOR_PREFILL_RESULT_KEY,
)
from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.instance import Instance, InsStatus, ParallelConfig, PDRole
from motor.config.coordinator import CoordinatorConfig, ExceptionConfig, SchedulerType
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.router.dispatch_capability import DispatchPlanNotSupported, select_dispatch_plan_for_pair
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter


def _instance(
    instance_id: int,
    role: PDRole,
    *,
    engine_type: str | None = None,
    dispatch_capabilities: list[str] | None = None,
) -> Instance:
    endpoint = Endpoint(
        id=instance_id,
        ip="127.0.0.1",
        business_port=str(8100 + instance_id),
        mgmt_port=str(9100 + instance_id),
        status=EndpointStatus.NORMAL,
    )
    return Instance(
        job_name=f"job-{instance_id}",
        model_name=engine_type or "model",
        engine_type=engine_type,
        dispatch_capabilities=dispatch_capabilities or [],
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
        endpoints={endpoint.ip: {endpoint.id: endpoint}},
    )


class _Scheduler:
    def __init__(
        self,
        enable_pair: bool = False,
        *,
        prefill_engine_type: str | None = None,
        decode_engine_type: str | None = None,
        prefill_capabilities: list[str] | None = None,
        decode_capabilities: list[str] | None = None,
    ):
        if prefill_capabilities is None:
            prefill_capabilities = [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
        if decode_capabilities is None:
            decode_capabilities = [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
        self.p = _instance(
            1,
            PDRole.ROLE_P,
            engine_type=prefill_engine_type,
            dispatch_capabilities=prefill_capabilities,
        )
        self.d = _instance(
            2,
            PDRole.ROLE_D,
            engine_type=decode_engine_type,
            dispatch_capabilities=decode_capabilities,
        )
        self.enable_pair = enable_pair
        self.pair_calls = 0
        self.update_workload = AsyncMock(return_value=True)

    async def select_and_allocate(self, role, req_info, **_kwargs):
        instance = self.p if role == PDRole.ROLE_P else self.d
        endpoint = next(iter(next(iter(instance.endpoints.values())).values()))
        return instance, endpoint, Workload(active_kv_cache=1, active_tokens=1)

    async def select_pair_and_allocate(self, req_info):
        if not self.enable_pair:
            return None
        from motor.coordinator.domain import ScheduledPair

        self.pair_calls += 1
        p_endpoint = next(iter(next(iter(self.p.endpoints.values())).values()))
        d_endpoint = next(iter(next(iter(self.d.endpoints.values())).values()))
        return ScheduledPair(
            prefill=ScheduledResource(instance=self.p, endpoint=p_endpoint),
            decode=ScheduledResource(instance=self.d, endpoint=d_endpoint),
            prefill_workload=Workload(active_kv_cache=1, active_tokens=1),
            decode_workload=Workload(active_kv_cache=0, active_tokens=1),
        )


class _Client:
    def __init__(self, name: str, exc: Exception | None = None):
        self.name = name
        self.exc = exc
        self.requests = []
        self.headers = []
        self.base_url = f"http://{name}"
        self.timeout = 1

    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        if self.exc is not None:
            raise self.exc
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        if self.name == "prefill":
            return httpx.Response(
                status_code=200,
                json={"status": "cached", "id": json["request_id"]},
                request=request,
            )
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            request=request,
        )


class _PrefillResultClient(_Client):
    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        dispatch = json[MOTOR_DISPATCH_KEY]
        return httpx.Response(
            status_code=200,
            json={
                "object": "motor.prefill_result",
                "schema_version": "1.0",
                "root_request_id": dispatch["root_request_id"],
                "engine_request_id": dispatch["engine_request_id"],
                "pair_id": dispatch["pair_id"],
                "attempt_seq": dispatch["attempt_seq"],
                "status": "completed",
                "handoff_mode": "handoff",
                "payload": {"opaque": "kv"},
            },
            request=request,
        )


class _StreamResponse:
    def __init__(self, chunks, exc_after_chunks: Exception | None = None):
        self.chunks = chunks
        self.exc_after_chunks = exc_after_chunks
        self.status_code = 200
        self.is_success = True
        self.text = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def aread(self):
        return b""

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk
        if self.exc_after_chunks is not None:
            raise self.exc_after_chunks


class _StreamClient(_Client):
    def __init__(self, name: str, exc_after_chunks: Exception | None = None):
        super().__init__(name)
        self.exc_after_chunks = exc_after_chunks

    def stream(self, method, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        return _StreamResponse(
            [
                b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n',
            ],
            exc_after_chunks=self.exc_after_chunks,
        )


def _config() -> CoordinatorConfig:
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    config.exception_config = ExceptionConfig(max_retry=1, retry_delay=0)
    return config


def test_dispatch_plan_prefers_explicit_capability_over_engine_fallback():
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="sglang",
        prefill_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        decode_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
    )
    p_endpoint = next(iter(next(iter(scheduler.p.endpoints.values())).values()))
    d_endpoint = next(iter(next(iter(scheduler.d.endpoints.values())).values()))

    plan = select_dispatch_plan_for_pair(
        prefill=ScheduledResource(instance=scheduler.p, endpoint=p_endpoint),
        decode=ScheduledResource(instance=scheduler.d, endpoint=d_endpoint),
    )

    assert plan == DispatchPlan.CONCURRENT_ENGINE_SYNC


def test_dispatch_plan_requires_connector_capability():
    scheduler = _Scheduler(prefill_capabilities=[], decode_capabilities=[])
    p_endpoint = next(iter(next(iter(scheduler.p.endpoints.values())).values()))
    d_endpoint = next(iter(next(iter(scheduler.d.endpoints.values())).values()))

    with pytest.raises(DispatchPlanNotSupported, match="do not advertise"):
        select_dispatch_plan_for_pair(
            prefill=ScheduledResource(instance=scheduler.p, endpoint=p_endpoint),
            decode=ScheduledResource(instance=scheduler.d, endpoint=d_endpoint),
        )


def test_dispatch_plan_requires_capability_from_both_instances():
    scheduler = _Scheduler(
        prefill_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        decode_capabilities=[],
    )
    p_endpoint = next(iter(next(iter(scheduler.p.endpoints.values())).values()))
    d_endpoint = next(iter(next(iter(scheduler.d.endpoints.values())).values()))

    with pytest.raises(DispatchPlanNotSupported, match="do not advertise"):
        select_dispatch_plan_for_pair(
            prefill=ScheduledResource(instance=scheduler.p, endpoint=p_endpoint),
            decode=ScheduledResource(instance=scheduler.d, endpoint=d_endpoint),
        )


@pytest.mark.asyncio
async def test_unified_pd_nonstream_dispatches_prefill_and_decode_with_same_attempt(monkeypatch):
    req_info = RequestInfo(
        req_id="root-1",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()

    assert response.body == b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1

    p_dispatch = p_client.requests[0][MOTOR_DISPATCH_KEY]
    d_dispatch = d_client.requests[0][MOTOR_DISPATCH_KEY]
    assert p_dispatch["role"] == "prefill"
    assert d_dispatch["role"] == "decode"
    assert p_dispatch["root_request_id"] == "root-1"
    assert d_dispatch["root_request_id"] == "root-1"
    assert p_dispatch["attempt_seq"] == d_dispatch["attempt_seq"] == 1
    assert p_dispatch["pair_id"] == d_dispatch["pair_id"]
    assert p_client.requests[0]["request_id"] == "root-1#a1"
    assert d_client.requests[0]["request_id"] == "root-1#a1"
    assert p_client.headers[0]["X-Request-Id"] == "root-1#a1"
    assert d_client.headers[0]["X-Request-Id"] == "root-1#a1"
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_prefers_pair_allocation(monkeypatch):
    req_info = RequestInfo(
        req_id="root-pair",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(enable_pair=True)
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    await router.handle_request()

    assert scheduler.pair_calls == 1
    assert p_client.requests[0][MOTOR_DISPATCH_KEY]["attempt_seq"] == 1
    # P tokens + P KV + D tokens
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_decode_failure_stops_both_legs(monkeypatch):
    req_info = RequestInfo(
        req_id="root-stop",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _Client("decode", exc=httpx.ConnectError("decode down"))
    stop_calls = []

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason.value))
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    with pytest.raises(httpx.ConnectError):
        await router.handle_request()

    assert len(stop_calls) == 2
    assert {call[0] for call in stop_calls} == {PDRole.ROLE_P, PDRole.ROLE_D}
    assert all(call[1] == 1 for call in stop_calls)
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_dual_dispatch_uses_dispatch_context_not_bootstrap_fields(monkeypatch):
    req_info = RequestInfo(
        req_id="root-sglang",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    await router.handle_request()

    for request_body in (p_client.requests[0], d_client.requests[0]):
        assert "bootstrap_host" not in request_body
        assert "bootstrap_port" not in request_body
        assert "bootstrap_room" not in request_body
        assert request_body[MOTOR_DISPATCH_KEY]["dispatch_mode"] == "pd_pair"


@pytest.mark.asyncio
async def test_unified_pd_cpcd_waits_for_prefill_result_before_decode(monkeypatch):
    req_info = RequestInfo(
        req_id="root-cpcd",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    handoff = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    scheduler = _Scheduler(prefill_capabilities=handoff, decode_capabilities=handoff)
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _PrefillResultClient("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    await router.handle_request()

    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1
    p_dispatch = p_client.requests[0][MOTOR_DISPATCH_KEY]
    d_dispatch = d_client.requests[0][MOTOR_DISPATCH_KEY]
    assert p_dispatch["attempt_seq"] == d_dispatch["attempt_seq"] == 1
    assert p_dispatch["pair_id"] == d_dispatch["pair_id"]
    prefill_result = d_client.requests[0][MOTOR_PREFILL_RESULT_KEY]
    assert prefill_result["status"] == "completed"
    assert prefill_result["handoff_mode"] == "handoff"
    assert prefill_result["payload"] == {"opaque": "kv"}
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_cpcd_sglang_uses_concurrent_plan(monkeypatch):
    req_info = RequestInfo(
        req_id="root-cpcd-sglang",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    concurrent = [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
    scheduler = _Scheduler(
        prefill_engine_type="sglang",
        decode_engine_type="sglang",
        prefill_capabilities=concurrent,
        decode_capabilities=concurrent,
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    await router.handle_request()

    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1
    assert MOTOR_PREFILL_RESULT_KEY not in d_client.requests[0]
    assert d_client.requests[0][MOTOR_DISPATCH_KEY]["dispatch_mode"] == "pd_pair"
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_rejects_pair_without_shared_connector_capability(monkeypatch):
    req_info = RequestInfo(
        req_id="root-mixed",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        decode_capabilities=[DispatchPlan.PREFILL_HANDOFF_DECODE.value],
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    stop_calls = []

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason.value))
        return None

    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    with pytest.raises(RuntimeError, match="no shared dispatch capability"):
        await router.handle_request()

    assert {call[0] for call in stop_calls} == {PDRole.ROLE_P, PDRole.ROLE_D}


@pytest.mark.asyncio
async def test_unified_pd_stream_dispatches_context_and_yields_visible_chunk(monkeypatch):
    req_info = RequestInfo(
        req_id="root-stream",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _StreamClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == [b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n']
    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1
    assert d_client.requests[0][MOTOR_DISPATCH_KEY]["role"] == "decode"
    assert p_client.requests[0][MOTOR_DISPATCH_KEY]["pair_id"] == d_client.requests[0][MOTOR_DISPATCH_KEY]["pair_id"]
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_stream_error_after_visible_chunk_does_not_retry(monkeypatch):
    req_info = RequestInfo(
        req_id="root-stream-error",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _StreamClient("decode", exc_after_chunks=httpx.ReadError("after chunk"))
    stop_calls = []

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason.value))
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks[0] == b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n'
    error_chunk = chunks[1].decode("utf-8") if isinstance(chunks[1], bytes) else chunks[1]
    assert "ReadError" in error_chunk
    assert len(d_client.requests) == 1
    assert len(stop_calls) == 2
    assert scheduler.update_workload.await_count == 3
