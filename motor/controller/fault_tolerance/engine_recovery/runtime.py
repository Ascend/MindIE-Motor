# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Shared instance fault-tolerance runtime, ownership and committed topology."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from motor.common.logger import get_logger

logger = get_logger(__name__)


class FtPhase(str, Enum):
    """Externally visible instance-level fault-tolerance phases."""

    DISABLED = "DISABLED"
    NORMAL = "NORMAL"
    DECIDING = "DECIDING"
    RELAUNCHING = "RELAUNCHING"
    WAITING_ENGINE_FAULT = "WAITING_ENGINE_FAULT"
    FAST_RECOVERING = "FAST_RECOVERING"
    SCALING_DOWN = "SCALING_DOWN"
    POST_SCALE_CLEANUP = "POST_SCALE_CLEANUP"
    SCALED_DOWN_RUNNING = "SCALED_DOWN_RUNNING"
    WARMING_UP = "WARMING_UP"
    RECONFIGURING = "RECONFIGURING"


@dataclass
class FtRuntime:
    """Persistable shape of one instance's DP scale-down runtime."""

    instance_id: int
    phase: FtPhase = FtPhase.NORMAL
    recovery_id: str = ""
    action: str = ""
    stage: str = "idle"
    original_dp_ranks: list[int] = field(default_factory=list)
    request_id: str = ""
    dead_committed: list[int] = field(default_factory=list)
    # Ranks still advertised to the Coordinator while phase is WARMING_UP.
    # Recorded from the survivor roster at warmup entry. Empty means no filter.
    serving_dp_ranks: list[int] = field(default_factory=list)
    pending_removed_ranks: list[int] = field(default_factory=list)
    fallback_strategy: str = ""
    recovery_paused: bool = False
    serving_published: bool = True
    preserve_recovery_streams: bool = False
    reconfiguration_dispatched: bool = False
    last_error: str | None = None
    fallback_reason: str | None = None
    # Elastic-EP DP master elected at scale-down. Scale-up/joiners reuse it
    # until the next scale-down or a whole-instance restart clears it.
    dp_master_rank: int | None = None
    dp_master_ip: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        unavailable = set(self.dead_committed) | set(self.pending_removed_ranks)
        result["alive_dp_ranks"] = sorted(set(self.original_dp_ranks) - unavailable)
        result["can_serve"] = (
            self.phase
            in {
                FtPhase.NORMAL,
                FtPhase.SCALED_DOWN_RUNNING,
                FtPhase.WARMING_UP,
            }
            and self.serving_published
            and not self.recovery_paused
        )
        return result


class StaleRecoveryOwner(RuntimeError):
    """The runtime was removed or transferred to another recovery owner."""


class FtRuntimeStore:
    """Thread-safe runtime store with minimal durable recovery state."""

    _DURABLE_FIELDS = {
        "phase",
        "action",
        "last_error",
        "fallback_reason",
        "original_dp_ranks",
        "dead_committed",
        "fallback_strategy",
        "reconfiguration_dispatched",
        "dp_master_rank",
        "dp_master_ip",
    }
    _COMMITTED_PHASES = {FtPhase.POST_SCALE_CLEANUP, FtPhase.SCALED_DOWN_RUNNING}
    _INCOMPLETE_PHASES = {
        FtPhase.DECIDING,
        FtPhase.RELAUNCHING,
        FtPhase.WAITING_ENGINE_FAULT,
        FtPhase.FAST_RECOVERING,
        FtPhase.SCALING_DOWN,
        FtPhase.RECONFIGURING,
        FtPhase.WARMING_UP,
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instances: dict[int, FtRuntime] = {}
        # Instance ids are monotonic. Late tasks must not revive a retired id.
        self._removed: set[int] = set()
        self._persist_callback: Callable[[], bool] | None = None

    def set_persist_callback(self, callback: Callable[[], bool] | None) -> None:
        self._persist_callback = callback

    def get_or_create(self, instance_id: int) -> FtRuntime:
        with self._lock:
            self._check_not_removed(instance_id)
            runtime = self._instances.setdefault(instance_id, FtRuntime(instance_id=instance_id))
            return copy.deepcopy(runtime)

    def put(self, runtime: FtRuntime) -> None:
        with self._lock:
            self._check_not_removed(runtime.instance_id)
            self._instances[runtime.instance_id] = copy.deepcopy(runtime)

    def transition(self, instance_id: int, *, expected_recovery_id: str | None = None, **changes: Any) -> FtRuntime:
        """Atomically update selected runtime fields and return a detached snapshot."""
        durable_changed = False
        with self._lock:
            self._check_not_removed(instance_id)
            runtime = self._instances.get(instance_id)
            if expected_recovery_id is not None and (runtime is None or runtime.recovery_id != expected_recovery_id):
                raise StaleRecoveryOwner("stale recovery owner for instance %s" % instance_id)
            if runtime is None:
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

    def transition_if_owned(self, instance_id: int, *, expected_recovery_id: str, **changes: Any) -> FtRuntime | None:
        """Update atomically, skipping an obsolete owner's best-effort cleanup."""
        try:
            return self.transition(instance_id, expected_recovery_id=expected_recovery_id, **changes)
        except StaleRecoveryOwner:
            return None

    def settle_recovery(self, instance_id: int, recovery_id: str, *, failed: bool) -> None:
        """Derive the final observation under the same lock as owner validation."""
        with self._lock:
            runtime = self._instances.get(instance_id)
            if runtime is None or runtime.recovery_id != recovery_id:
                return
            if failed:
                runtime.stage = "failed"
            elif runtime.reconfiguration_dispatched:
                runtime.stage = "dispatched"
            elif not runtime.serving_published:
                runtime.stage = "publication_pending"
            else:
                runtime.stage = "completed"

    def get(self, instance_id: int) -> dict[str, Any] | None:
        with self._lock:
            runtime = self._instances.get(instance_id)
            return runtime.to_dict() if runtime is not None else None

    def is_reconfiguration_dispatched(self, instance_id: int) -> bool:
        """Whether this old instance has already been handed off for replacement."""
        with self._lock:
            runtime = self._instances.get(instance_id)
            return bool(
                runtime is not None and runtime.phase == FtPhase.RECONFIGURING and runtime.reconfiguration_dispatched
            )

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._instances[key].to_dict() for key in sorted(self._instances)]

    def remove(self, instance_id: int) -> bool:
        """Remove one obsolete instance runtime and persist durable deletion."""
        persist_callback: Callable[[], bool] | None = None
        with self._lock:
            self._removed.add(instance_id)
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
            out: dict[str, Any] = {}
            for instance_id, runtime in self._instances.items():
                if runtime.phase in {FtPhase.DISABLED, FtPhase.NORMAL} and not runtime.dead_committed:
                    continue
                payload: dict[str, Any] = {
                    "instance_id": instance_id,
                    "phase": runtime.phase.value,
                    "original_dp_ranks": list(runtime.original_dp_ranks),
                    "dead_committed": list(runtime.dead_committed),
                    "fallback_strategy": runtime.fallback_strategy,
                    "reconfiguration_dispatched": runtime.reconfiguration_dispatched,
                }
                if runtime.action:
                    payload["action"] = runtime.action
                if runtime.fallback_reason:
                    payload["fallback_reason"] = runtime.fallback_reason
                if runtime.last_error:
                    payload["last_error"] = runtime.last_error
                if runtime.dp_master_rank is not None:
                    payload["dp_master_rank"] = runtime.dp_master_rank
                if runtime.dp_master_ip:
                    payload["dp_master_ip"] = runtime.dp_master_ip
                out[str(instance_id)] = payload
            return out

    def clear_dp_master(self, instance_id: int) -> None:
        """Drop the scale-down elected master (whole-instance restart / reconfig)."""
        with self._lock:
            runtime = self._instances.get(instance_id)
            if runtime is None or (runtime.dp_master_rank is None and not runtime.dp_master_ip):
                return
        self.transition(instance_id, dp_master_rank=None, dp_master_ip=None)

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
                action = str(raw.get("action", ""))
                fallback_reason = raw.get("fallback_reason")
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
                    if phase == FtPhase.WARMING_UP:
                        error = "controller restarted during DP scale-up; original fallback required"
                    elif phase == FtPhase.RELAUNCHING or action == "RELAUNCH":
                        error = "controller restarted during engine relaunch; original fallback required"
                    elif phase == FtPhase.FAST_RECOVERING or action == "FAST_RECOVERY":
                        error = "controller restarted during fast recovery; original fallback required"
                    else:
                        error = "controller restarted during DP scale-down; original fallback required"
                else:
                    restored_phase = phase
                    error = None
                dp_master_rank = None
                dp_master_ip = None
                if restored_phase == FtPhase.SCALED_DOWN_RUNNING:
                    dp_master_rank = self._restore_optional_rank(raw.get("dp_master_rank"))
                    dp_master_ip = self._restore_optional_ip(raw.get("dp_master_ip"))
                restored[instance_id] = FtRuntime(
                    instance_id=instance_id,
                    phase=restored_phase,
                    action="RECONFIGURE" if restored_phase == FtPhase.RECONFIGURING else action,
                    stage="dispatched"
                    if reconfiguration_dispatched
                    else (
                        "fallback"
                        if restored_phase == FtPhase.RECONFIGURING
                        else "publication_pending"
                        if restored_phase == FtPhase.SCALED_DOWN_RUNNING
                        else "idle"
                    ),
                    original_dp_ranks=original_dp_ranks,
                    dead_committed=dead_committed,
                    fallback_strategy=fallback,
                    reconfiguration_dispatched=reconfiguration_dispatched,
                    serving_published=not dead_committed and restored_phase == FtPhase.NORMAL,
                    last_error=error or raw.get("last_error"),
                    fallback_reason=fallback_reason,
                    dp_master_rank=dp_master_rank,
                    dp_master_ip=dp_master_ip,
                )
            except (TypeError, ValueError, AttributeError) as error:
                logger.warning("Ignoring invalid persisted FT runtime %s: %s", raw_id, error)
        with self._lock:
            self._instances = {key: runtime for key, runtime in restored.items() if key not in self._removed}
            interrupted = {key: fallback for key, fallback in interrupted.items() if key not in self._removed}
        return interrupted

    @staticmethod
    def _restore_ranks(raw: Any) -> list[int]:
        if not isinstance(raw, list) or any(
            not isinstance(rank, int) or isinstance(rank, bool) or rank < 0 for rank in raw
        ):
            raise ValueError("DP ranks must be a list of non-negative integers")
        return sorted(set(raw))

    @staticmethod
    def _restore_optional_rank(raw: Any) -> int | None:
        if raw is None:
            return None
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise ValueError("dp_master_rank must be a non-negative integer")
        return raw

    @staticmethod
    def _restore_optional_ip(raw: Any) -> str | None:
        if raw is None or raw == "":
            return None
        if not isinstance(raw, str):
            raise ValueError("dp_master_ip must be a string")
        return raw

    def mark_serving_published(
        self,
        instance_ids: set[int],
        expected_owners: dict[int, str] | None = None,
        *,
        release_pause: bool = False,
    ) -> set[int]:
        """Acknowledge publication; release the pause only after an explicit unpaused SET."""
        published: set[int] = set()
        with self._lock:
            for instance_id in instance_ids:
                runtime = self._instances.get(instance_id)
                if expected_owners is not None and (
                    runtime is None or expected_owners.get(instance_id) != runtime.recovery_id
                ):
                    continue
                if runtime is None or runtime.phase not in {
                    FtPhase.NORMAL,
                    FtPhase.SCALED_DOWN_RUNNING,
                    FtPhase.WARMING_UP,
                }:
                    continue
                runtime.serving_published = True
                if release_pause:
                    runtime.recovery_paused = False
                if release_pause or runtime.stage == "publication_pending":
                    runtime.stage = "completed"
                if runtime.last_error:
                    errors = [
                        error
                        for error in runtime.last_error.split("; ")
                        if not error.startswith("ServingOverlay publication pending")
                    ]
                    runtime.last_error = "; ".join(errors) or None
                published.add(instance_id)
        return published

    def prepare_after_engine_relaunch(self, instance_id: int, expected_recovery_id: str | None = None) -> None:
        """Restore full DP membership after all restarted engines are ready."""
        with self._lock:
            runtime = self._instances.get(instance_id)
            if expected_recovery_id is not None and (runtime is None or runtime.recovery_id != expected_recovery_id):
                return
            if runtime is None or runtime.phase not in {
                FtPhase.RECONFIGURING,
                FtPhase.DECIDING,
                FtPhase.RELAUNCHING,
                FtPhase.WAITING_ENGINE_FAULT,
            }:
                return
            runtime.stage = "publication_pending"
            runtime.phase = FtPhase.NORMAL
            runtime.dead_committed = []
            runtime.serving_dp_ranks = []
            runtime.dp_master_rank = None
            runtime.dp_master_ip = None
            runtime.pending_removed_ranks = []
            runtime.request_id = ""
            runtime.serving_published = False
            runtime.last_error = None

    def resume_after_fault_clear(self, instance_id: int) -> FtRuntime | None:
        """Restore the last committed serving topology after evidence self-clears."""
        with self._lock:
            runtime = self._instances.get(instance_id)
            if runtime is None or runtime.phase != FtPhase.WAITING_ENGINE_FAULT:
                return None
            runtime.stage = "publication_pending"
            runtime.phase = FtPhase.SCALED_DOWN_RUNNING if runtime.dead_committed else FtPhase.NORMAL
            runtime.pending_removed_ranks = []
            runtime.preserve_recovery_streams = False
            runtime.serving_published = False
            runtime.last_error = None
            return copy.deepcopy(runtime)

    def mark_instance_reconfiguration_dispatched(
        self, instance_id: int, expected_recovery_id: str | None = None
    ) -> None:
        """Close scale-down history after recovery dispatch without declaring the old instance healthy."""
        self.transition(
            instance_id,
            expected_recovery_id=expected_recovery_id,
            phase=FtPhase.RECONFIGURING,
            original_dp_ranks=[],
            request_id="",
            dead_committed=[],
            pending_removed_ranks=[],
            serving_published=False,
            reconfiguration_dispatched=True,
            action="RECONFIGURE",
            stage="dispatched",
            last_error=None,
        )

    def clear(self) -> None:
        with self._lock:
            self._instances.clear()
            self._removed.clear()

    def _check_not_removed(self, instance_id: int) -> None:
        """Called under the store lock by every runtime creation path."""
        if instance_id in self._removed:
            raise StaleRecoveryOwner("stale recovery owner for removed instance %s" % instance_id)


_RUNTIME_STORE = FtRuntimeStore()


def get_ft_runtime_store() -> FtRuntimeStore:
    return _RUNTIME_STORE


def committed_dp_ranks(instance_id: int) -> set[int]:
    """Return ranks already committed by successful scale-down rounds."""
    runtime = _RUNTIME_STORE.get(instance_id)
    return set(runtime["dead_committed"]) if runtime is not None else set()


_PARTIAL_LOSS_TRANSACTION_PHASES = {
    FtPhase.DECIDING.value,
    FtPhase.RELAUNCHING.value,
    FtPhase.WAITING_ENGINE_FAULT.value,
    FtPhase.FAST_RECOVERING.value,
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
    if phase in (FtPhase.SCALED_DOWN_RUNNING.value, FtPhase.WARMING_UP.value):
        return PartialLossPolicy(
            ignored_dead_ranks=frozenset(runtime["dead_committed"]),
            suppress_legacy_shutdown=True,
        )
    return PartialLossPolicy()
