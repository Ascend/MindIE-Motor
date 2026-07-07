# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Developer tests (DT) for ScaleP2DStrategy.

Coverage blocks:
1. execute() entry and completion
2. _get_d_instance / _get_faulty_node_count
3. _check_d_instance_status
4. _select_p_instances_to_kill / _select_instances_algorithm
5. _kill_and_release_p_instances
6. stop() and helpers
"""

from unittest.mock import Mock, patch

import pytest

from motor.common.resources import InsStatus, NodeManagerInfo, PDRole
from motor.common.resources.instance import Instance
from motor.controller.fault_tolerance.fault_manager import FaultManager
from motor.controller.fault_tolerance.fault_types import FaultLevel
from motor.controller.fault_tolerance.strategy.scale_p2d import (
    RecoveryContext,
    RecoveryState,
    ScaleP2DStrategy,
)


def _decode_instance(
    *,
    instance_id: int = 1,
    job_name: str = "decode-1",
    status: InsStatus = InsStatus.INACTIVE,
    node_managers: list | None = None,
) -> Mock:
    inst = Mock(spec=Instance)
    inst.id = instance_id
    inst.job_name = job_name
    inst.status = status
    inst.role = PDRole.ROLE_D
    inst.get_node_managers.return_value = node_managers or []
    return inst


def _prefill_instance(
    instance_id: int,
    node_count: int = 1,
    status: InsStatus = InsStatus.ACTIVE,
) -> Mock:
    inst = Mock(spec=Instance)
    inst.id = instance_id
    inst.job_name = f"prefill-{instance_id}"
    inst.status = status
    inst.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip=f"10.0.0.{instance_id}", port="8080") for _ in range(node_count)
    ]
    return inst


@pytest.fixture
def strategy() -> ScaleP2DStrategy:
    return ScaleP2DStrategy()


@pytest.fixture
def recovery_context() -> RecoveryContext:
    return RecoveryContext(d_instance_id=1, d_instance_job_name="decode-1")


@pytest.fixture(autouse=True)
def clear_fault_manager_singleton():
    """Avoid FaultManager singleton leakage across DT cases."""
    from motor.common.utils.singleton import ThreadSafeSingleton

    yield
    if FaultManager in ThreadSafeSingleton._instances:
        ThreadSafeSingleton._instances[FaultManager].stop()
        del ThreadSafeSingleton._instances[FaultManager]


@pytest.fixture
def bind_context(strategy, recovery_context):
    strategy.context = recovery_context
    return strategy


# =============================================================================
# 1. execute()
# =============================================================================


def test_execute_aborts_when_d_instance_not_found(strategy):
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instance.return_value = None
        strategy.execute(1)

    assert strategy.is_finished()
    assert strategy.context is None


def test_execute_succeeds_when_recovery_flow_passes(strategy):
    d_inst = _decode_instance(node_managers=[NodeManagerInfo(pod_ip="1.1.1.1", port="8080")])
    p_inst = _prefill_instance(2)

    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im = mock_im_cls.return_value
        mock_im.get_instance.return_value = d_inst
        mock_im.get_instances_by_role.return_value = [p_inst, _prefill_instance(3)]

        with patch.object(strategy, "_get_faulty_node_count", return_value=1):
            with patch.object(strategy, "_check_d_instance_status", return_value=True):
                with patch.object(
                    strategy,
                    "_select_instances_algorithm",
                    return_value=[p_inst],
                ):
                    with patch(
                        "motor.controller.fault_tolerance.strategy.scale_p2d.NodeManagerApiClient.stop",
                        return_value=True,
                    ):
                        strategy.execute(1)

    assert strategy.is_finished()
    assert strategy.context.current_state == RecoveryState.SUCCESS


def test_execute_fails_when_d_instance_still_active(strategy):
    """D instance has recovered (ACTIVE) before ScaleP2D preemption — strategy should fail."""
    d_inst = _decode_instance(status=InsStatus.ACTIVE)

    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im = mock_im_cls.return_value
        mock_im.get_instance.return_value = d_inst
        # _check_d_instance_status looks up the D instance by job_name;
        # returning the ACTIVE instance causes it to abort immediately.
        mock_im.get_instance_by_job_name.return_value = d_inst
        with patch.object(strategy, "_get_faulty_node_count", return_value=0):
            with patch.object(strategy, "_check_d_instance_status", return_value=False):
                strategy.execute(1)

    assert strategy.is_finished()
    assert strategy.context.current_state == RecoveryState.FAILED


# =============================================================================
# 2. _get_d_instance / _get_faulty_node_count
# =============================================================================


def test_get_d_instance_not_found(bind_context):
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instance.return_value = None
        assert bind_context._get_d_instance() is False

    assert "not found" in bind_context.context.last_error


def test_get_d_instance_success(bind_context):
    d_inst = _decode_instance(node_managers=[NodeManagerInfo(pod_ip="1.1.1.1", port="8080")])
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instance.return_value = d_inst
        with patch.object(bind_context, "_get_faulty_node_count", return_value=2):
            assert bind_context._get_d_instance() is True

    assert bind_context.context.d_instance is d_inst
    assert bind_context.context.num_required_node == 2


def test_get_faulty_node_count_returns_zero_without_nodes(bind_context):
    d_inst = _decode_instance()
    assert bind_context._get_faulty_node_count(d_inst) == 0


def test_get_faulty_node_count_counts_l3_fault(bind_context):
    d_inst = _decode_instance(node_managers=[NodeManagerInfo(pod_ip="1.1.1.1", port="8080")])

    with patch("motor.controller.fault_tolerance.fault_manager.FaultManager") as mock_fm_cls:
        mock_fm = mock_fm_cls.return_value
        mock_fm.get_node_fault_levels.return_value = {"node-0": FaultLevel.L3}
        assert bind_context._get_faulty_node_count(d_inst) == 1


def test_get_faulty_node_count_healthy_nodes_not_counted(bind_context):
    d_inst = _decode_instance(node_managers=[NodeManagerInfo(pod_ip="1.1.1.1", port="8080")])

    with patch("motor.controller.fault_tolerance.fault_manager.FaultManager") as mock_fm_cls:
        mock_fm = mock_fm_cls.return_value
        mock_fm.get_node_fault_levels.return_value = {"node-0": FaultLevel.L2}
        assert bind_context._get_faulty_node_count(d_inst) == 0


def test_get_faulty_node_count_falls_back_when_no_node_data(bind_context):
    d_inst = _decode_instance(node_managers=[NodeManagerInfo(pod_ip="9.9.9.9", port="8080")])

    with patch("motor.controller.fault_tolerance.fault_manager.FaultManager") as mock_fm_cls:
        mock_fm = mock_fm_cls.return_value
        mock_fm.get_node_fault_levels.return_value = {}
        assert bind_context._get_faulty_node_count(d_inst) == 1


def test_get_faulty_node_count_fallback_on_exception(bind_context):
    d_inst = _decode_instance(
        node_managers=[
            NodeManagerInfo(pod_ip="1.1.1.1", port="8080"),
            NodeManagerInfo(pod_ip="1.1.1.2", port="8080"),
        ]
    )

    with patch(
        "motor.controller.fault_tolerance.fault_manager.FaultManager",
        side_effect=RuntimeError("fm unavailable"),
    ):
        assert bind_context._get_faulty_node_count(d_inst) == 2


# =============================================================================
# 3. _check_d_instance_status
# =============================================================================


def test_check_d_instance_status_rejects_active(bind_context):
    d_inst = _decode_instance(status=InsStatus.ACTIVE)
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instance_by_job_name.return_value = d_inst
        assert bind_context._check_d_instance_status() is False

    assert bind_context.context.current_state == RecoveryState.CHECKING


def test_check_d_instance_status_accepts_inactive(bind_context):
    d_inst = _decode_instance(status=InsStatus.INACTIVE)
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instance_by_job_name.return_value = d_inst
        with patch.object(bind_context, "d_instance_reinit_wait_timeout", 0):
            assert bind_context._check_d_instance_status() is True


def test_check_d_instance_status_rejects_when_no_instance_by_job_name(bind_context):
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instance_by_job_name.return_value = None
        with patch.object(bind_context, "d_instance_reinit_wait_timeout", 0):
            assert bind_context._check_d_instance_status() is False

    assert "not found for job_name" in bind_context.context.last_error


def test_check_d_instance_status_rejects_recovered_instance_with_new_id(bind_context):
    recovered_inst = _decode_instance(instance_id=5, status=InsStatus.ACTIVE)
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instance_by_job_name.return_value = recovered_inst
        assert bind_context._check_d_instance_status() is False


def test_check_d_instance_status_interrupted_by_stop(bind_context):
    d_inst = _decode_instance(status=InsStatus.INACTIVE)
    bind_context.event.set()
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instance_by_job_name.return_value = d_inst
        assert bind_context._check_d_instance_status() is False


def test_check_d_instance_status_times_out_while_active(bind_context):
    d_inst = _decode_instance(status=InsStatus.ACTIVE)
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instance_by_job_name.return_value = d_inst
        with patch.object(bind_context, "d_instance_reinit_wait_timeout", 0):
            assert bind_context._check_d_instance_status() is False

    assert "did not become INACTIVE" in bind_context.context.last_error


# =============================================================================
# 4. _select_p_instances_to_kill / _select_instances_algorithm
# =============================================================================


def test_select_p_instances_fails_when_no_prefill(bind_context):
    bind_context.context.num_required_node = 1
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instances_by_role.return_value = []
        assert bind_context._select_p_instances_to_kill() is False


def test_select_p_instances_fails_when_only_inactive_prefill(bind_context):
    bind_context.context.num_required_node = 1
    p_instances = [
        _prefill_instance(2, status=InsStatus.INACTIVE),
        _prefill_instance(3, status=InsStatus.INACTIVE),
    ]
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instances_by_role.return_value = p_instances
        assert bind_context._select_p_instances_to_kill() is False

    assert "No operational Prefill instances" in bind_context.context.last_error


def test_select_p_instances_fails_when_insufficient_nodes(bind_context):
    bind_context.context.num_required_node = 10
    p_instances = [_prefill_instance(2), _prefill_instance(3)]
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instances_by_role.return_value = p_instances
        assert bind_context._select_p_instances_to_kill() is False


def test_select_p_instances_success(bind_context):
    bind_context.context.num_required_node = 1
    p_instances = [_prefill_instance(2), _prefill_instance(3)]
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instances_by_role.return_value = p_instances
        with patch.object(
            bind_context,
            "_select_instances_algorithm",
            return_value=[p_instances[0]],
        ):
            assert bind_context._select_p_instances_to_kill() is True

    assert bind_context.context.selected_p_instances == [p_instances[0]]


def test_select_p_instances_skips_inactive(bind_context):
    bind_context.context.num_required_node = 1
    inactive = _prefill_instance(2, status=InsStatus.INACTIVE)
    active = _prefill_instance(3, status=InsStatus.ACTIVE)
    initial = _prefill_instance(4, status=InsStatus.INITIAL)
    p_instances = [inactive, active, initial]
    with patch("motor.controller.fault_tolerance.strategy.scale_p2d.InstanceManager") as mock_im_cls:
        mock_im_cls.return_value.get_instances_by_role.return_value = p_instances
        assert bind_context._select_p_instances_to_kill() is True

    assert bind_context.context.selected_p_instances == [initial]


def test_select_instances_algorithm_picks_minimum_instances(bind_context):
    bind_context.context.num_required_node = 3
    bind_context.context.num_node_per_instance_P = 2
    instances = [_prefill_instance(5), _prefill_instance(2), _prefill_instance(8)]

    selected = bind_context._select_instances_algorithm(instances)

    assert [inst.id for inst in selected] == [2, 5]


def test_select_instances_algorithm_prefers_initial_over_active(bind_context):
    bind_context.context.num_required_node = 1
    bind_context.context.num_node_per_instance_P = 1
    instances = [
        _prefill_instance(2, status=InsStatus.ACTIVE),
        _prefill_instance(3, status=InsStatus.INITIAL),
        _prefill_instance(4, status=InsStatus.ACTIVE),
    ]

    selected = bind_context._select_instances_algorithm(instances)

    assert [inst.id for inst in selected] == [3]


def test_select_instances_algorithm_prefers_lower_id_among_initial(bind_context):
    bind_context.context.num_required_node = 2
    bind_context.context.num_node_per_instance_P = 1
    instances = [
        _prefill_instance(5, status=InsStatus.INITIAL),
        _prefill_instance(2, status=InsStatus.INITIAL),
        _prefill_instance(8, status=InsStatus.ACTIVE),
    ]

    selected = bind_context._select_instances_algorithm(instances)

    assert [inst.id for inst in selected] == [2, 5]


# =============================================================================
# 5. _kill_and_release_p_instances
# =============================================================================


def test_kill_and_release_success(bind_context):
    p_inst = _prefill_instance(2)
    bind_context.context.selected_p_instances = [p_inst]

    with patch(
        "motor.controller.fault_tolerance.strategy.scale_p2d.NodeManagerApiClient.stop",
        return_value=True,
    ):
        assert bind_context._kill_and_release_p_instances() is True


def test_kill_and_release_fails_when_stop_returns_false(bind_context):
    p_inst = _prefill_instance(2)
    bind_context.context.selected_p_instances = [p_inst]

    with patch(
        "motor.controller.fault_tolerance.strategy.scale_p2d.NodeManagerApiClient.stop",
        return_value=False,
    ):
        assert bind_context._kill_and_release_p_instances() is False

    assert "Failed to stop" in bind_context.context.last_error


# =============================================================================
# 6. stop()
# =============================================================================


def test_stop_sets_event_and_finished(strategy, recovery_context):
    strategy.context = recovery_context
    strategy.stop()

    assert strategy.event.is_set()
    assert strategy.is_finished()
