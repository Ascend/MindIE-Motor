# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Recovery admission, initial planning and failure handoff decisions."""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
from typing import TYPE_CHECKING

from motor.common.resources.instance import Instance, ReadOnlyInstance
from motor.config.controller import ControllerConfig, FaultToleranceConfig
from motor.controller.fault_tolerance.engine_recovery.contracts import (
    FailurePolicy,
    GateSnapshot,
    RecoveryAdvice,
    RecoveryContext,
    RecoveryPlan,
    RecoveryResult,
    RecoverySource,
)
from motor.controller.fault_tolerance.fault_types import FaultLevel, RecoveryCause, classify_recovery_cause

if TYPE_CHECKING:
    from motor.controller.fault_tolerance.strategy.base import StrategyBase


class _RecoveryScene(str, Enum):
    ALL_REMOVED = "all_removed"
    UNAVAILABLE = "unavailable"
    ALL_UNHEALTHY = "all_unhealthy"
    COLLECTION_COMPLETE = "collection_complete"
    COLLECTION_TIMEOUT = "collection_timeout"
    COLLECTING = "collecting"
    NO_ENGINE_FAULT = "no_engine_fault"


def _classify_recovery_scene(context: RecoveryContext, actions_available: bool) -> _RecoveryScene:
    """Resolve overlapping evidence in policy order, without selecting a strategy."""
    if context.all_dp_removed:
        return _RecoveryScene.ALL_REMOVED
    if not actions_available:
        return _RecoveryScene.UNAVAILABLE
    if context.all_dp_unhealthy:
        return _RecoveryScene.ALL_UNHEALTHY
    if context.collection_complete:
        return _RecoveryScene.COLLECTION_COMPLETE
    if context.collection_timed_out or context.hardware_wait_timed_out:
        return _RecoveryScene.COLLECTION_TIMEOUT
    if context.engine_fault_observed or context.hardware_fault_observed:
        return _RecoveryScene.COLLECTING
    return _RecoveryScene.NO_ENGINE_FAULT


def scale_down_gate(config, instance) -> GateSnapshot:
    """Use registered NodeManager capabilities; never parse engine configuration here."""
    reasons = []
    if not config.enable_fault_tolerance or not config.enable_dp_scale_down:
        return GateSnapshot(allowed=False, reason_codes=["FEATURE_DISABLED"])
    capability = instance.ft_capability
    requirements = (
        (capability.enabled, "ENGINE_FT_REQUIRED"),
        (capability.external_lb, "EXTERNAL_LB_REQUIRED"),
        (capability.scale_down_supported, "ENGINE_SCALE_DOWN_UNSUPPORTED"),
    )
    reasons.extend(reason for allowed, reason in requirements if not allowed)
    return GateSnapshot(allowed=not reasons, reason_codes=reasons)


def engine_recovery_eligible(
    instance: Instance | ReadOnlyInstance | None,
    runtime: dict,
    *,
    recovery_ready: bool,
    engine_fault_observed: bool,
    separated: bool = False,
) -> bool:
    """Serving loss alone must not disable engine recovery; NM loss still does."""
    from motor.common.resources import InsStatus
    from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase

    if instance is None or separated or runtime.get("reconfiguration_dispatched"):
        return False
    if runtime.get("phase") in {FtPhase.RECONFIGURING.value, FtPhase.WARMING_UP.value, FtPhase.RELAUNCHING.value}:
        return False
    retired = set(runtime.get("dead_committed") or [])
    if instance.status == InsStatus.ACTIVE:
        return bool(instance.is_all_endpoints_heartbeat_fresh(ignored_endpoint_ids=retired))
    return bool(
        instance.status == InsStatus.INACTIVE
        and recovery_ready
        and engine_fault_observed
        and instance.is_have_one_endpoint_abnormal(retired)
        and instance.is_all_endpoints_heartbeat_fresh(ignored_endpoint_ids=retired)
    )


def fast_recovery_gate(config: FaultToleranceConfig, instance: Instance | ReadOnlyInstance) -> GateSnapshot:
    """Retry requires engine FT, without inheriting scale-down capability gates."""
    if not config.enable_fault_tolerance or not config.enable_fast_recovery:
        return GateSnapshot(allowed=False, reason_codes=["FEATURE_DISABLED"])
    reasons = [] if instance.ft_capability.enabled else ["ENGINE_FT_REQUIRED"]
    return GateSnapshot(allowed=not reasons, reason_codes=reasons)


def fast_recovery_policy(config: ControllerConfig, context: RecoveryContext) -> type[StrategyBase] | None:
    from motor.controller.fault_tolerance.strategy.engine_ft.fast_recovery import (
        GenericFastRecoveryStrategy,
        HbmUceRecoveryStrategy,
        TokenReinferenceStrategy,
    )

    ft = config.fault_tolerance_config
    if not ft.enable_fault_tolerance or not ft.enable_fast_recovery or not context.fast_recovery_allowed:
        return None
    if context.hardware_fault_observed or context.recovery_hardware_severe:
        return None
    policies = {
        RecoveryCause.HBM: (HbmUceRecoveryStrategy, ft.enable_hbm_uce_recovery),
        RecoveryCause.TOKEN_REINFERENCE: (TokenReinferenceStrategy, ft.enable_token_reinference),
        RecoveryCause.SOFTWARE: (GenericFastRecoveryStrategy, ft.enable_no_fault_code_recovery),
    }
    strategy, enabled = policies[classify_recovery_cause(context.hardware_fault_codes)]
    return strategy if enabled else None


def build_recovery_plan(
    config: ControllerConfig,
    context: RecoveryContext,
    generated_strategy: type[StrategyBase] | None,
    fault_level: FaultLevel,
    *,
    scale_down_allowed: bool = True,
) -> RecoveryPlan:
    """Preserve native priority, then fast recovery, shrink, and full-instance fallback."""
    from motor.controller.fault_tolerance.strategy import (
        DpScaleDownStrategy,
        EngineFastRecoveryStrategy,
        EngineRelaunchStrategy,
        InstanceReconfigurationStrategy,
        NmSuicideStrategy,
    )
    from motor.controller.fault_tolerance.engine_recovery.runner import EngineRecoveryStrategy

    source = RecoverySource(context.source)
    fallback = InstanceReconfigurationStrategy
    if not context.hardware_fault_observed and config.fault_tolerance_config.enable_engine_relaunch:
        fallback = EngineRelaunchStrategy

    def plan(strategy: type[StrategyBase] | None, context: RecoveryContext = context) -> RecoveryPlan:
        return RecoveryPlan(source, strategy, context, fallback)

    if (
        not context.engine_recovery_eligible
        and generated_strategy is not None
        and issubclass(generated_strategy, EngineFastRecoveryStrategy)
    ):
        return plan(None)

    ft = config.fault_tolerance_config
    if not ft.enable_fault_tolerance or not (
        ft.enable_fast_recovery or (ft.enable_dp_scale_down and scale_down_allowed)
    ):
        fallback = InstanceReconfigurationStrategy
        if context.already_removed_fault_keys and not (
            context.engine_fault_observed or context.hardware_fault_observed
        ):
            return plan(None)
        if not context.engine_recovery_eligible and (context.engine_fault_observed or context.hardware_fault_observed):
            return plan(generated_strategy or fallback)
        if context.hardware_fault_observed and (
            not context.hardware_mapping_complete or not context.hardware_affected_ranks
        ):
            return plan(generated_strategy or fallback)
        return plan(generated_strategy)
    if generated_strategy is NmSuicideStrategy:
        return plan(generated_strategy)
    if not context.engine_recovery_eligible:
        fallback = InstanceReconfigurationStrategy
        return plan(
            generated_strategy
            if not (context.engine_fault_observed or context.hardware_fault_observed)
            else generated_strategy or fallback
        )
    if context.already_removed_fault_keys and not (context.hardware_fault_observed or context.engine_fault_observed):
        return plan(None)
    if context.hardware_fault_observed and (
        not context.hardware_mapping_complete or not context.hardware_affected_ranks
    ):
        return plan(generated_strategy or fallback)
    fast = fast_recovery_policy(config, context) if not context.all_dp_removed else None
    shrink = scale_down_allowed and ft.enable_dp_scale_down

    scene_strategies = {
        _RecoveryScene.ALL_REMOVED: fallback,
        _RecoveryScene.UNAVAILABLE: generated_strategy or (fallback if context.engine_fault_observed else None),
        _RecoveryScene.ALL_UNHEALTHY: fast or fallback,
        _RecoveryScene.COLLECTION_COMPLETE: DpScaleDownStrategy
        if shrink and context.pending_removed_ranks
        else fallback,
        _RecoveryScene.COLLECTION_TIMEOUT: fallback,
        _RecoveryScene.COLLECTING: EngineRecoveryStrategy,
        _RecoveryScene.NO_ENGINE_FAULT: None if fault_level <= FaultLevel.L2 else generated_strategy or fallback,
    }
    scene = _classify_recovery_scene(context, actions_available=bool(fast or shrink))
    selected = scene_strategies[scene]
    if scene == _RecoveryScene.COLLECTION_COMPLETE and selected is DpScaleDownStrategy:
        return plan(selected, replace(context, fallback_strategy=fallback.__name__))
    return plan(selected)


def build_failure_plan(
    result: RecoveryResult,
    config: ControllerConfig,
    context: RecoveryContext,
    *,
    generated_strategy: type[StrategyBase] | None = None,
) -> type[StrategyBase] | None:
    """Plan from behavioral results after the previous owner and cleanup have returned."""
    from motor.controller.fault_tolerance.strategy import (
        EngineRelaunchStrategy,
        InstanceReconfigurationStrategy,
        NmSuicideStrategy,
        ScaleP2DStrategy,
    )

    hardware = context.hardware_fault_observed
    relaunch = config.fault_tolerance_config.enable_engine_relaunch

    def plan_relaunch_or_reconfigure() -> type[StrategyBase]:
        return EngineRelaunchStrategy if relaunch and not hardware else InstanceReconfigurationStrategy

    def plan_native_failure() -> type[StrategyBase] | None:
        return EngineRelaunchStrategy if relaunch else None

    def plan_preparation_failure() -> type[StrategyBase]:
        handoff = {
            RecoveryAdvice.STOP_INSTANCE: NmSuicideStrategy,
            RecoveryAdvice.SCALE_P2D: ScaleP2DStrategy,
            RecoveryAdvice.RECONFIGURE: InstanceReconfigurationStrategy,
        }
        return handoff.get(result.advice) or plan_relaunch_or_reconfigure()

    def plan_fast_recovery_failure() -> type[StrategyBase]:
        if result.advice == RecoveryAdvice.RECONFIGURE:
            return InstanceReconfigurationStrategy
        return plan_relaunch_or_reconfigure()

    def plan_relaunch_failure() -> type[StrategyBase]:
        return InstanceReconfigurationStrategy

    def plan_reconfiguration_failure() -> type[StrategyBase] | None:
        return generated_strategy if hardware else None

    failure_handlers = {
        FailurePolicy.NATIVE: plan_native_failure,
        FailurePolicy.PREPARATION: plan_preparation_failure,
        FailurePolicy.IN_PLACE: plan_relaunch_or_reconfigure,
        FailurePolicy.FAST_RECOVERY: plan_fast_recovery_failure,
        FailurePolicy.RELAUNCH: plan_relaunch_failure,
        FailurePolicy.RECONFIGURATION: plan_reconfiguration_failure,
    }
    return failure_handlers[result.failure_policy]()
