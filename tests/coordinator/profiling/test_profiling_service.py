# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""ProfilingService: ordered engine calls and per-target results."""

import asyncio

import pytest
from requests.exceptions import ReadTimeout

from motor.common.http.http_client import HttpStatusError
from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import Instance
from motor.config.coordinator import ProfilingConfig
from motor.coordinator.profiling.models import (
    ProfilingStartRequest,
    ProfilingTargetSelector,
    ProfilingTargetStatus,
)
from motor.coordinator.profiling.service import (
    ProfilingError,
    ProfilingService,
    ProfilingUnavailableError,
)


def _instance(instance_id: int = 0, role: str = "prefill", dp_ranks: tuple[int, ...] = (0, 1)) -> Instance:
    pod_ip = f"10.0.0.{instance_id + 1}"
    endpoints = {rank: Endpoint(id=rank, ip=pod_ip, business_port=str(8000 + rank)) for rank in dp_ranks}
    return Instance(
        job_name=f"inst-{instance_id}",
        model_name="deepseek-v3",
        id=instance_id,
        role=role,
        endpoints={pod_ip: endpoints},
    )


class _EngineRecorder:
    """Record engine profiling calls and inject per-address failures."""

    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.calls: list[tuple[str, str, float, dict | None]] = []
        self.failures = failures or {}

    def __call__(self, action, address, tls_config, timeout, body) -> None:
        self.calls.append((action.value, address, timeout, body))
        failure = self.failures.get(address)
        if failure is not None:
            raise failure


def _provider(instances: dict[int, Instance]):
    async def provide() -> dict[int, Instance]:
        return instances

    return provide


def _service(
    instances: dict[int, Instance] | None = None,
    recorder: _EngineRecorder | None = None,
    config: ProfilingConfig | None = None,
) -> tuple[ProfilingService, _EngineRecorder]:
    engine = recorder or _EngineRecorder()
    service = ProfilingService(
        config=config or ProfilingConfig(enable=True),
        tls_config=None,
        instances_provider=_provider(instances if instances is not None else {0: _instance()}),
        engine_call=engine,
    )
    return service, engine


async def test_start_merges_caller_profiler_options_into_the_engine_body():
    service, engine = _service()

    await service.start(ProfilingStartRequest(profiler_options={"profile_prefix": "manual_run", "activities": ["CPU"]}))

    calls_by_address = {call[1]: call for call in engine.calls}
    body = calls_by_address["10.0.0.1:8000"][3]
    assert body["activities"] == ["CPU"]
    assert body["profile_prefix"] == "manual_run_ins-0_role-prefill_dp-0"


async def test_unavailable_instance_view_maps_to_a_service_unavailable_error():
    async def unavailable():
        raise RuntimeError("scheduler client is not connected")

    service = ProfilingService(
        config=ProfilingConfig(enable=True),
        tls_config=None,
        instances_provider=unavailable,
        engine_call=_EngineRecorder(),
    )

    with pytest.raises(ProfilingUnavailableError):
        await service.start(ProfilingStartRequest())


async def test_empty_instance_view_maps_to_a_service_unavailable_error():
    service, _ = _service(instances={})

    with pytest.raises(ProfilingUnavailableError):
        await service.start(ProfilingStartRequest())


async def test_selector_that_matches_nothing_is_a_client_error():
    service, _ = _service()

    with pytest.raises(ProfilingError) as error:
        await service.start(ProfilingStartRequest(instances=[5]))

    assert error.value.status_code == 400


@pytest.mark.parametrize("prefix", ["x" * 128, "_trace", "invalid/prefix"])
async def test_invalid_effective_prefix_is_rejected_before_fanout(prefix: str) -> None:
    service, engine = _service()
    with pytest.raises(ProfilingError, match="profile_prefix"):
        await service.start(ProfilingStartRequest(profiler_options={"profile_prefix": prefix}))
    assert engine.calls == []


async def test_start_and_stop_use_the_same_dp_selector() -> None:
    service, engine = _service()

    started = await service.start(ProfilingStartRequest(dps=[1]))
    stopped = await service.stop(ProfilingTargetSelector(dps=[1]))

    assert (started.succeeded, stopped.succeeded) == (1, 1)
    assert started.targets[0].profile_prefix == "motor_profile_svc-deepseek-v3_ins-0_role-prefill_dp-1"
    assert stopped.targets[0].status == ProfilingTargetStatus.STOPPED
    assert [call[:2] for call in engine.calls] == [("start", "10.0.0.1:8001"), ("stop", "10.0.0.1:8001")]


async def test_stop_resolves_current_targets_without_a_previous_start() -> None:
    service, engine = _service()

    stopped = await service.stop(ProfilingTargetSelector(roles=["prefill"]))

    assert stopped.succeeded == 2
    assert sorted(call[:2] for call in engine.calls) == [("stop", "10.0.0.1:8000"), ("stop", "10.0.0.1:8001")]


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (HttpStatusError(404, "HTTP 404: no profiling route"), "HTTP 404: no profiling route"),
        (ReadTimeout("engine response timed out"), "engine response timed out"),
        (RuntimeError(), "RuntimeError"),
    ],
)
@pytest.mark.parametrize("action", ["start", "stop"])
async def test_control_reports_engine_error_and_continues(action: str, error: Exception, detail: str) -> None:
    engine = _EngineRecorder(failures={"10.0.0.1:8000": error})
    service, _ = _service(recorder=engine)

    if action == "start":
        result = await service.start(ProfilingStartRequest())
        expected_success = ProfilingTargetStatus.STARTED
    else:
        result = await service.stop(ProfilingTargetSelector())
        expected_success = ProfilingTargetStatus.STOPPED

    failed, succeeded = result.targets
    assert (failed.address, failed.status, failed.detail) == ("10.0.0.1:8000", ProfilingTargetStatus.FAILED, detail)
    assert (succeeded.address, succeeded.status) == ("10.0.0.1:8001", expected_success)
    assert (result.succeeded, result.failed) == (1, 1)


@pytest.mark.parametrize("cancel_start", [False, True])
async def test_engine_concurrency_is_bounded_and_requests_stay_serial(
    cancel_start: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    engine = _EngineRecorder()
    in_flight: list[str] = []

    async def blocked_engine_call(function, action, target, *args, **kwargs):
        if action.value == "start":
            in_flight.append(target.address)
            if len(in_flight) == 8:
                entered.set()
            await release.wait()
        return function(action, target, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", blocked_engine_call)
    service = ProfilingService(
        config=ProfilingConfig(enable=True),
        tls_config=None,
        instances_provider=_provider({0: _instance(dp_ranks=tuple(range(10)))}),
        engine_call=engine,
    )
    start = asyncio.create_task(service.start(ProfilingStartRequest()))
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if cancel_start:
            start.cancel()
        stop = asyncio.create_task(service.stop(ProfilingTargetSelector()))
        await asyncio.sleep(0)
        assert not stop.done()
        assert in_flight == [f"10.0.0.1:{port}" for port in range(8000, 8008)]
        assert engine.calls == []
    finally:
        release.set()
        if cancel_start and stop is not None:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(start, timeout=2)
        else:
            await asyncio.wait_for(start, timeout=2)
        if stop is not None:
            await asyncio.wait_for(stop, timeout=2)

    start_count = 8 if cancel_start else 10
    assert [call[0] for call in engine.calls] == ["start"] * start_count + ["stop"] * 10
    assert len(in_flight) == start_count
