# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from collections.abc import Callable
from motor.config.controller import ControllerConfig
from motor.controller.fault_tolerance.fault_types import (
    A2_PD_ISOLATION_FAULT_CODES,
    FaultLevel,
    SpecialFaultCode,
    is_800i_a2,
)
from motor.controller.fault_tolerance.strategy.base import StrategyBase


def healthy_strategy(fault_code: int, instance_id: int, config: ControllerConfig) -> type[StrategyBase] | None:
    "level healthy means this instance is healthy, so no strategy is needed."
    return None


def level1_strategy(fault_code: int, instance_id: int, config: ControllerConfig) -> type[StrategyBase] | None:
    return None


def level2_strategy(fault_code: int, instance_id: int, config: ControllerConfig) -> type[StrategyBase] | None:
    # A dead engine cannot be recovered in place — run the fallback relaunch
    # (restart engines, then containers). ENGINE_UNHEALTHY keeps the current
    # no-op behavior (fast-recovery strategies take it over once merged).
    if fault_code == int(SpecialFaultCode.ENGINE_DEAD):
        if not config.fault_tolerance_config.enable_engine_relaunch:
            return None
        from motor.controller.fault_tolerance.strategy.engine_relaunch import EngineRelaunchStrategy

        return EngineRelaunchStrategy

    # Hardware L2 faults: only handle whitelisted fault codes (token reinference)
    if not config.fault_tolerance_config.enable_token_reinference:
        return None

    if fault_code in [0x00F1FEF5, 0x08520003]:
        from motor.controller.fault_tolerance.strategy.token_reinference import TokenReinferenceStrategy

        return TokenReinferenceStrategy
    return None


def level3_strategy(fault_code: int, instance_id: int, config: ControllerConfig) -> type[StrategyBase] | None:
    # L3 faults call L1 strategy logic
    return level1_strategy(fault_code, instance_id, config)


def level4_strategy(fault_code: int, instance_id: int, config: ControllerConfig) -> type[StrategyBase] | None:
    # Check if strategy is enabled first
    if not config.fault_tolerance_config.enable_scale_p2d:
        return None

    from motor.controller.core.instance_manager import InstanceManager
    from motor.controller.fault_tolerance.strategy.scale_p2d import ScaleP2DStrategy

    instance = InstanceManager().get_instance(instance_id)
    if instance is not None and instance.role == "decode":
        return ScaleP2DStrategy
    else:
        return None


def level5_strategy(fault_code: int, instance_id: int, config: ControllerConfig) -> type[StrategyBase] | None:
    # Note: Currently L5 faults call L4 strategy logic
    return level4_strategy(fault_code, instance_id, config)


def level6_strategy(fault_code: int, instance_id: int, config: ControllerConfig) -> type[StrategyBase] | None:
    from motor.controller.core.instance_manager import InstanceManager
    from motor.controller.fault_tolerance.fault_types import instance_requires_a2_linkdown_l6
    from motor.controller.fault_tolerance.strategy.nm_suicide import NmSuicideStrategy

    instance = InstanceManager().get_instance(instance_id)
    if instance is None:
        return None

    role_val = getattr(instance.role, "value", instance.role)
    uses_nm_suicide = (
        is_800i_a2(getattr(config, "hardware_type", "") or "") and fault_code in A2_PD_ISOLATION_FAULT_CODES
    )
    # A2 isolation codes leave Decode running; ScaleP2D only stops Prefill.
    if role_val in ("prefill", "decode") and uses_nm_suicide:
        return NmSuicideStrategy
    if role_val == "decode":
        return level4_strategy(fault_code, instance_id, config)
    if role_val == "union" and uses_nm_suicide and instance_requires_a2_linkdown_l6(instance):
        return NmSuicideStrategy
    return None


def generate_strategy_map() -> dict[int, Callable[[int, int, ControllerConfig], type[StrategyBase] | None] | None]:
    return {
        FaultLevel.HEALTHY: healthy_strategy,
        FaultLevel.L1: level1_strategy,
        FaultLevel.L2: level2_strategy,
        FaultLevel.L3: level3_strategy,
        FaultLevel.L4: level4_strategy,
        FaultLevel.L5: level5_strategy,
        FaultLevel.L6: level6_strategy,
    }
