# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
from dataclasses import dataclass, replace
from enum import Enum

from motor.config.controller import ControllerConfig
from motor.controller.fault_tolerance.dp_scale_down import ScaleDownContext
from motor.controller.fault_tolerance.fault_types import FaultLevel
from motor.controller.fault_tolerance.strategy.base import StrategyBase


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
        InstanceReconfigurationStrategy,
    )
    from motor.controller.fault_tolerance.strategy.fast_recovery import FastRecoveryContext

    fallback = InstanceReconfigurationStrategy
    source = RecoverySource(context.source)
    evidence = context
    if context.already_removed_fault_keys and not (context.hardware_fault_observed or context.engine_fault_observed):
        return RecoveryPlan(source, None, context, fallback, evidence)

    # Engine-side recovery only applies to an ACTIVE serving instance. Keep the
    # original controller strategy for inactive instances and for hardware
    # evidence that cannot be mapped to a DP rank.
    if not context.engine_recovery_eligible:
        return RecoveryPlan(source, generated_strategy or fallback, context, fallback, evidence)
    if context.hardware_fault_observed and (
        not context.hardware_mapping_complete or not context.hardware_affected_ranks
    ):
        return RecoveryPlan(source, generated_strategy or fallback, context, fallback, evidence)

    if source == RecoverySource.HARDWARE and context.hardware_fault_observed:
        # A complete engine snapshot is authoritative even when its ranks
        # arrive after the short hardware-correlation window. The window only
        # decides how long hardware-only evidence may wait before falling back.
        if context.hardware_wait_timed_out and not context.collection_complete:
            return RecoveryPlan(source, fallback, context, fallback, evidence)
        if not context.hardware_ft_observed and not context.collection_complete:
            return RecoveryPlan(source, None, context, fallback, evidence)

    if context.all_dp_unhealthy:
        fast_context = FastRecoveryContext(
            fault_level=fault_level,
            fault_code=fault_code,
            source=source.value,
        )
        if EngineFastRecoveryStrategy.is_applicable(instance_id, fast_context, config):
            return RecoveryPlan(source, EngineFastRecoveryStrategy, fast_context, fallback, evidence)
        strategy = fallback if context.collection_timed_out else None
        return RecoveryPlan(source, strategy, context, fallback, evidence)

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
