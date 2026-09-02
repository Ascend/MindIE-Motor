# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.

"""Tests for topology readiness policy."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from motor.config.coordinator import CoordinatorConfig
from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources.instance import Instance, PDRole
from motor.coordinator.api_server.inference_server import InferenceServer
from motor.coordinator.domain.probe import ReadinessProbe, ReadinessResult, RoleHeartbeatResult
from motor.coordinator.domain.scheduling import InstanceReadiness


def test_decode_only_requires_explicit_fallback_capability():
    """Decode-only must not bypass the gate unless co-location is enabled."""
    readiness = InstanceReadiness.ONLY_DECODE

    assert readiness.is_run() is False
    assert readiness.is_run(allow_decode_only=False) is False
    assert readiness.is_run(allow_decode_only=True) is True


def test_existing_runnable_topologies_are_unchanged():
    """The new capability flag must not alter established readiness states."""
    assert InstanceReadiness.REQUIRED_MET.is_run() is True
    assert InstanceReadiness.ONLY_PREFILL.is_run() is True
    assert InstanceReadiness.ENCODE_PREFILL.is_run() is True
    assert InstanceReadiness.NONE.is_run(allow_decode_only=True) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback_enabled", "engine_type", "capabilities", "expected"),
    [
        (False, "vllm", [DispatchPlan.DECODE_COLOCATION.value], False),
        (True, "sglang", [DispatchPlan.DECODE_COLOCATION.value], False),
        (True, "vllm", [], False),
        (True, "vllm", [DispatchPlan.DECODE_COLOCATION.value], True),
    ],
)
async def test_inference_gate_requires_eligible_decode_candidate(fallback_enabled, engine_type, capabilities, expected):
    """Decode-only inference readiness must use the same eligibility gate as routing."""
    server = object.__new__(InferenceServer)
    server.coordinator_config = CoordinatorConfig()
    server.coordinator_config.scheduler_config.enable_pd_separation_fallback_to_hybrid = fallback_enabled
    scheduler_client = MagicMock()
    scheduler_client.has_required_instances = AsyncMock(return_value=InstanceReadiness.ONLY_DECODE)
    decode = Instance(
        job_name="decode",
        model_name="model",
        id=1,
        role=PDRole.ROLE_D,
        engine_type=engine_type,
        dispatch_capabilities=capabilities,
    )
    scheduler_client.get_unblocked_instances = AsyncMock(return_value=[decode.id])
    scheduler_client.get_local_instances = AsyncMock(return_value={decode.id: decode})
    server._scheduler_connection = MagicMock()
    server._scheduler_connection.get_client.return_value = scheduler_client

    assert await server._is_available() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_decode_only", "engine_type", "capabilities", "expected"),
    [
        (False, "vllm", [DispatchPlan.DECODE_COLOCATION.value], False),
        (True, "sglang", [DispatchPlan.DECODE_COLOCATION.value], False),
        (True, "vllm", [], False),
        (True, "vllm", [DispatchPlan.DECODE_COLOCATION.value], True),
    ],
)
async def test_management_readiness_requires_eligible_decode_candidate(
    allow_decode_only, engine_type, capabilities, expected
):
    """Management readiness must reject Decode instances that routing cannot serve."""
    daemon = MagicMock()
    daemon.read_role_and_heartbeat.return_value = RoleHeartbeatResult(
        is_master=True,
        heartbeat_stale=False,
        orphaned=False,
    )
    instance_manager = MagicMock()
    instance_manager.get_required_instances_status.return_value = InstanceReadiness.ONLY_DECODE
    decode = Instance(
        job_name="decode",
        model_name="model",
        id=1,
        role=PDRole.ROLE_D,
        engine_type=engine_type,
        dispatch_capabilities=capabilities,
    )
    instance_manager.get_available_instances.return_value = {decode.id: decode}
    probe = ReadinessProbe(
        daemon,
        instance_manager,
        enable_master_standby=False,
        allow_decode_only=allow_decode_only,
    )

    result = await probe.check()

    assert result.result == ReadinessResult.OK_STANDBY
    assert result.is_ready is expected


@pytest.mark.asyncio
async def test_management_readiness_applies_hot_reloaded_fallback_policy():
    """Changing the runtime fallback flag must immediately update readiness."""
    daemon = MagicMock()
    daemon.read_role_and_heartbeat.return_value = RoleHeartbeatResult(
        is_master=True,
        heartbeat_stale=False,
        orphaned=False,
    )
    instance_manager = MagicMock()
    instance_manager.get_required_instances_status.return_value = InstanceReadiness.ONLY_DECODE
    decode = Instance(
        job_name="decode",
        model_name="model",
        id=1,
        role=PDRole.ROLE_D,
        engine_type="vllm",
        dispatch_capabilities=[DispatchPlan.DECODE_COLOCATION.value],
    )
    instance_manager.get_available_instances.return_value = {decode.id: decode}
    probe = ReadinessProbe(daemon, instance_manager, enable_master_standby=False)

    assert (await probe.check()).is_ready is False

    probe.allow_decode_only = True

    assert (await probe.check()).is_ready is True
