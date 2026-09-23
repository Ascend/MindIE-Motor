# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Runtime state and priority arbitration for DP fault scale-down."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from motor.common.logger import get_logger
from motor.controller.fault_tolerance.fault_types import parse_npu_chip_ids


logger = get_logger(__name__)


class FtPhase(str, Enum):
    """Externally visible instance-level fault-tolerance phases."""

    DISABLED = "DISABLED"
    NORMAL = "NORMAL"
    DECIDING = "DECIDING"
    WAITING_ENGINE_FAULT = "WAITING_ENGINE_FAULT"
    SCALING_DOWN = "SCALING_DOWN"
    POST_SCALE_CLEANUP = "POST_SCALE_CLEANUP"
    SCALED_DOWN_RUNNING = "SCALED_DOWN_RUNNING"
    RECONFIGURING = "RECONFIGURING"


@dataclass
class GateSnapshot:
    """Auditable DP scale-down admission result."""

    allowed: bool = False
    reason_codes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScaleDownContext:
    """Immutable rank candidates and fallback provenance for one scale-down attempt."""

    pending_removed_ranks: tuple[int, ...] = ()
    source: str = "software"
    fallback_strategy: str = ""
    fault_keys: tuple[str, ...] = ()
    all_dp_unhealthy: bool = False
    collection_complete: bool = False
    collection_timed_out: bool = False
    engine_fault_observed: bool = False
    all_dp_removed: bool = False
    hardware_fault_observed: bool = False
    hardware_wait_timed_out: bool = False
    hardware_affected_ranks: tuple[int, ...] = ()
    hardware_ft_observed: bool = False
    already_removed_fault_keys: tuple[str, ...] = ()
    engine_recovery_eligible: bool = True
    hardware_mapping_complete: bool = True


class FaultNormalizer:
    """Normalize fault-manager evidence into original DP-rank coordinates."""

    @staticmethod
    def hardware_fault_affects_instance(instance: Any, pod_ip: str, fault: Any) -> bool:
        """Return whether node hardware evidence belongs to this instance."""
        npu_name = getattr(fault, "npu_name", "")
        if not npu_name:
            return True
        device_ids = parse_npu_chip_ids(npu_name)
        if not device_ids:
            return True

        endpoints_by_pod = getattr(instance, "endpoints", None)
        if not isinstance(endpoints_by_pod, dict):
            return True
        pod_endpoints = endpoints_by_pod.get(pod_ip)
        if not isinstance(pod_endpoints, dict):
            return True
        owned_device_ids = {
            int(str(device.device_id))
            for endpoint in pod_endpoints.values()
            for device in endpoint.device_infos
            if str(device.device_id).isdigit()
        }
        # Preserve conservative node-level behavior for legacy registrations
        # that do not carry endpoint device ownership.
        return not owned_device_ids or bool(device_ids & owned_device_ids)

    @staticmethod
    def software_dp_ranks(faults: list[Any]) -> set[int]:
        """Return definitively dead engine ranks without interpreting engine Mask."""
        dead_ranks = set()
        for fault in faults:
            engine_id = getattr(fault, "engine_id", None)
            engine_status = getattr(fault, "engine_status", None)
            if not isinstance(engine_id, int):
                continue
            if engine_status == 1:
                dead_ranks.add(engine_id)
        # UNHEALTHY identifies an apply-capable engine, not the failed rank:
        # peer survivors can all become UNHEALTHY after a collective failure.
        return dead_ranks

    @staticmethod
    def hardware_dp_ranks(instance: Any, faults: list[tuple[str, str, Any]]) -> tuple[set[int], set[str]]:
        """Map MindCluster node/device evidence to Motor-owned DP endpoint topology."""
        endpoints_by_pod = getattr(instance, "endpoints", None)
        if not isinstance(endpoints_by_pod, dict):
            return set(), set()

        candidates: set[int] = set()
        mapped_fault_keys: set[str] = set()
        for fault_key, pod_ip, fault in faults:
            pod_endpoints = endpoints_by_pod.get(pod_ip)
            if not isinstance(pod_endpoints, dict) or not pod_endpoints:
                continue

            npu_name = getattr(fault, "npu_name", "")
            if not npu_name:
                candidates.update(endpoint.id for endpoint in pod_endpoints.values())
                mapped_fault_keys.add(fault_key)
                continue

            device_ids = parse_npu_chip_ids(npu_name)
            if not device_ids:
                continue
            matched = False
            for endpoint in pod_endpoints.values():
                if any(
                    str(device.device_id).isdigit() and int(str(device.device_id)) in device_ids
                    for device in endpoint.device_infos
                ):
                    candidates.add(endpoint.id)
                    matched = True
            if matched:
                mapped_fault_keys.add(fault_key)

        return candidates, mapped_fault_keys


@dataclass
class FtRuntime:
    """Persistable shape of one instance's DP scale-down runtime."""

    instance_id: int
    phase: FtPhase = FtPhase.NORMAL
    original_dp_ranks: list[int] = field(default_factory=list)
    request_id: str = ""
    dead_committed: list[int] = field(default_factory=list)
    pending_removed_ranks: list[int] = field(default_factory=list)
    fallback_strategy: str = ""
    gate: GateSnapshot = field(default_factory=GateSnapshot)
    engine_statuses: dict[int, dict[str, Any]] = field(default_factory=dict)
    serving_published: bool = True
    reconfiguration_dispatched: bool = False
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        unavailable = set(self.dead_committed) | set(self.pending_removed_ranks)
        result["alive_dp_ranks"] = sorted(set(self.original_dp_ranks) - unavailable)
        result["can_serve"] = (
            self.phase
            in {
                FtPhase.NORMAL,
                FtPhase.SCALED_DOWN_RUNNING,
            }
            and self.serving_published
        )
        return result


class FtRuntimeStore:
    """Thread-safe runtime store with minimal durable recovery state."""

    _DURABLE_FIELDS = {
        "phase",
        "original_dp_ranks",
        "dead_committed",
        "fallback_strategy",
        "reconfiguration_dispatched",
    }
    _COMMITTED_PHASES = {FtPhase.POST_SCALE_CLEANUP, FtPhase.SCALED_DOWN_RUNNING}
    _INCOMPLETE_PHASES = {
        FtPhase.DECIDING,
        FtPhase.WAITING_ENGINE_FAULT,
        FtPhase.SCALING_DOWN,
        FtPhase.RECONFIGURING,
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instances: dict[int, FtRuntime] = {}
        self._persist_callback: Callable[[], bool] | None = None

    def set_persist_callback(self, callback: Callable[[], bool] | None) -> None:
        self._persist_callback = callback

    def get_or_create(self, instance_id: int) -> FtRuntime:
        with self._lock:
            runtime = self._instances.setdefault(instance_id, FtRuntime(instance_id=instance_id))
            return copy.deepcopy(runtime)

    def put(self, runtime: FtRuntime) -> None:
        with self._lock:
            self._instances[runtime.instance_id] = copy.deepcopy(runtime)

    def transition(self, instance_id: int, **changes: Any) -> FtRuntime:
        """Atomically update selected runtime fields and return a detached snapshot."""
        durable_changed = False
        with self._lock:
            runtime = self._instances.setdefault(instance_id, FtRuntime(instance_id=instance_id))
            for name, value in changes.items():
                if not hasattr(runtime, name):
                    raise ValueError("unknown FT runtime field: %s" % name)
                value = copy.deepcopy(value)
                durable_changed = durable_changed or (name in self._DURABLE_FIELDS and getattr(runtime, name) != value)
                setattr(runtime, name, value)
            snapshot = copy.deepcopy(runtime)
        if durable_changed and self._persist_callback is not None:
            self._persist_callback()
        return snapshot

    def get(self, instance_id: int) -> dict[str, Any] | None:
        with self._lock:
            runtime = self._instances.get(instance_id)
            return copy.deepcopy(runtime.to_dict()) if runtime is not None else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(self._instances[key].to_dict()) for key in sorted(self._instances)]

    def remove(self, instance_id: int) -> bool:
        """Remove one obsolete instance runtime and persist durable deletion."""
        persist_callback: Callable[[], bool] | None = None
        with self._lock:
            runtime = self._instances.pop(instance_id, None)
            if runtime is not None and (
                runtime.phase not in {FtPhase.DISABLED, FtPhase.NORMAL} or runtime.dead_committed
            ):
                persist_callback = self._persist_callback
        if persist_callback is not None:
            persist_callback()
        return runtime is not None

    def persistent_data(self) -> dict[str, Any]:
        """Return only fields required to restore committed topology or fail an interrupted attempt."""
        with self._lock:
            return {
                str(instance_id): {
                    "instance_id": instance_id,
                    "phase": runtime.phase.value,
                    "original_dp_ranks": list(runtime.original_dp_ranks),
                    "dead_committed": list(runtime.dead_committed),
                    "fallback_strategy": runtime.fallback_strategy,
                    "reconfiguration_dispatched": runtime.reconfiguration_dispatched,
                }
                for instance_id, runtime in self._instances.items()
                if runtime.phase not in {FtPhase.DISABLED, FtPhase.NORMAL} or runtime.dead_committed
            }

    def restore_persistent_data(self, data: dict[str, Any]) -> dict[int, str]:
        """Restore committed views and return interrupted attempts that must use their fallback."""
        if not isinstance(data, dict):
            return {}
        restored: dict[int, FtRuntime] = {}
        interrupted: dict[int, str] = {}
        for raw_id, raw in data.items():
            if not isinstance(raw, dict):
                continue
            try:
                instance_id = int(raw.get("instance_id", raw_id))
                phase = FtPhase(raw.get("phase", FtPhase.NORMAL))
                original_dp_ranks = self._restore_ranks(raw.get("original_dp_ranks", []))
                dead_committed = self._restore_ranks(raw.get("dead_committed", []))
                fallback = str(raw.get("fallback_strategy", ""))
                reconfiguration_dispatched = raw.get("reconfiguration_dispatched") is True
                if phase in self._COMMITTED_PHASES:
                    restored_phase = FtPhase.SCALED_DOWN_RUNNING
                    error = None
                elif phase == FtPhase.RECONFIGURING and reconfiguration_dispatched:
                    restored_phase = FtPhase.RECONFIGURING
                    error = None
                elif phase in self._INCOMPLETE_PHASES:
                    restored_phase = FtPhase.RECONFIGURING
                    fallback = fallback or "InstanceReconfigurationStrategy"
                    interrupted[instance_id] = fallback
                    error = "controller restarted during DP scale-down; original fallback required"
                else:
                    restored_phase = phase
                    error = None
                restored[instance_id] = FtRuntime(
                    instance_id=instance_id,
                    phase=restored_phase,
                    original_dp_ranks=original_dp_ranks,
                    dead_committed=dead_committed,
                    fallback_strategy=fallback,
                    reconfiguration_dispatched=reconfiguration_dispatched,
                    serving_published=not dead_committed and restored_phase == FtPhase.NORMAL,
                    last_error=error,
                )
            except (TypeError, ValueError, AttributeError) as error:
                logger.warning("Ignoring invalid persisted FT runtime %s: %s", raw_id, error)
        with self._lock:
            self._instances = restored
        return interrupted

    @staticmethod
    def _restore_ranks(raw: Any) -> list[int]:
        if not isinstance(raw, list) or any(
            not isinstance(rank, int) or isinstance(rank, bool) or rank < 0 for rank in raw
        ):
            raise ValueError("DP ranks must be a list of non-negative integers")
        return sorted(set(raw))

    def mark_serving_published(self, instance_ids: set[int]) -> None:
        """Atomically converge serving publication without overwriting a newer FT phase."""
        with self._lock:
            for instance_id in instance_ids:
                runtime = self._instances.get(instance_id)
                if runtime is None or runtime.phase not in {
                    FtPhase.NORMAL,
                    FtPhase.SCALED_DOWN_RUNNING,
                }:
                    continue
                runtime.serving_published = True
                if runtime.last_error:
                    errors = [
                        error
                        for error in runtime.last_error.split("; ")
                        if not error.startswith("ServingOverlay publication pending")
                    ]
                    runtime.last_error = "; ".join(errors) or None

    def prepare_after_engine_relaunch(self, instance_id: int) -> None:
        """Restore the last committed topology and wait for Coordinator republication."""
        with self._lock:
            runtime = self._instances.get(instance_id)
            if runtime is None or runtime.phase != FtPhase.RECONFIGURING:
                return
            runtime.phase = FtPhase.SCALED_DOWN_RUNNING if runtime.dead_committed else FtPhase.NORMAL
            runtime.pending_removed_ranks = []
            runtime.serving_published = False
            runtime.last_error = None

    def resume_after_fault_clear(self, instance_id: int) -> FtRuntime | None:
        """Restore the last committed serving topology after evidence self-clears."""
        with self._lock:
            runtime = self._instances.get(instance_id)
            if runtime is None or runtime.phase != FtPhase.WAITING_ENGINE_FAULT:
                return None
            runtime.phase = FtPhase.SCALED_DOWN_RUNNING if runtime.dead_committed else FtPhase.NORMAL
            runtime.pending_removed_ranks = []
            runtime.engine_statuses = {}
            runtime.serving_published = False
            runtime.last_error = None
            return copy.deepcopy(runtime)

    def mark_instance_reconfiguration_dispatched(self, instance_id: int) -> None:
        """Close scale-down history after recovery dispatch without declaring the old instance healthy."""
        self.transition(
            instance_id,
            phase=FtPhase.RECONFIGURING,
            original_dp_ranks=[],
            request_id="",
            dead_committed=[],
            pending_removed_ranks=[],
            gate=GateSnapshot(),
            engine_statuses={},
            serving_published=False,
            reconfiguration_dispatched=True,
            last_error=None,
        )

    def clear(self) -> None:
        with self._lock:
            self._instances.clear()


class TopologyResolver:
    """Resolve stable original DP ranks from the Instance endpoint topology."""

    @staticmethod
    def original_dp_ranks(instance: Any) -> list[int]:
        """Return and validate the immutable external-LB DP rank coordinate space."""
        parallel_config = getattr(instance, "parallel_config", None)
        dp_size = getattr(parallel_config, "dp_size", 0)
        if not isinstance(dp_size, int) or dp_size <= 0:
            raise ValueError("instance DP size is unavailable")
        endpoints = instance.get_all_endpoints(include_headless=False)
        ranks = sorted({endpoint.id for endpoint in endpoints})
        if ranks != list(range(dp_size)):
            raise ValueError("routable endpoint ids do not match original DP ranks: %s" % ranks)
        return ranks

    @staticmethod
    def pod_dp_ranks(instance: Any) -> dict[str, set[int]]:
        """Return the original DP ranks hosted by each Pod IP."""
        endpoints = getattr(instance, "endpoints", None)
        if not isinstance(endpoints, dict):
            raise ValueError("instance endpoint topology is unavailable")

        original_ranks = set(TopologyResolver.original_dp_ranks(instance))
        result: dict[str, set[int]] = {}
        for pod_ip, pod_endpoints in endpoints.items():
            if not isinstance(pod_ip, str) or not isinstance(pod_endpoints, dict):
                raise ValueError("instance endpoint topology is malformed")
            ranks = {endpoint.id for endpoint in pod_endpoints.values() if endpoint.id in original_ranks}
            if ranks:
                result[pod_ip] = ranks
        return result

    @classmethod
    def recyclable_pods(cls, instance: Any, dead_committed: set[int]) -> list[str]:
        """Return Pods whose complete original DP set has been removed."""
        return sorted(
            pod_ip for pod_ip, pod_ranks in cls.pod_dp_ranks(instance).items() if pod_ranks.issubset(dead_committed)
        )


_RUNTIME_STORE = FtRuntimeStore()


def get_ft_runtime_store() -> FtRuntimeStore:
    return _RUNTIME_STORE


def committed_dp_ranks(instance_id: int) -> set[int]:
    """Return ranks already committed by successful scale-down rounds."""
    runtime = _RUNTIME_STORE.get(instance_id)
    return set(runtime["dead_committed"]) if runtime is not None else set()


_PARTIAL_LOSS_TRANSACTION_PHASES = {
    FtPhase.DECIDING.value,
    FtPhase.WAITING_ENGINE_FAULT.value,
    FtPhase.SCALING_DOWN.value,
    FtPhase.POST_SCALE_CLEANUP.value,
}


@dataclass(frozen=True)
class PartialLossPolicy:
    """How legacy partial-loss handling yields to the current DP runtime."""

    protect_transaction: bool = False
    ignored_dead_ranks: frozenset[int] = frozenset()
    suppress_legacy_shutdown: bool = False


def partial_loss_policy(instance_id: int) -> PartialLossPolicy:
    """Return one coherent partial-loss policy from a single runtime snapshot."""
    runtime = _RUNTIME_STORE.get(instance_id)
    if runtime is None:
        return PartialLossPolicy()
    phase = runtime["phase"]
    if phase in _PARTIAL_LOSS_TRANSACTION_PHASES:
        return PartialLossPolicy(protect_transaction=True, suppress_legacy_shutdown=True)
    if phase == FtPhase.SCALED_DOWN_RUNNING.value:
        return PartialLossPolicy(
            ignored_dead_ranks=frozenset(runtime["dead_committed"]),
            suppress_legacy_shutdown=True,
        )
    return PartialLossPolicy()
