# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Policy choice stays independent from shrink capability and serving status."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from motor.common.resources import InsStatus
from motor.config.controller import ControllerConfig
from motor.controller.fault_tolerance.engine_recovery.planner import (
    engine_recovery_eligible,
    build_failure_plan,
    build_recovery_plan,
    fast_recovery_policy,
)
from motor.controller.fault_tolerance.engine_recovery.contracts import (
    RecoveryContext,
    FailurePolicy,
    RecoveryResult,
    legacy_recovery_result,
)
from motor.controller.fault_tolerance.fault_types import FaultLevel
from motor.controller.fault_tolerance.strategy import (
    DpScaleDownStrategy,
    EngineRelaunchStrategy,
    InstanceReconfigurationStrategy,
    NmSuicideStrategy,
)
from motor.controller.fault_tolerance.engine_recovery.runner import EngineRecoveryStrategy
from motor.controller.fault_tolerance.strategy.engine_ft.fast_recovery import (
    GenericFastRecoveryStrategy,
    TokenReinferenceStrategy,
    HbmUceRecoveryStrategy,
)


@pytest.mark.parametrize("fast,shrink", [(True, True), (True, False), (False, True), (False, False)])
def test_fast_gate_is_independent_of_shrink(fast, shrink):
    config = ControllerConfig()
    config.fault_tolerance_config.enable_fast_recovery = fast
    config.fault_tolerance_config.enable_dp_scale_down = shrink
    config.fault_tolerance_config.enable_engine_relaunch = True
    context = RecoveryContext(all_dp_unhealthy=True, engine_fault_observed=True)
    plan = build_recovery_plan(config, context, EngineRelaunchStrategy, FaultLevel.L2, scale_down_allowed=shrink)
    assert plan.strategy is (GenericFastRecoveryStrategy if fast else EngineRelaunchStrategy)


@pytest.mark.parametrize(
    "codes,hbm_enabled,policy",
    [
        ((), True, GenericFastRecoveryStrategy),
        ((0x08520003,), True, TokenReinferenceStrategy),
        ((0x80E01801, 0x08520003), True, HbmUceRecoveryStrategy),
        ((0x80E01801, 0x08520003), False, None),
    ],
)
def test_fast_cause_priority_does_not_fall_through_when_disabled(codes, hbm_enabled, policy):
    config = ControllerConfig()
    ft = config.fault_tolerance_config
    ft.enable_fast_recovery = ft.enable_token_reinference = ft.enable_no_fault_code_recovery = True
    ft.enable_hbm_uce_recovery = hbm_enabled
    context = RecoveryContext(all_dp_unhealthy=True, engine_fault_observed=True, hardware_fault_codes=codes)
    assert fast_recovery_policy(config, context) is policy
    if policy is not None:
        assert build_recovery_plan(config, context, None, FaultLevel.L2).strategy is policy


@pytest.mark.parametrize("hardware", [False, True])
def test_incomplete_evidence_waits_then_routes_actionable_hardware_fallback(hardware):
    config = ControllerConfig()
    config.fault_tolerance_config.enable_fast_recovery = True
    config.fault_tolerance_config.enable_dp_scale_down = True
    config.fault_tolerance_config.enable_engine_relaunch = True
    context = RecoveryContext(
        engine_fault_observed=True,
        hardware_fault_observed=hardware,
        hardware_affected_ranks=(1,) if hardware else (),
    )
    assert build_recovery_plan(config, context, None, FaultLevel.L2).strategy is EngineRecoveryStrategy
    expired = replace(context, collection_timed_out=True)
    expected = InstanceReconfigurationStrategy if hardware else EngineRelaunchStrategy
    assert build_recovery_plan(config, expired, None, FaultLevel.L2).strategy is expected


@pytest.mark.parametrize(
    "hardware,severe,mapped,relaunch",
    [
        (False, False, True, True),
        (False, True, True, True),
        (True, True, True, True),
        (True, True, False, True),
        (False, False, True, False),
    ],
    ids=["observational", "excluded-hardware", "active-hardware", "unmapped-hardware", "relaunch-disabled"],
)
def test_shrink_fallback_uses_current_actionable_hardware(hardware, severe, mapped, relaunch):
    """Unrelated topology alarms must not turn software recovery into instance replacement."""
    config = ControllerConfig()
    config.fault_tolerance_config.enable_dp_scale_down = True
    config.fault_tolerance_config.enable_engine_relaunch = relaunch
    context = RecoveryContext(
        engine_fault_observed=True,
        collection_complete=True,
        pending_removed_ranks=(1,),
        recovery_hardware_severe=severe,
        hardware_fault_observed=hardware,
        hardware_mapping_complete=mapped,
        hardware_affected_ranks=(1,) if hardware and mapped else (),
    )
    expected = EngineRelaunchStrategy if relaunch and not hardware else InstanceReconfigurationStrategy
    plan = build_recovery_plan(config, context, None, FaultLevel.L2)
    assert plan.fallback is expected
    assert plan.strategy is (expected if hardware and not mapped else DpScaleDownStrategy)
    if plan.strategy is DpScaleDownStrategy:
        assert plan.context == replace(context, fallback_strategy=expected.__name__)
    result = RecoveryResult(failure_policy=FailurePolicy.IN_PLACE)
    assert build_failure_plan(result, config, plan.context) is expected
    assert build_failure_plan(result, config, replace(plan.context, hardware_fault_observed=True)) is (
        InstanceReconfigurationStrategy
    )


@pytest.mark.parametrize(
    "policy,advice,hardware,relaunch,expected",
    [
        ("fast_recovery", "relaunch", False, True, EngineRelaunchStrategy),
        ("fast_recovery", "relaunch", True, True, InstanceReconfigurationStrategy),
        ("fast_recovery", "reconfigure", False, True, InstanceReconfigurationStrategy),
        ("fast_recovery", "reconfigure", True, True, InstanceReconfigurationStrategy),
        ("relaunch", "default", False, True, InstanceReconfigurationStrategy),
        ("native", "default", False, False, None),
        ("preparation", "stop_instance", False, True, NmSuicideStrategy),
        ("preparation", "relaunch", True, True, InstanceReconfigurationStrategy),
        ("native", "reconfigure", True, False, None),
        ("reconfiguration", "reconfigure", False, True, None),
    ],
    ids=[
        "software-relaunch",
        "hardware-relaunch-advice",
        "software-reconfigure-advice",
        "hardware-reconfigure-advice",
        "relaunch-already-failed",
        "native-relaunch-disabled",
        "preparation-native-handoff",
        "preparation-new-hardware",
        "native-no-action-before-advice",
        "reconfiguration-no-action-before-advice",
    ],
)
def test_failure_plan_preserves_advice_and_hardware_safety(policy, advice, hardware, relaunch, expected):
    config = ControllerConfig()
    config.fault_tolerance_config.enable_engine_relaunch = relaunch
    result = RecoveryResult(failure_policy=policy, advice=advice)
    context = RecoveryContext(hardware_fault_observed=hardware)
    assert build_failure_plan(result, config, context) is expected


@pytest.mark.parametrize(
    "status,ready,fresh,separated,phase,expected",
    [
        (InsStatus.INACTIVE, True, True, False, "NORMAL", True),
        (InsStatus.INACTIVE, False, True, False, "NORMAL", False),
        (InsStatus.INACTIVE, True, False, False, "NORMAL", False),
        (InsStatus.ACTIVE, True, True, True, "NORMAL", False),
        (InsStatus.ACTIVE, True, False, False, "NORMAL", False),
        (InsStatus.INITIAL, False, True, False, "NORMAL", False),
        (InsStatus.ACTIVE, True, True, False, "RELAUNCHING", False),
    ],
)
def test_admission_uses_recoverability_without_requiring_active(status, ready, fresh, separated, phase, expected):
    instance = Mock(status=status)
    instance.is_have_one_endpoint_abnormal.return_value = True
    instance.is_all_endpoints_heartbeat_fresh.return_value = fresh
    assert (
        engine_recovery_eligible(
            instance, {"phase": phase}, recovery_ready=ready, engine_fault_observed=True, separated=separated
        )
        is expected
    )


@pytest.mark.parametrize("external_lb", [False, True])
@pytest.mark.parametrize("engine_ft", [False, True])
def test_fast_capability_does_not_inherit_shrink_requirements(external_lb, engine_ft):
    from motor.common.resources.instance import FtCapabilitySnapshot
    from motor.controller.fault_tolerance.engine_recovery.planner import fast_recovery_gate

    config = ControllerConfig()
    config.fault_tolerance_config.enable_fast_recovery = True
    config.fault_tolerance_config.enable_engine_relaunch = False
    instance = Mock(ft_capability=FtCapabilitySnapshot(enabled=engine_ft, external_lb=external_lb))
    gate = fast_recovery_gate(config.fault_tolerance_config, instance)
    assert gate.allowed is engine_ft
    assert gate.reason_codes == ([] if engine_ft else ["ENGINE_FT_REQUIRED"])
    context = RecoveryContext(all_dp_unhealthy=True, engine_fault_observed=True, fast_recovery_allowed=gate.allowed)
    plan = build_recovery_plan(config, context, None, FaultLevel.L2)
    assert plan.strategy is (GenericFastRecoveryStrategy if engine_ft else InstanceReconfigurationStrategy)


@pytest.mark.parametrize("relaunch", [False, True])
@pytest.mark.parametrize("unmapped_hardware", [False, True])
def test_disabled_recovery_preserves_native_plan(relaunch, unmapped_hardware):
    """Disabled optional policies must not introduce instance reconfiguration."""
    config = ControllerConfig()
    config.fault_tolerance_config.enable_fast_recovery = False
    config.fault_tolerance_config.enable_dp_scale_down = False
    config.fault_tolerance_config.enable_engine_relaunch = relaunch
    context = RecoveryContext(engine_fault_observed=True, hardware_fault_observed=unmapped_hardware)
    native = EngineRelaunchStrategy if relaunch else None
    expected = InstanceReconfigurationStrategy if unmapped_hardware and native is None else native
    assert build_recovery_plan(config, context, native, FaultLevel.L2).strategy is expected


@pytest.mark.parametrize("master", [False, True])
@pytest.mark.parametrize("code", [0x00F1FEF5, 0x08520003])
def test_token_code_waits_for_engine_evidence_and_uses_only_fast_policy(master, code):
    from motor.controller.fault_tolerance.strategy.strategy import level2_strategy
    from motor.controller.fault_tolerance.strategy import TokenReinferenceStrategy as ExportedToken

    config = ControllerConfig()
    config.fault_tolerance_config.enable_fast_recovery = master
    assert ExportedToken is TokenReinferenceStrategy
    assert level2_strategy(code, 7, config) is None
    context = RecoveryContext(hardware_fault_codes=(code,))
    assert build_recovery_plan(config, context, None, FaultLevel.L2).strategy is None
    context = replace(context, engine_fault_observed=True)
    assert build_recovery_plan(config, context, None, FaultLevel.L2).strategy is (
        EngineRecoveryStrategy if master else None
    )
    context = replace(context, all_dp_unhealthy=True)
    assert build_recovery_plan(config, context, None, FaultLevel.L2).strategy is (
        TokenReinferenceStrategy if master else None
    )


@pytest.mark.parametrize(
    "name,fallback,policy,advice,protect",
    [
        ("TokenReinferenceStrategy", "InstanceReconfigurationStrategy", "fast_recovery", "reconfigure", True),
        (
            "FastRecoveryTokenReinferenceStrategy",
            "InstanceReconfigurationStrategy",
            "fast_recovery",
            "reconfigure",
            True,
        ),
        ("HbmUceRecoveryStrategy", "EngineRelaunchStrategy", "fast_recovery", "relaunch", False),
        ("HbmUceRecoveryStrategy", "InstanceReconfigurationStrategy", "fast_recovery", "reconfigure", False),
        ("GenericFastRecoveryStrategy", "EngineRelaunchStrategy", "fast_recovery", "relaunch", True),
        ("EngineRecoveryStrategy", "NmSuicideStrategy", "preparation", "stop_instance", False),
        ("EngineRecoveryStrategy", "EngineRelaunchStrategy", "preparation", "relaunch", False),
        ("EngineRelaunchStrategy", "", "relaunch", "default", False),
        ("ScaleP2DStrategy", "", "native", "default", False),
        ("DpScaleDownStrategy", "", "in_place", "default", False),
    ],
)
def test_legacy_failure_records_preserve_behavior_fields(name, fallback, policy, advice, protect):
    result = legacy_recovery_result(name, fallback)
    assert result.model_dump(mode="json") == {
        "failure_policy": policy,
        "advice": advice,
        "protect_relaunch": protect,
        "evaluate_without_fault": policy in {"preparation", "fast_recovery"},
        "scale_up_reconfiguration": policy == "in_place",
    }


def test_persisted_result_overrides_misleading_display_name():
    from motor.controller.fault_tolerance.fault_types import InstanceMetadata

    result = RecoveryResult(failure_policy=FailurePolicy.RELAUNCH)
    metadata = InstanceMetadata(
        instance_id=7, prev_strategy_name="arbitrary renamed policy", prev_recovery_result=result
    )
    restored = InstanceMetadata.model_validate(metadata.model_dump(mode="json"))
    assert restored.failure_result == result
    assert (
        build_failure_plan(restored.failure_result, ControllerConfig(), RecoveryContext())
        is InstanceReconfigurationStrategy
    )


@pytest.mark.parametrize(
    "evidence,generated,expected",
    [
        ({"all_dp_unhealthy": True}, None, GenericFastRecoveryStrategy),
        ({}, None, DpScaleDownStrategy),
        (
            {"engine_fault_observed": False, "collection_complete": False, "collection_timed_out": False},
            InstanceReconfigurationStrategy,
            None,
        ),
    ],
    ids=["unhealthy-before-complete-and-timeout", "complete-shrink-before-timeout", "no-action-before-native-fallback"],
)
def test_scene_classification_preserves_evidence_priority(evidence, generated, expected):
    """Overlapping facts must select the same scene and leave input evidence unchanged."""
    config = ControllerConfig()
    ft = config.fault_tolerance_config
    ft.enable_fast_recovery = True
    ft.enable_dp_scale_down = True
    ft.enable_engine_relaunch = True
    context = replace(
        RecoveryContext(
            engine_fault_observed=True,
            collection_complete=True,
            collection_timed_out=True,
            pending_removed_ranks=(1,),
        ),
        **evidence,
    )
    plan = build_recovery_plan(config, context, generated, FaultLevel.L2)
    assert plan.strategy is expected
    expected_context = (
        replace(context, fallback_strategy=EngineRelaunchStrategy.__name__)
        if expected is DpScaleDownStrategy
        else context
    )
    assert plan.context == expected_context
    assert context.fallback_strategy == ""
