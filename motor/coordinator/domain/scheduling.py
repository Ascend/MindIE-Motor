# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Scheduling facade abstraction: select instance + allocate, update workload.
Router depends only on this interface, not on Scheduler/AsyncSchedulerClient concrete types.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Protocol, Tuple

from pydantic import BaseModel

from motor.common.resources.endpoint import Endpoint, Workload, WorkloadAction
from motor.common.resources.instance import Instance, PDRole
from motor.coordinator.models.request import RequestInfo

# Protocol stub bodies use ellipsis; pylint false positive (W2301).
# pylint: disable=unnecessary-ellipsis


class InstanceReadiness(str, Enum):
    """
    Instance readiness state for deploy mode (e.g. PD separate).
    Callers can distinguish "both P and D", "only P", "only D", "none" for routing/readiness.
    """

    REQUIRED_MET_EPD = "required_met_epd"  # PD: both E, P and D
    ENCODE_PREFILL = "encode_prefill"  # PD: only encode and prefill instances
    REQUIRED_MET = "required_met"  # PD: both P and D; SINGLE_NODE: has hybrid
    ONLY_PREFILL = "only_prefill"  # PD mode: only prefill instances
    ONLY_DECODE = "only_decode"  # PD mode: only decode instances
    ONLY_ENCODE = "only_encode"  # EPD mode: only encode instances
    NONE = "none"  # No required instances
    UNKNOWN = "unknown"  # Unknown deploy mode

    def is_ready(self) -> bool:
        """True if required instances are present for the deploy mode."""
        return self in {InstanceReadiness.REQUIRED_MET, InstanceReadiness.REQUIRED_MET_EPD}

    def is_run(self) -> bool:
        """True indicates that it can run normally."""
        return self.is_ready() or self in {InstanceReadiness.ONLY_PREFILL, InstanceReadiness.ENCODE_PREFILL}


def readiness_from_instances(instances: Iterable[Instance]) -> InstanceReadiness:
    """Infer readiness from currently available instance roles."""
    has_e = False
    has_p = False
    has_d = False
    has_u = False
    for instance in instances:
        role = getattr(instance, "role", None)
        role_value = role.value if hasattr(role, "value") else str(role)
        if role_value == PDRole.ROLE_E.value:
            has_e = True
        elif role_value == PDRole.ROLE_P.value:
            has_p = True
        elif role_value == PDRole.ROLE_D.value:
            has_d = True
        elif role_value in (PDRole.ROLE_U.value, "both", "hybrid"):
            has_u = True

    if has_e and has_p and has_d:
        return InstanceReadiness.REQUIRED_MET_EPD
    if has_p and has_d:
        return InstanceReadiness.REQUIRED_MET
    if has_e and has_p:
        return InstanceReadiness.ENCODE_PREFILL
    if has_p:
        return InstanceReadiness.ONLY_PREFILL
    if has_d:
        return InstanceReadiness.ONLY_DECODE
    if has_e:
        return InstanceReadiness.ONLY_ENCODE
    if has_u:
        return InstanceReadiness.REQUIRED_MET
    return InstanceReadiness.NONE


class ScheduledResource(BaseModel):
    """
    Represents a scheduled resource with an instance and endpoint.
    Output type of scheduling allocation.
    """

    instance: Instance | None = None
    endpoint: Endpoint | None = None


@dataclass(frozen=True)
class ScheduledPair:
    prefill: ScheduledResource
    decode: ScheduledResource
    prefill_workload: Workload
    decode_workload: Workload


@dataclass(frozen=True)
class UpdateWorkloadParams:
    """
    Parameters for update_workload (G.FNM.03: encapsulate many related args).
    """

    instance_id: int
    endpoint_id: int
    role: PDRole | str
    req_id: str
    workload_action: WorkloadAction
    workload_change: Workload


def build_release_workload_params(
    instance_id: int,
    endpoint_id: int,
    role: PDRole,
    req_id: str,
    workload: Workload,
) -> tuple[UpdateWorkloadParams, UpdateWorkloadParams]:
    """Build token and KV releases that compensate one allocation."""
    common = {
        "instance_id": instance_id,
        "endpoint_id": endpoint_id,
        "role": role,
        "req_id": req_id,
    }
    return (
        UpdateWorkloadParams(
            **common,
            workload_action=WorkloadAction.RELEASE_TOKENS,
            workload_change=Workload(active_tokens=-workload.active_tokens),
        ),
        UpdateWorkloadParams(
            **common,
            workload_action=WorkloadAction.RELEASE_KV,
            workload_change=Workload(active_kv_cache=-workload.active_kv_cache),
        ),
    )


class SchedulingFacade(Protocol):
    """
    Scheduling + workload update facade protocol.
    Implemented by Scheduler (in-process) and AsyncSchedulerClient (standalone process); used by BaseRouter for DI.
    Allocation workload is determined by the implementation (e.g. RR uses zero, LoadBalance uses demand).
    """

    async def select_and_allocate(
        self,
        role: PDRole,
        req_info: RequestInfo,
    ) -> Tuple[Instance, Endpoint, Workload] | None:
        """
        Atomic: select instance + one workload allocation (ALLOCATION).
        Returns (instance, endpoint, allocation_workload). Caller records allocation_workload for release.
        """
        ...

    async def select_pair_and_allocate(
        self,
        req_info: RequestInfo,
    ) -> ScheduledPair | None:
        """Select and allocate a P/D pair as one scheduling operation."""
        ...

    async def update_workload(self, params: UpdateWorkloadParams) -> bool:
        """Update workload (ALLOCATION / RELEASE_KV / RELEASE_TOKENS)."""
        ...

    async def has_required_instances(self) -> InstanceReadiness:
        """
        Check by deploy mode; returns detailed state (REQUIRED_MET, ONLY_PREFILL, ONLY_DECODE, NONE, UNKNOWN).
        Use .is_ready() for boolean, or compare to enum for routing/readiness.
        """
        ...

    async def get_available_instances(self, role: PDRole | None = None) -> dict[int, Instance]:
        """Return available instances for role; role=None means all roles."""
        ...
