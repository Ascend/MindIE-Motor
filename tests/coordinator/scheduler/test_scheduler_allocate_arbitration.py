# -*- coding: utf-8 -*-
"""Tests for SchedulerServer allocate-only candidate arbitration."""

import pytest

from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.config.coordinator import CoordinatorConfig, SchedulerType
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.runtime.scheduler_server import _SchedulerRequestDispatcher
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    CANDIDATE_POLICY_KV_CACHE_AFFINITY,
    CANDIDATE_POLICY_LOAD_BALANCE,
    SchedulerRequest,
    SchedulerRequestType,
    SchedulerResponseType,
)
from motor.coordinator.scheduler.scheduler import Scheduler


class _DummyWorkloadWriter:
    """Minimal workload writer stub for dispatcher allocation tests."""

    def __init__(self):
        self.sequence = 0
        self.instance_version = 1
        self.writes: list[tuple[int, int]] = []

    async def write_single_entry(self, instance_id: int, endpoint_id: int) -> None:
        self.sequence += 2
        self.writes.append((instance_id, endpoint_id))


def _make_prefill_instance(
    instance_id: int,
    endpoint_ids: tuple[int, int],
    role: PDRole = PDRole.ROLE_P,
) -> Instance:
    inst = Instance(
        job_name=f"{role.value}-{instance_id}",
        model_name="test_model",
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=2),
    )
    inst.add_endpoints(
        f"pod-{instance_id}",
        {
            idx: Endpoint(
                id=endpoint_id,
                ip=f"10.0.0.{instance_id}",
                business_port=f"80{idx}",
                mgmt_port=f"90{idx}",
                status=EndpointStatus.NORMAL,
                workload=Workload(),
            )
            for idx, endpoint_id in enumerate(endpoint_ids)
        },
    )
    return inst


@pytest.mark.asyncio
async def test_allocate_only_commits_best_authoritative_candidate():
    """SchedulerServer should score candidates against the current central workload ledger."""
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)

    inst_a = _make_prefill_instance(1, (10, 11))
    inst_b = _make_prefill_instance(2, (20, 21))
    await instance_manager.refresh_instances(EventType.ADD, [inst_a, inst_b])
    await instance_manager.update_instance_workload(1, 10, Workload(active_tokens=20))
    await instance_manager.update_instance_workload(1, 11, Workload(active_tokens=30))
    await instance_manager.update_instance_workload(2, 20, Workload(active_tokens=1))
    await instance_manager.update_instance_workload(2, 21, Workload(active_tokens=40))

    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    workload_writer = _DummyWorkloadWriter()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        workload_writer=workload_writer,
    )
    request = SchedulerRequest(
        request_type=SchedulerRequestType.ALLOCATE_ONLY,
        request_id="alloc-1",
        data={
            "instance_id": 1,
            "endpoint_id": 10,
            "role": PDRole.ROLE_P.value,
            "req_id": "req-1",
            "workload": Workload(active_tokens=3).model_dump(mode="json"),
        },
    )

    response = await dispatcher.dispatch(request)

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["instance"]["id"] == 2
    assert response.data["endpoint"]["id"] == 20
    _, selected_workload = await instance_manager.get_endpoint_workload(2, 20)
    _, stale_workload = await instance_manager.get_endpoint_workload(1, 10)
    assert selected_workload.active_tokens == 4
    assert stale_workload.active_tokens == 20
    assert workload_writer.writes == [(2, 20)]
    assert response.data["fast_path"] is False


@pytest.mark.asyncio
async def test_allocate_only_load_balance_scans_all_endpoints_when_sequence_mismatch():
    """Non-fast-path load_balance should not be limited to the worker's stale topK."""
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)

    inst_a = _make_prefill_instance(1, (10, 11))
    inst_b = _make_prefill_instance(2, (20, 21))
    await instance_manager.refresh_instances(EventType.ADD, [inst_a, inst_b])
    await instance_manager.update_instance_workload(1, 10, Workload(active_tokens=20))
    await instance_manager.update_instance_workload(1, 11, Workload(active_tokens=30))
    await instance_manager.update_instance_workload(2, 20, Workload(active_tokens=1))
    await instance_manager.update_instance_workload(2, 21, Workload(active_tokens=40))

    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    workload_writer = _DummyWorkloadWriter()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        workload_writer=workload_writer,
    )
    request = SchedulerRequest(
        request_type=SchedulerRequestType.ALLOCATE_ONLY,
        request_id="alloc-global",
        data={
            "instance_id": 1,
            "endpoint_id": 10,
            "role": PDRole.ROLE_P.value,
            "req_id": "req-global",
            "workload_sequence": workload_writer.sequence - 2,
            "instance_version": workload_writer.instance_version,
            "workload": Workload(active_tokens=3).model_dump(mode="json"),
        },
    )

    response = await dispatcher.dispatch(request)

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["fast_path"] is False
    assert response.data["instance"]["id"] == 2
    assert response.data["endpoint"]["id"] == 20
    _, selected_workload = await instance_manager.get_endpoint_workload(2, 20)
    _, worker_candidate_workload = await instance_manager.get_endpoint_workload(1, 10)
    assert selected_workload.active_tokens == 4
    assert worker_candidate_workload.active_tokens == 20
    assert workload_writer.writes == [(2, 20)]


@pytest.mark.asyncio
async def test_allocate_only_scans_all_endpoints_when_candidate_policy_is_load_balance():
    """LB candidates should use global scan even under a non-LB scheduler type."""
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.KV_CACHE_AFFINITY
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)

    inst_a = _make_prefill_instance(1, (10, 11), role=PDRole.ROLE_D)
    inst_b = _make_prefill_instance(2, (20, 21), role=PDRole.ROLE_D)
    await instance_manager.refresh_instances(EventType.ADD, [inst_a, inst_b])
    await instance_manager.update_instance_workload(1, 10, Workload(active_tokens=20))
    await instance_manager.update_instance_workload(1, 11, Workload(active_tokens=30))
    await instance_manager.update_instance_workload(2, 20, Workload(active_tokens=1))
    await instance_manager.update_instance_workload(2, 21, Workload(active_tokens=40))

    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    workload_writer = _DummyWorkloadWriter()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        workload_writer=workload_writer,
    )
    request = SchedulerRequest(
        request_type=SchedulerRequestType.ALLOCATE_ONLY,
        request_id="alloc-kv-decode-lb",
        data={
            "instance_id": 1,
            "endpoint_id": 10,
            "role": PDRole.ROLE_D.value,
            "req_id": "req-kv-decode-lb",
            "workload_sequence": workload_writer.sequence - 2,
            "instance_version": workload_writer.instance_version,
            "workload": Workload(active_tokens=3).model_dump(mode="json"),
            "candidate_policy": CANDIDATE_POLICY_LOAD_BALANCE,
        },
    )

    response = await dispatcher.dispatch(request)

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["fast_path"] is False
    assert response.data["instance"]["id"] == 2
    assert response.data["endpoint"]["id"] == 20
    _, selected_workload = await instance_manager.get_endpoint_workload(2, 20)
    _, worker_candidate_workload = await instance_manager.get_endpoint_workload(1, 10)
    assert selected_workload.active_tokens == 4
    assert worker_candidate_workload.active_tokens == 20
    assert workload_writer.writes == [(2, 20)]


@pytest.mark.asyncio
async def test_allocate_only_keeps_affinity_candidate_when_candidate_policy_is_kv_affinity():
    """KV-affinity candidates should not be replaced by a global load-balance scan."""
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.KV_CACHE_AFFINITY
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)

    inst_a = _make_prefill_instance(1, (10, 11))
    inst_b = _make_prefill_instance(2, (20, 21))
    await instance_manager.refresh_instances(EventType.ADD, [inst_a, inst_b])
    await instance_manager.update_instance_workload(1, 10, Workload(active_tokens=20))
    await instance_manager.update_instance_workload(1, 11, Workload(active_tokens=30))
    await instance_manager.update_instance_workload(2, 20, Workload(active_tokens=1))
    await instance_manager.update_instance_workload(2, 21, Workload(active_tokens=40))

    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    workload_writer = _DummyWorkloadWriter()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        workload_writer=workload_writer,
    )
    request = SchedulerRequest(
        request_type=SchedulerRequestType.ALLOCATE_ONLY,
        request_id="alloc-kv-affinity",
        data={
            "instance_id": 1,
            "endpoint_id": 10,
            "role": PDRole.ROLE_P.value,
            "req_id": "req-kv-affinity",
            "workload_sequence": workload_writer.sequence - 2,
            "instance_version": workload_writer.instance_version,
            "workload": Workload(active_tokens=3).model_dump(mode="json"),
            "candidate_policy": CANDIDATE_POLICY_KV_CACHE_AFFINITY,
        },
    )

    response = await dispatcher.dispatch(request)

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["fast_path"] is False
    assert response.data["instance"]["id"] == 1
    assert response.data["endpoint"]["id"] == 10
    _, selected_workload = await instance_manager.get_endpoint_workload(1, 10)
    _, lb_better_workload = await instance_manager.get_endpoint_workload(2, 20)
    assert selected_workload.active_tokens == 23
    assert lb_better_workload.active_tokens == 1
    assert workload_writer.writes == [(1, 10)]


@pytest.mark.asyncio
async def test_allocate_only_affinity_reselects_least_loaded_among_candidates():
    """On a stale-view burst, KV-affinity re-picks the least-loaded *within* the proposed set."""
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.KV_CACHE_AFFINITY
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)

    inst_a = _make_prefill_instance(1, (10, 11))
    inst_b = _make_prefill_instance(2, (20, 21))
    await instance_manager.refresh_instances(EventType.ADD, [inst_a, inst_b])
    # ep11 is the globally least-loaded but is NOT among the proposed candidates.
    await instance_manager.update_instance_workload(1, 10, Workload(active_tokens=20))
    await instance_manager.update_instance_workload(1, 11, Workload(active_tokens=5))
    await instance_manager.update_instance_workload(2, 20, Workload(active_tokens=10))
    await instance_manager.update_instance_workload(2, 21, Workload(active_tokens=40))

    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    workload_writer = _DummyWorkloadWriter()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        workload_writer=workload_writer,
    )
    request = SchedulerRequest(
        request_type=SchedulerRequestType.ALLOCATE_ONLY,
        request_id="alloc-kv-burst",
        data={
            "instance_id": 1,
            "endpoint_id": 10,  # worker top-1 (stale view)
            "candidates": [
                {"instance_id": 1, "endpoint_id": 10},
                {"instance_id": 2, "endpoint_id": 20},
            ],
            "role": PDRole.ROLE_P.value,
            "req_id": "req-kv-burst",
            "workload_sequence": workload_writer.sequence - 2,  # stale -> slow path
            "instance_version": workload_writer.instance_version,
            "workload": Workload(active_tokens=3).model_dump(mode="json"),
            "candidate_policy": CANDIDATE_POLICY_KV_CACHE_AFFINITY,
        },
    )

    response = await dispatcher.dispatch(request)

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["fast_path"] is False
    # Among {ep10=20, ep20=10} the fresh ledger picks ep20; the globally lighter ep11 (5) is
    # NOT chosen because it was not in the proposed affinity set.
    assert response.data["instance"]["id"] == 2
    assert response.data["endpoint"]["id"] == 20
    _, selected_workload = await instance_manager.get_endpoint_workload(2, 20)
    assert selected_workload.active_tokens == 13
    assert workload_writer.writes == [(2, 20)]


@pytest.mark.asyncio
async def test_allocate_only_unknown_candidate_policy_falls_back_to_scheduler_type():
    """Unknown candidate_policy should not silently disable LB's global scan."""
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)

    inst_a = _make_prefill_instance(1, (10, 11))
    inst_b = _make_prefill_instance(2, (20, 21))
    await instance_manager.refresh_instances(EventType.ADD, [inst_a, inst_b])
    await instance_manager.update_instance_workload(1, 10, Workload(active_tokens=20))
    await instance_manager.update_instance_workload(1, 11, Workload(active_tokens=30))
    await instance_manager.update_instance_workload(2, 20, Workload(active_tokens=1))
    await instance_manager.update_instance_workload(2, 21, Workload(active_tokens=40))

    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    workload_writer = _DummyWorkloadWriter()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        workload_writer=workload_writer,
    )
    request = SchedulerRequest(
        request_type=SchedulerRequestType.ALLOCATE_ONLY,
        request_id="alloc-unknown-policy",
        data={
            "instance_id": 1,
            "endpoint_id": 10,
            "role": PDRole.ROLE_P.value,
            "req_id": "req-unknown-policy",
            "workload_sequence": workload_writer.sequence - 2,
            "instance_version": workload_writer.instance_version,
            "workload": Workload(active_tokens=3).model_dump(mode="json"),
            "candidate_policy": "load-balnace",
        },
    )

    response = await dispatcher.dispatch(request)

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["fast_path"] is False
    assert response.data["instance"]["id"] == 2
    assert response.data["endpoint"]["id"] == 20


@pytest.mark.asyncio
async def test_allocate_only_fast_path_accepts_worker_top1_when_sequence_matches():
    """When worker workload sequence matches, SchedulerServer validates and commits top1 directly."""
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)

    inst_a = _make_prefill_instance(1, (10, 11))
    inst_b = _make_prefill_instance(2, (20, 21))
    await instance_manager.refresh_instances(EventType.ADD, [inst_a, inst_b])
    await instance_manager.update_instance_workload(1, 10, Workload(active_tokens=20))
    await instance_manager.update_instance_workload(2, 20, Workload(active_tokens=1))

    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    workload_writer = _DummyWorkloadWriter()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        workload_writer=workload_writer,
    )
    request = SchedulerRequest(
        request_type=SchedulerRequestType.ALLOCATE_ONLY,
        request_id="alloc-fast",
        data={
            "instance_id": 1,
            "endpoint_id": 10,
            "role": PDRole.ROLE_P.value,
            "req_id": "req-fast",
            "workload_sequence": workload_writer.sequence,
            "instance_version": workload_writer.instance_version,
            "workload": Workload(active_tokens=3).model_dump(mode="json"),
        },
    )

    response = await dispatcher.dispatch(request)

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["fast_path"] is True
    assert response.data["instance"]["id"] == 1
    assert response.data["endpoint"]["id"] == 10
    selected_role, selected_workload = await instance_manager.get_endpoint_workload(1, 10)
    _, untouched_workload = await instance_manager.get_endpoint_workload(2, 20)
    assert selected_role == PDRole.ROLE_P
    assert selected_workload.active_tokens == 23
    assert untouched_workload.active_tokens == 1


@pytest.mark.asyncio
async def test_allocate_only_fast_path_accepts_encode_candidate():
    """Fast-path validation must find encode instances stored in the ROLE_E pool."""
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    config.scheduler_config.endpoint_instance_score_weight = 0.0
    instance_manager = InstanceManager(config)

    inst = _make_prefill_instance(1, (10, 11), role=PDRole.ROLE_E)
    await instance_manager.refresh_instances(EventType.ADD, [inst])

    scheduler = Scheduler(instance_provider=instance_manager, config=config)
    workload_writer = _DummyWorkloadWriter()
    dispatcher = _SchedulerRequestDispatcher(
        instance_manager,
        scheduler,
        config,
        workload_writer=workload_writer,
    )
    request = SchedulerRequest(
        request_type=SchedulerRequestType.ALLOCATE_ONLY,
        request_id="alloc-fast-encode",
        data={
            "instance_id": 1,
            "endpoint_id": 10,
            "role": PDRole.ROLE_E.value,
            "req_id": "req-fast-encode",
            "workload_sequence": workload_writer.sequence,
            "instance_version": workload_writer.instance_version,
            "workload": Workload(active_tokens=3).model_dump(mode="json"),
        },
    )

    response = await dispatcher.dispatch(request)

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data["fast_path"] is True
    assert response.data["instance"]["id"] == 1
    assert response.data["endpoint"]["id"] == 10
    selected_role, selected_workload = await instance_manager.get_endpoint_workload(1, 10)
    assert selected_role == PDRole.ROLE_E
    assert selected_workload.active_tokens == 3
    assert workload_writer.writes == [(1, 10)]
