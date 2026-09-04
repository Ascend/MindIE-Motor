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
from typing import Any, Iterable, Protocol

from pydantic import BaseModel

from motor.common.resources.dispatch import is_decode_colocation_instance
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

    def is_run(self, *, allow_decode_only: bool = False) -> bool:
        """Return whether this topology can accept inference traffic.

        A decode-only topology is routable only when PD-separation fallback can
        co-locate prefill on a decode instance.  Keep that policy explicit at
        the caller so deployments with fallback disabled do not become ready.
        """
        runnable = {InstanceReadiness.ONLY_PREFILL, InstanceReadiness.ENCODE_PREFILL}
        if allow_decode_only:
            runnable.add(InstanceReadiness.ONLY_DECODE)
        return self.is_ready() or self in runnable


def readiness_from_instances(instances: Iterable[Instance]) -> InstanceReadiness:
    """Infer readiness from the available native-engine roles."""
    encode_instances = []
    prefill_instances = []
    decode_instances = []
    union_instances = []
    for instance in instances:
        role = getattr(instance, "role", None)
        role_value = role.value if hasattr(role, "value") else str(role)
        if role_value == PDRole.ROLE_E.value:
            encode_instances.append(instance)
        elif role_value == PDRole.ROLE_P.value:
            prefill_instances.append(instance)
        elif role_value == PDRole.ROLE_D.value:
            decode_instances.append(instance)
        elif role_value in (PDRole.ROLE_U.value, "both", "hybrid"):
            union_instances.append(instance)

    if prefill_instances and decode_instances:
        return InstanceReadiness.REQUIRED_MET_EPD if encode_instances else InstanceReadiness.REQUIRED_MET
    if union_instances:
        return InstanceReadiness.REQUIRED_MET
    if encode_instances and prefill_instances:
        return InstanceReadiness.ENCODE_PREFILL
    if prefill_instances:
        return InstanceReadiness.ONLY_PREFILL
    if decode_instances:
        return InstanceReadiness.ONLY_DECODE
    if encode_instances:
        return InstanceReadiness.ONLY_ENCODE
    return InstanceReadiness.NONE


async def get_decode_colocation_candidate_ids(scheduler: Any) -> tuple[int, ...]:
    """Return eligible unblocked Decode IDs in deterministic order."""
    get_unblocked = getattr(scheduler, "get_unblocked_instances", None)
    get_local = getattr(scheduler, "get_local_instances", None)
    if get_unblocked is None or get_local is None:
        return ()

    unblocked_ids = set(await get_unblocked(PDRole.ROLE_D))
    if not unblocked_ids:
        return ()

    instances = await get_local(PDRole.ROLE_D)
    return tuple(
        sorted(
            instance_id
            for instance_id, instance in instances.items()
            if instance_id in unblocked_ids and is_decode_colocation_instance(instance)
        )
    )


async def has_decode_colocation_candidate(scheduler: Any) -> bool:
    """Return whether an unblocked Decode instance can execute a bare local request."""
    return bool(await get_decode_colocation_candidate_ids(scheduler))


class ScheduledResource(BaseModel):
    """
    Represents a scheduled resource with an instance and endpoint.
    Output type of scheduling allocation.
    """

    instance: Instance | None = None
    endpoint: Endpoint | None = None


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
    operation_id: str | None = None


class SchedulingFacade(Protocol):
    """
    Scheduling + workload update facade protocol.
    Implemented by AsyncSchedulerClient (Infer Worker); used by BaseRouter for DI.
    Allocation workload is determined by the implementation (e.g. RR uses zero, LoadBalance uses demand).
    """

    async def select_and_allocate(
        self,
        role: PDRole,
        req_info: RequestInfo,
        *,
        target_instance_id: int | None = None,
        required_engine_type: str | None = None,
        required_dispatch_capability: str | None = None,
    ) -> tuple[Instance, Endpoint, Workload] | None:
        """
        Atomic: select instance + one workload allocation (ALLOCATION).
        When target_instance_id is set, pin to that instance (skip policy selection).
        Engine type and dispatch capability constraints filter eligible instances before selection.
        Returns (instance, endpoint, allocation_workload). Caller records allocation_workload for release.
        """
        ...

    async def update_workload(self, params: UpdateWorkloadParams) -> bool:
        """Update workload (ALLOCATION / RELEASE_TOKENS)."""
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

    async def get_available_instance_roles(self) -> set[PDRole]:
        """Return roles currently present in the scheduler's local instance view."""
        ...

    async def report_cb_event(self, instance_id: int, event: str) -> None:
        """Report a circuit-breaker event ("failure" | "success") for an instance.

        Implementations that run alongside SchedulerServer forward the event via
        ZMQ; in-process implementations may be a no-op.
        """
        ...

    async def get_unblocked_instances(self, role: PDRole) -> list[int]:
        """Return instance IDs of the given role that are NOT blocked by circuit breaker."""
        ...

    async def get_local_instances(self, role: PDRole | None = None) -> dict[int, Instance]:
        """Return the local instance view; warm-up only when the cache is empty."""
        ...
