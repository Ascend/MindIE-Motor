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
from motor.controller.fault_tolerance.dp_scale_down import (
    FtPhase,
    get_ft_runtime_store,
)
from motor.controller.fault_tolerance.strategy.base import StrategyBase

logger = get_logger(__name__)


class InstanceReconfigurationStrategy(StrategyBase):
    """Isolate an instance and stop all of its Pods through the original recovery service."""

    def execute(self, instance_id: int) -> None:
        get_ft_runtime_store().transition(
            instance_id,
            phase=FtPhase.RECONFIGURING,
            serving_published=False,
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

        get_ft_runtime_store().prepare_after_instance_reconfiguration(instance_id)
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
                phase=FtPhase.RECONFIGURING,
                serving_published=False,
                last_error=error,
            )
            self.mark_failed()
            logger.error("Instance reconfiguration failed for %d: %s", instance_id, error)
        with self._lock:
            self._is_finished = True
