# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

"""End-to-end tests: SchedulingConstraint → Router → Scheduler target_instance_id."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from motor.common.resources.endpoint import Endpoint, Workload
from motor.common.resources.instance import Instance, InsStatus, ParallelConfig, PDRole
from motor.config.coordinator import SchedulerType
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.domain.scheduling_constraint import SchedulingConstraint
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.router.strategies.pd_hybrid import PDHybridRouter
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter


def _make_instance(instance_id: int, role: PDRole) -> Instance:
    inst = Instance(
        job_name=f"test-job-{instance_id}",
        model_name=f"test-model-{instance_id}",
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1, tp_size=1),
        endpoints={},
    )
    ep = Endpoint(id=instance_id, ip="127.0.0.1", business_port=str(8000 + instance_id), mgmt_port="8000")
    inst.endpoints = {"127.0.0.1": {instance_id: ep}}
    return inst


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    cfg.scheduler_config.endpoint_instance_score_weight = 1.0
    cfg.scheduler_config.kv_affinity_load_weight = 0.5
    cfg.exception_config.max_retry = 1
    cfg.exception_config.retry_delay = 0.0001
    cfg.exception_config.reschedule_enabled = False
    cfg.token_sampling_config.precision_check_enabled = True
    cfg.token_sampling_config.interval_seconds = 30.0
    cfg.token_sampling_config.logprobs_count = 1
    cfg.token_sampling_config.precision_issue_threshold = 3
    cfg.token_sampling_config.probe_max_attempts = 2
    cfg.token_sampling_config.probe_timeout_seconds = 600.0
    return cfg


def _make_req_info(constraint: SchedulingConstraint | None = None) -> RequestInfo:
    req_data = {"model": "test", "messages": [{"role": "user", "content": "hi"}], "stream": True}
    return RequestInfo(
        req_id="e2e-test-1",
        req_data=req_data,
        api="v1/chat/completions",
        req_len=50,
        client_expects_chat_shape=True,
        scheduling_constraint=constraint,
    )


class TestConstraintToTargetInstanceId:
    """Verify SchedulingConstraint flows through to select_and_allocate target_instance_id."""

    @pytest.mark.asyncio
    async def test_hybrid_router_passes_target_instance_id(self) -> None:
        """PDHybridRouter (SINGLE_NODE): prepare_resource passes target_instance_id per role."""
        constraint = SchedulingConstraint.for_precision_probe(p_instance_id=1, d_instance_id=5)
        req_info = _make_req_info(constraint)
        config = _make_config()

        inst_p = _make_instance(1, PDRole.ROLE_P)
        ep_p = list(inst_p.endpoints["127.0.0.1"].values())[0]

        captured_targets: dict[str, int | None] = {}

        async def mock_select_and_allocate(role, req_info, *, target_instance_id=None):
            role_str = role.value if hasattr(role, "value") else str(role)
            captured_targets[role_str] = target_instance_id
            return inst_p, ep_p, Workload()

        scheduler = MagicMock()
        scheduler.select_and_allocate = mock_select_and_allocate
        scheduler.update_workload = AsyncMock(return_value=True)
        scheduler.get_available_instances = AsyncMock(return_value={1: inst_p})

        rm = MagicMock(spec=RequestManager)
        rm.add_req_workload = AsyncMock(return_value=True)

        router = PDHybridRouter(
            req_info=req_info,
            config=config,
            scheduler=scheduler,
            request_manager=rm,
        )

        resource = await router.prepare_resource(PDRole.ROLE_P)
        assert resource is not None
        assert captured_targets.get("prefill") == 1

    @pytest.mark.asyncio
    async def test_unified_router_prepare_attempt_passes_target_instance_id(self) -> None:
        """UnifiedPDRouter._prepare_attempt_resource extracts target_instance_id from constraint."""
        constraint = SchedulingConstraint.for_precision_probe(p_instance_id=10, d_instance_id=20)
        req_info = _make_req_info(constraint)
        config = _make_config()

        inst_p = _make_instance(10, PDRole.ROLE_P)
        inst_d = _make_instance(20, PDRole.ROLE_D)
        ep_p = list(inst_p.endpoints["127.0.0.1"].values())[0]
        ep_d = list(inst_d.endpoints["127.0.0.1"].values())[0]

        captured_targets: dict[str, int | None] = {}

        async def mock_select_and_allocate(role, req_info, *, target_instance_id=None):
            role_str = role.value if hasattr(role, "value") else str(role)
            captured_targets[role_str] = target_instance_id
            inst = inst_p if role_str == "prefill" else inst_d
            ep = ep_p if role_str == "prefill" else ep_d
            return inst, ep, Workload()

        scheduler = MagicMock()
        scheduler.select_and_allocate = mock_select_and_allocate
        scheduler.update_workload = AsyncMock(return_value=True)

        rm = MagicMock(spec=RequestManager)
        rm.add_req_attempt_workload = AsyncMock(return_value=True)
        rm.generate_request_id = AsyncMock(return_value="e2e-test-1")

        router = UnifiedPDRouter(
            req_info=req_info,
            config=config,
            scheduler=scheduler,
            request_manager=rm,
        )

        resource_p = await router._prepare_attempt_resource(PDRole.ROLE_P, attempt_seq=1)
        assert resource_p is not None
        assert captured_targets.get("prefill") == 10

        resource_d = await router._prepare_attempt_resource(PDRole.ROLE_D, attempt_seq=1)
        assert resource_d is not None
        assert captured_targets.get("decode") == 20

    @pytest.mark.asyncio
    async def test_no_constraint_passes_none_target(self) -> None:
        """When scheduling_constraint is None, target_instance_id must be None."""
        req_info = _make_req_info(constraint=None)
        config = _make_config()

        inst = _make_instance(1, PDRole.ROLE_P)
        ep = list(inst.endpoints["127.0.0.1"].values())[0]

        captured_target = None

        async def mock_select_and_allocate(role, req_info, *, target_instance_id=None):
            nonlocal captured_target
            captured_target = target_instance_id
            return inst, ep, Workload()

        scheduler = MagicMock()
        scheduler.select_and_allocate = mock_select_and_allocate
        scheduler.update_workload = AsyncMock(return_value=True)

        rm = MagicMock(spec=RequestManager)
        rm.add_req_attempt_workload = AsyncMock(return_value=True)

        router = UnifiedPDRouter(
            req_info=req_info,
            config=config,
            scheduler=scheduler,
            request_manager=rm,
        )

        await router._prepare_attempt_resource(PDRole.ROLE_D, attempt_seq=1)
        assert captured_target is None
