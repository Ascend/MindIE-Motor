# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Engine retry strategy for a domain-wide transient DP failure."""

import time
import uuid
from dataclasses import dataclass

from motor.common.logger import get_logger
from motor.config.controller import ControllerConfig
from motor.controller.fault_tolerance.fault_types import FaultLevel
from motor.controller.fault_tolerance.strategy.base import StrategyBase

logger = get_logger(__name__)


@dataclass(frozen=True)
class FastRecoveryContext:
    """Fault facts exposed to a future fast-recovery implementation."""

    fault_level: FaultLevel
    fault_code: int
    source: str


class EngineFastRecoveryStrategy(StrategyBase):
    """Issue ``retry`` when every DP rank reported UNHEALTHY and none is DEAD."""

    @classmethod
    def is_applicable(
        cls,
        instance_id: int,
        context: FastRecoveryContext,
        config: ControllerConfig,
    ) -> bool:
        """Return whether the external fast-recovery implementation can handle this fault."""
        del instance_id, context, config
        return False

    def execute(self, instance_id: int) -> None:
        from motor.controller.fault_tolerance.strategy.dp_scale_down import (
            DpScaleDownStrategy,
            _get_instance,
        )

        try:
            if self.event.is_set():
                return
            if self._controller_config is None:
                raise RuntimeError("strategy configuration is not bound")
            config = self._controller_config.fault_tolerance_config
            scale_down_config = config.dp_scale_down_config
            instance = _get_instance(instance_id)
            if instance is None:
                raise RuntimeError("instance not found")
            groups = DpScaleDownStrategy._build_node_manager_groups(instance, set())
            request_id = "retry-%s" % uuid.uuid4().hex
            timeout = scale_down_config.request_timeout_sec
            DpScaleDownStrategy._guard_node_managers(
                groups,
                request_id,
                DpScaleDownStrategy._guard_lease_sec(config),
                timeout,
            )
            try:
                statuses = DpScaleDownStrategy._query_via_node_managers(groups, timeout)
                readiness, failed_status = DpScaleDownStrategy._apply_readiness(statuses)
                if readiness != "ready":
                    raise RuntimeError(
                        DpScaleDownStrategy._status_failure("all DP ranks are not retry-ready", failed_status)
                    )
                DpScaleDownStrategy._apply_via_node_managers(groups, {}, request_id, timeout, instruction="retry")
                deadline = time.monotonic() + scale_down_config.execution_deadline_sec
                while not self.event.is_set() and time.monotonic() < deadline:
                    statuses = DpScaleDownStrategy._query_via_node_managers(groups, timeout)
                    outcome, failed_status = DpScaleDownStrategy._scale_down_outcome(statuses)
                    if outcome == "succeeded":
                        logger.info("Engine retry succeeded for all DP ranks in instance %d", instance_id)
                        return
                    if outcome == "failed":
                        raise RuntimeError(DpScaleDownStrategy._status_failure("engine retry failed", failed_status))
                    self.event.wait(scale_down_config.poll_interval_sec)
                raise RuntimeError("engine retry recovery deadline exceeded")
            finally:
                DpScaleDownStrategy._finalize_node_managers(groups, set(), request_id, False, None, timeout)
        except Exception as error:
            logger.error("Engine retry failed for instance %d: %s", instance_id, error)
            self.mark_failed()
        finally:
            with self._lock:
                self._is_finished = True

    def stop(self) -> None:
        self.event.set()
