# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Data contracts for recovery evidence, decisions, admission and outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from motor.controller.fault_tolerance.fault_types import FaultInfo
    from motor.controller.fault_tolerance.strategy.base import StrategyBase


# (node name, reporter key) -> (evidence version, observed fault); None means instance-owned evidence.
SoftwareFaultEvidence = dict[tuple[str | None, int | str], tuple[int, "FaultInfo"]]


@dataclass(frozen=True)
class RecoveryContext:
    """Immutable fault evidence, deadlines and admission for one engine recovery attempt."""

    pending_removed_ranks: tuple[int, ...] = ()
    source: str = "software"
    fallback_strategy: str = ""
    fault_keys: tuple[str, ...] = ()
    hardware_evidence_versions: tuple[tuple[str, str], ...] = ()
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
    fast_recovery_allowed: bool = True
    hardware_mapping_complete: bool = True
    readiness_deadline: float | None = None
    hardware_correlation_deadline: float | None = None
    hardware_fault_codes: tuple[int, ...] = ()
    recovery_hardware_severe: bool = False


class RecoverySource(str, Enum):
    HARDWARE = "hardware"
    SOFTWARE = "software"


@dataclass(frozen=True)
class RecoveryPlan:
    """One immutable decision; EngineRecoveryStrategy means collect before choosing."""

    source: RecoverySource
    strategy: type[StrategyBase] | None
    context: RecoveryContext
    fallback: type[StrategyBase] | None


@dataclass
class GateSnapshot:
    """Auditable DP scale-down admission result."""

    allowed: bool = False
    reason_codes: list[str] = field(default_factory=list)


class FailurePolicy(str, Enum):
    NATIVE = "native"
    PREPARATION = "preparation"
    IN_PLACE = "in_place"
    FAST_RECOVERY = "fast_recovery"
    RELAUNCH = "relaunch"
    RECONFIGURATION = "reconfiguration"


class RecoveryAdvice(str, Enum):
    DEFAULT = "default"
    RELAUNCH = "relaunch"
    RECONFIGURE = "reconfigure"
    SCALE_P2D = "scale_p2d"
    STOP_INSTANCE = "stop_instance"


class RecoveryResult(BaseModel):
    """Persisted instructions for settlement, fallback and relapse protection."""

    model_config = ConfigDict(frozen=True)
    failure_policy: FailurePolicy = FailurePolicy.NATIVE
    advice: RecoveryAdvice = RecoveryAdvice.DEFAULT
    evaluate_without_fault: bool = False
    protect_relaunch: bool = False
    scale_up_reconfiguration: bool = False


def legacy_advice(name: str) -> RecoveryAdvice:
    """Decode the existing strategy recommendation field at its compatibility boundary."""
    return {
        "EngineRelaunchStrategy": RecoveryAdvice.RELAUNCH,
        "InstanceReconfigurationStrategy": RecoveryAdvice.RECONFIGURE,
        "ScaleP2DStrategy": RecoveryAdvice.SCALE_P2D,
        "NmSuicideStrategy": RecoveryAdvice.STOP_INSTANCE,
    }.get(name, RecoveryAdvice.DEFAULT)


def legacy_recovery_result(name: str, fallback: str = "") -> RecoveryResult:
    """Upgrade records written before behavioral results were persisted."""
    policy = {
        "EngineRecoveryStrategy": FailurePolicy.PREPARATION,
        "DpScaleDownStrategy": FailurePolicy.IN_PLACE,
        "EngineFastRecoveryStrategy": FailurePolicy.FAST_RECOVERY,
        "GenericFastRecoveryStrategy": FailurePolicy.FAST_RECOVERY,
        "TokenReinferenceStrategy": FailurePolicy.FAST_RECOVERY,
        "FastRecoveryTokenReinferenceStrategy": FailurePolicy.FAST_RECOVERY,
        "HbmUceRecoveryStrategy": FailurePolicy.FAST_RECOVERY,
        "EngineRelaunchStrategy": FailurePolicy.RELAUNCH,
        "InstanceReconfigurationStrategy": FailurePolicy.RECONFIGURATION,
    }.get(name, FailurePolicy.NATIVE)
    advice = legacy_advice(fallback)
    if name == "EngineFastRecoveryStrategy" and not fallback:
        advice = RecoveryAdvice.RECONFIGURE
    return RecoveryResult(
        failure_policy=policy,
        advice=advice,
        evaluate_without_fault=policy in {FailurePolicy.PREPARATION, FailurePolicy.FAST_RECOVERY},
        protect_relaunch=name
        in {"GenericFastRecoveryStrategy", "TokenReinferenceStrategy", "FastRecoveryTokenReinferenceStrategy"},
        scale_up_reconfiguration=policy == FailurePolicy.IN_PLACE,
    )
