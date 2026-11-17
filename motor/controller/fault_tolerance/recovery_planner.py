# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from dataclasses import dataclass, replace
from enum import Enum

from motor.config.controller import ControllerConfig
from motor.controller.fault_tolerance.dp_scale_down import ScaleDownContext
from motor.controller.fault_tolerance.fault_types import (
    FaultLevel,
    RecoveryCause,
    classify_recovery_cause,
)
from motor.controller.fault_tolerance.strategy.base import StrategyBase


def fast_recovery_policy(config: ControllerConfig, context: ScaleDownContext) -> type[StrategyBase] | None:
    """Select the entry point from hardware evidence before software fallback."""
    from motor.controller.fault_tolerance.strategy.fast_recovery import (
        HbmUceRecoveryStrategy,
        GenericFastRecoveryStrategy,
        TokenReinferenceStrategy,
    )

    ft = config.fault_tolerance_config
    if (
        not ft.enable_fault_tolerance
        or not ft.enable_fast_recovery
        or context.hardware_fault_observed
        or context.recovery_hardware_severe
    ):
        return None
    cause = classify_recovery_cause(context.hardware_fault_codes)
    if cause == RecoveryCause.HBM:
        return HbmUceRecoveryStrategy if ft.enable_hbm_uce_recovery else None
    if cause == RecoveryCause.TOKEN_REINFERENCE:
        return TokenReinferenceStrategy if ft.enable_token_reinference else None
    return GenericFastRecoveryStrategy if ft.enable_no_fault_code_recovery else None


class RecoverySource(str, Enum):
    HARDWARE = "hardware"
    SOFTWARE = "software"


@dataclass(frozen=True)
class RecoveryPlan:
    """Immutable decision for one recovery round."""

    source: RecoverySource
    strategy: type[StrategyBase] | None
    context: object
    fallback: type[StrategyBase] | None
    evidence: ScaleDownContext


def build_recovery_plan(
    instance_id: int,
    config: ControllerConfig,
    context: ScaleDownContext,
    generated_strategy: type[StrategyBase] | None,
    fault_level: FaultLevel,
    fault_code: int,
) -> RecoveryPlan:
    """Choose a recovery path from authoritative fault evidence.

    The first evidence source controls only hardware correlation timing. Both
    hardware and engine FT evidence participate in the final decision.
    """
    from motor.controller.fault_tolerance.strategy import (
        DpScaleDownStrategy,
        EngineFastRecoveryStrategy,
        EngineRelaunchStrategy,
        InstanceReconfigurationStrategy,
    )
    from motor.controller.fault_tolerance.strategy.fast_recovery import (
        FastRecoveryContext,
    )

    fallback = InstanceReconfigurationStrategy
    source = RecoverySource(context.source)
    evidence = context
    if context.already_removed_fault_keys and not (context.hardware_fault_observed or context.engine_fault_observed):
        return RecoveryPlan(source, None, context, fallback, evidence)

    # Engine-side recovery only applies to an ACTIVE serving instance. Keep the
    # original controller strategy for inactive instances and for hardware
    # evidence that cannot be mapped to a DP rank.
    if not context.engine_recovery_eligible:
        if generated_strategy is not None and issubclass(generated_strategy, EngineFastRecoveryStrategy):
            # An isolated/stopped instance cannot execute in-place fast recovery.
            # Keep native high-severity strategies below unchanged.
            return RecoveryPlan(source, None, context, fallback, evidence)
        # Heartbeat isolation is only a symptom: the instance is already
        # INACTIVE, but no engine or hardware evidence may have arrived yet.
        # Keep the original strategy (normally no-op for L1) so pod
        # rescheduling gets a chance to converge before whole-instance
        # reconfiguration is dispatched. If heartbeats do not recover,
        # InstanceManager removes the inactive instance after
        # CLEAR_INSTANCE_TIMEOUT.
        if not context.engine_fault_observed and not context.hardware_fault_observed:
            return RecoveryPlan(source, generated_strategy, context, fallback, evidence)
        return RecoveryPlan(source, generated_strategy or fallback, context, fallback, evidence)
    if context.hardware_fault_observed and (
        not context.hardware_mapping_complete or not context.hardware_affected_ranks
    ):
        return RecoveryPlan(source, generated_strategy or fallback, context, fallback, evidence)

    policy = fast_recovery_policy(config, context)
    if context.hardware_fault_codes and policy is not None and not context.engine_fault_observed:
        # Recognized recoverable hardware is recorded first. The software report
        # opens collection; the short L4 hardware correlation timer is unrelated.
        return RecoveryPlan(source, None, context, fallback, evidence)

    if source == RecoverySource.HARDWARE and context.hardware_fault_observed:
        # A complete engine snapshot is authoritative even when its ranks
        # arrive after the short hardware-correlation window. The window only
        # decides how long hardware-only evidence may wait before falling back.
        if context.hardware_wait_timed_out and not context.collection_complete:
            return RecoveryPlan(source, fallback, context, fallback, evidence)
        if not context.hardware_ft_observed and not context.collection_complete:
            return RecoveryPlan(source, None, context, fallback, evidence)

    if context.all_dp_unhealthy:
        if context.hardware_fault_observed and config.fault_tolerance_config.enable_fast_recovery:
            return RecoveryPlan(source, generated_strategy or fallback, context, fallback, evidence)
        if policy is not None:
            return RecoveryPlan(source, policy, context, fallback, evidence)
        if config.fault_tolerance_config.enable_fast_recovery:
            return RecoveryPlan(source, fallback, context, fallback, evidence)
        fast_context = FastRecoveryContext(
            fault_level=fault_level,
            fault_code=fault_code,
            source=source.value,
        )
        if EngineFastRecoveryStrategy.is_applicable(instance_id, fast_context, config):
            return RecoveryPlan(source, EngineFastRecoveryStrategy, fast_context, fallback, evidence)
        strategy = fallback if context.collection_timed_out else None
        return RecoveryPlan(source, strategy, context, fallback, evidence)

    # Losing every DP rank normally means the instance needs reconfiguration.
    # A pure software ENGINE_DEAD report is different: while the instance is
    # still ACTIVE, the generated relaunch strategy can restore those ranks in
    # place. This is especially important for a single-DP docker-only instance,
    # where all_dp_removed is true as soon as its only engine dies.
    if (
        context.all_dp_removed
        and source == RecoverySource.SOFTWARE
        and context.engine_fault_observed
        and not context.hardware_fault_observed
        and generated_strategy is EngineRelaunchStrategy
    ):
        return RecoveryPlan(source, generated_strategy, context, fallback, evidence)
    if context.all_dp_removed:
        return RecoveryPlan(source, fallback, context, fallback, evidence)
    if context.collection_complete:
        if config.fault_tolerance_config.enable_dp_scale_down and context.pending_removed_ranks:
            planned_context = replace(context, fallback_strategy=fallback.__name__)
            return RecoveryPlan(source, DpScaleDownStrategy, planned_context, fallback, evidence)
        return RecoveryPlan(source, fallback, context, fallback, evidence)
    if context.engine_fault_observed:
        strategy = fallback if context.collection_timed_out else None
        return RecoveryPlan(source, strategy, context, fallback, evidence)
    if context.hardware_fault_observed:
        return RecoveryPlan(source, None, context, fallback, evidence)
    if fault_level <= FaultLevel.L2:
        return RecoveryPlan(source, None, context, fallback, evidence)
    return RecoveryPlan(source, generated_strategy or fallback, context, fallback, evidence)
