# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Bridge failed fast recovery to Motor's existing whole-instance recovery flow."""

from motor.common.logger import get_logger
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, get_ft_runtime_store
from motor.controller.fault_tolerance.engine_recovery.contracts import FailurePolicy, RecoveryAdvice
from motor.controller.fault_tolerance.strategy.base import StrategyBase

logger = get_logger(__name__)


class InstanceReconfigurationStrategy(StrategyBase):
    """Isolate an instance and stop all of its Pods through the original recovery service."""

    recovery_action = "RECONFIGURE"
    failure_policy = FailurePolicy.RECONFIGURATION
    recovery_target = RecoveryAdvice.RECONFIGURE

    def execute(self, instance_id: int) -> None:
        get_ft_runtime_store().transition(
            instance_id,
            expected_recovery_id=self.recovery_id or None,
            phase=FtPhase.RECONFIGURING,
            action="RECONFIGURE",
            stage="dispatching",
            serving_published=False,
            reconfiguration_dispatched=False,
            dp_master_rank=None,
            dp_master_ip=None,
        )

        try:
            from motor.controller.core.recovery_service import (
                terminate_instance_for_recovery,
            )

            succeeded = terminate_instance_for_recovery(
                instance_id,
                reason="dp_scale_down_reconfiguration",
            )
        except Exception as e:
            self._finish(instance_id, failed=True, error=str(e))
            return

        if not succeeded:
            self._finish(
                instance_id,
                failed=True,
                error="whole-instance recovery dispatch failed",
            )
            return

        get_ft_runtime_store().mark_instance_reconfiguration_dispatched(instance_id, self.recovery_id or None)
        self._finish(instance_id, failed=False)

    def stop(self) -> None:
        self.event.set()

    def _finish(
        self,
        instance_id: int,
        *,
        failed: bool,
        error: str | None = None,
    ) -> None:
        if failed:
            get_ft_runtime_store().transition(
                instance_id,
                expected_recovery_id=self.recovery_id or None,
                phase=FtPhase.RECONFIGURING,
                stage="failed",
                serving_published=False,
                last_error=error,
            )
            self.mark_failed()
            logger.error("Instance reconfiguration failed for %d: %s", instance_id, error)
        with self._lock:
            self._is_finished = True
