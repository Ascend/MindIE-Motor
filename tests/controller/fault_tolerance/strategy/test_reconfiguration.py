# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for the bridge to Motor's original whole-instance recovery flow."""

from unittest.mock import patch

from motor.controller.fault_tolerance.dp_scale_down import (
    FtPhase,
    FtRuntime,
    get_ft_runtime_store,
)
from motor.controller.fault_tolerance.strategy.reconfiguration import (
    InstanceReconfigurationStrategy,
)


def test_success_reuses_original_recovery_and_marks_dispatch_without_declaring_normal():
    store = get_ft_runtime_store()
    store.clear()
    store.put(
        FtRuntime(
            instance_id=5,
            phase=FtPhase.RECONFIGURING,
            dead_committed=[1],
            pending_removed_ranks=[2],
            serving_published=False,
        )
    )
    strategy = InstanceReconfigurationStrategy()

    with patch(
        "motor.controller.core.recovery_service.terminate_instance_for_recovery",
        return_value=True,
    ) as terminate:
        strategy.execute(5)

    terminate.assert_called_once_with(
        5,
        reason="dp_scale_down_reconfiguration",
    )
    runtime = store.get(5)
    assert strategy.is_finished() is True
    assert strategy.is_failed() is False
    assert runtime["original_dp_ranks"] == []
    assert runtime["gate"] == {"allowed": False, "reason_codes": []}
    assert runtime["phase"] == FtPhase.RECONFIGURING.value
    assert runtime["reconfiguration_dispatched"] is True
    assert runtime["dead_committed"] == []
    assert runtime["pending_removed_ranks"] == []
    assert runtime["serving_published"] is False
    assert runtime["can_serve"] is False
    store.clear()


def test_failed_original_recovery_remains_reconfiguring_without_using_pod_lifecycle():
    store = get_ft_runtime_store()
    store.clear()
    strategy = InstanceReconfigurationStrategy()

    with (
        patch(
            "motor.controller.core.recovery_service.terminate_instance_for_recovery",
            return_value=False,
        ),
        patch("motor.controller.fault_tolerance.pod_lifecycle.PodLifecycle.stop_recyclable_pods") as stop_pods,
    ):
        strategy.execute(6)

    runtime = store.get(6)
    assert strategy.is_finished() is True
    assert strategy.is_failed() is True
    assert runtime["phase"] == FtPhase.RECONFIGURING.value
    assert runtime["last_error"] == "whole-instance recovery dispatch failed"
    assert runtime["can_serve"] is False
    stop_pods.assert_not_called()
    store.clear()
