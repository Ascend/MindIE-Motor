# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""One task owns collection, child execution and failure handoff."""

from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from motor.config.controller import ControllerConfig
from motor.controller.fault_tolerance.fault_types import FaultInfo
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, get_ft_runtime_store
from motor.controller.fault_tolerance.engine_recovery.contracts import (
    RecoveryContext,
    FailurePolicy,
    RecoveryAdvice,
    RecoveryPlan,
    RecoverySource,
)
from motor.controller.fault_tolerance.strategy import EngineRelaunchStrategy
from motor.controller.fault_tolerance.engine_recovery.runner import EngineRecoveryStrategy
from motor.controller.fault_tolerance.strategy.engine_ft.fast_recovery import (
    GenericFastRecoveryStrategy,
)


@pytest.fixture
def owner():
    store = get_ft_runtime_store()
    store.clear()
    store.transition(7, recovery_id="owner", phase=FtPhase.WAITING_ENGINE_FAULT)
    strategy = EngineRecoveryStrategy()
    strategy.recovery_id = "owner"
    strategy.bind_config(ControllerConfig())
    context = RecoveryContext(all_dp_unhealthy=True, engine_fault_observed=True)
    strategy.context_provider = Mock(return_value=context)
    fault = FaultInfo.from_exception(RuntimeError("collective"), engine_id=0, engine_status=2, instance_id=7)
    strategy.evidence_provider = Mock(return_value={(None, "rank-0"): (1, fault)})
    with (
        patch.object(GenericFastRecoveryStrategy, "_resolve_config", side_effect=lambda _, default: default),
        patch("motor.controller.core.instance_manager.InstanceManager") as im,
        patch(
            "motor.controller.fault_tolerance.engine_recovery.runner.ServingOverlay.pause_for_recovery",
            return_value=True,
        ),
    ):
        im.return_value.get_instance.return_value = Mock(id=7)
        yield strategy, context
    store.clear()


def test_native_handoff_keeps_evidence_for_fm_and_does_not_run_native_inline(owner):
    strategy, context = owner
    strategy.plan_provider = Mock(
        return_value=RecoveryPlan(RecoverySource.SOFTWARE, EngineRelaunchStrategy, context, None)
    )
    with patch.object(EngineRelaunchStrategy, "execute") as execute:
        strategy.execute(7)
    execute.assert_not_called()
    assert strategy.is_failed()
    assert strategy.failure_fallback == "EngineRelaunchStrategy"
    assert strategy.completed_strategy_name == "EngineRecoveryStrategy"
    assert strategy.recovery_result.failure_policy == FailurePolicy.PREPARATION
    assert strategy.recovery_result.advice == RecoveryAdvice.RELAUNCH
    assert get_ft_runtime_store().get(7)["phase"] == FtPhase.WAITING_ENGINE_FAULT


def test_child_execution_shares_owner_and_forwards_result(owner):
    policy = GenericFastRecoveryStrategy
    strategy, context = owner
    strategy.plan_provider = Mock(return_value=RecoveryPlan(RecoverySource.SOFTWARE, policy, context, None))

    def execute(child, instance_id):
        assert instance_id == 7
        assert child.recovery_id == "owner"
        assert strategy.selected_strategy is child
        strategy.stop()
        assert child.event.is_set()
        child.failure_fallback = "EngineRelaunchStrategy"
        child.mark_failed()

    with patch.object(policy, "execute", execute):
        strategy.execute(7)
    assert strategy.completed_strategy_name == policy.__name__
    assert strategy.is_failed()
    assert strategy.failure_fallback == "EngineRelaunchStrategy"
    assert strategy.consumed_evidence == strategy.evidence_provider.return_value
    assert strategy.recovery_result == strategy.selected_strategy.recovery_result
    assert strategy.recovery_result.protect_relaunch
    assert strategy.recovery_result.advice == RecoveryAdvice.RELAUNCH


@pytest.mark.parametrize("hardware", [False, True])
def test_collection_without_timestamp_still_has_fixed_deadline(owner, hardware):
    strategy, _context = owner
    context = RecoveryContext(
        hardware_fault_observed=hardware,
        hardware_affected_ranks=(0,) if hardware else (),
        engine_fault_observed=not hardware,
    )
    strategy._controller_config.fault_tolerance_config.fault_collection_timeout_seconds = 5
    strategy.context_provider.return_value = context
    clock = [100.0]

    def plan(snapshot):
        target = (
            EngineRelaunchStrategy
            if snapshot.hardware_wait_timed_out or snapshot.collection_timed_out
            else EngineRecoveryStrategy
        )
        return RecoveryPlan(RecoverySource.HARDWARE, target, snapshot, None)

    strategy.plan_provider = plan
    with (
        patch("motor.controller.fault_tolerance.engine_recovery.runner.time.monotonic", side_effect=lambda: clock[0]),
        patch.object(strategy.event, "wait", side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)),
        patch("motor.controller.fault_tolerance.engine_recovery.runner.probe_recovery_rank"),
    ):
        strategy.execute(7)
    assert strategy.is_failed()
    assert strategy.failure_fallback == "EngineRelaunchStrategy"
    assert clock[0] < 106.0


def test_common_collection_does_not_read_shrink_tuning(owner):
    strategy, _context = owner
    context = RecoveryContext(engine_fault_observed=True)
    strategy.context_provider.side_effect = [
        RecoveryContext(hardware_fault_observed=True),
        context,
        context,
    ]
    strategy.plan_provider = Mock(
        return_value=RecoveryPlan(RecoverySource.SOFTWARE, EngineRecoveryStrategy, context, None)
    )
    ft = strategy._controller_config.fault_tolerance_config
    ft.dp_scale_down_config = None
    ft.recovery_request_timeout_sec = 4
    ft.recovery_preparation_poll_interval_sec = 0.25
    with (
        patch("motor.controller.fault_tolerance.engine_recovery.runner.probe_recovery_rank") as probe,
        patch.object(
            strategy.event, "wait", side_effect=lambda _delay: strategy.stop() if wait.call_count == 3 else None
        ) as wait,
    ):
        strategy.execute(7)
    probe.assert_called_once()
    assert probe.call_args.args[2] == 4
    assert wait.call_count == 3
    assert all(call.args == (0.25,) for call in wait.call_args_list)


def test_owner_executes_renamed_shrink_through_declared_capabilities(owner):
    from motor.controller.fault_tolerance.strategy import DpScaleDownStrategy

    strategy, context = owner

    class RenamedShrink(DpScaleDownStrategy):
        def execute(self, instance_id):
            assert instance_id == 7
            assert self.strategy_context.collection_complete
            assert self.strategy_context.readiness_deadline is not None
            assert self.context_provider is strategy.context_provider
            assert self.evidence_provider is strategy.evidence_provider

    strategy.context_provider.side_effect = [context, replace(context, collection_complete=True)]
    strategy.plan_provider = Mock(
        side_effect=lambda snapshot: RecoveryPlan(
            RecoverySource.SOFTWARE,
            RenamedShrink if snapshot.collection_complete else EngineRecoveryStrategy,
            snapshot,
            None,
        )
    )
    with (
        patch("motor.controller.fault_tolerance.engine_recovery.runner.probe_recovery_rank"),
        patch.object(strategy.event, "wait"),
    ):
        strategy.execute(7)
    assert strategy.plan_provider.call_count == 2
    assert isinstance(strategy.selected_strategy, RenamedShrink)
    assert not strategy.is_failed()


def test_collection_self_clear_republishes_without_running_child(owner):
    strategy, _context = owner
    context = RecoveryContext()
    strategy.context_provider.return_value = context
    strategy.plan_provider = Mock(return_value=RecoveryPlan(RecoverySource.SOFTWARE, None, context, None))
    with patch("motor.controller.fault_tolerance.engine_recovery.runner.ServingOverlay.publish", return_value=True):
        strategy.execute(7)
    assert strategy.selected_strategy is None
    assert not strategy.is_failed()
    assert strategy.consumed_evidence == {}
    assert get_ft_runtime_store().get(7)["phase"] == FtPhase.NORMAL
