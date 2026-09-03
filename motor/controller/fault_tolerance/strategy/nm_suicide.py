# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Stop every NodeManager of an instance so the whole instance exits together."""

from motor.common.logger import get_logger
from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient, NodeManagerStopStatus
from motor.controller.core.instance_manager import InstanceManager
from motor.controller.fault_tolerance.strategy.base import StrategyBase

logger = get_logger(__name__)


class NmSuicideStrategy(StrategyBase):
    """Stop every NodeManager of the isolated instance via ``/node-manager/stop``.

    Covers A2 PD-isolation Prefill / Decode L6 and multi-pod union L6.
    Timeout / connection refused counts as already exiting and is not a failure.
    HTTP 5xx and other dispatch errors call ``mark_failed`` so the strategy
    center can retry or escalate. A superseded instance id is a no-op.
    """

    def execute(self, instance_id: int) -> None:
        try:
            instance_manager = InstanceManager()
            instance = instance_manager.get_instance(instance_id)
            if instance is None:
                logger.error(
                    "NmSuicide aborted: instance not found. instance_id=%d",
                    instance_id,
                )
                return

            current = instance_manager.get_instance_by_job_name(instance.job_name)
            current_id = getattr(current, "id", None) if current is not None else None
            if isinstance(current_id, int) and current_id != instance.id:
                logger.info(
                    "NmSuicide skipped stale instance. trigger_id=%d, current_id=%d, job_name=%s",
                    instance.id,
                    current_id,
                    instance.job_name,
                )
                return

            node_managers = instance.get_node_managers()
            logger.info(
                "NmSuicide started. instance_id=%d, job_name=%s, role=%s, nm_count=%d",
                instance.id,
                instance.job_name,
                getattr(instance.role, "value", instance.role),
                len(node_managers),
            )
            if not node_managers:
                logger.error(
                    "NmSuicide aborted: no NodeManagers. instance_id=%d, job_name=%s",
                    instance.id,
                    instance.job_name,
                )
                return

            unreachable = 0
            dispatch_failed = False
            for node_mgr in node_managers:
                status = NodeManagerApiClient.stop_status(node_mgr)
                if status == NodeManagerStopStatus.OK:
                    continue
                if status == NodeManagerStopStatus.UNREACHABLE:
                    unreachable += 1
                    logger.warning(
                        "NmSuicide NodeManager already unreachable, treating as exiting. "
                        "instance_id=%d, job_name=%s, pod_ip=%s",
                        instance.id,
                        instance.job_name,
                        node_mgr.pod_ip,
                    )
                    continue
                dispatch_failed = True
                logger.error(
                    "NmSuicide NodeManager stop failed. instance_id=%d, job_name=%s, pod_ip=%s",
                    instance.id,
                    instance.job_name,
                    node_mgr.pod_ip,
                )

            if dispatch_failed:
                self.mark_failed()
                logger.error(
                    "NmSuicide finished with stop failures. instance_id=%d, job_name=%s, nm_count=%d, unreachable=%d",
                    instance.id,
                    instance.job_name,
                    len(node_managers),
                    unreachable,
                )
            else:
                logger.info(
                    "NmSuicide finished. instance_id=%d, job_name=%s, nm_count=%d, unreachable=%d",
                    instance.id,
                    instance.job_name,
                    len(node_managers),
                    unreachable,
                )
        except Exception:
            logger.exception("NmSuicide unexpected error. instance_id=%d", instance_id)
        finally:
            with self._lock:
                self._is_finished = True

    def stop(self) -> None:
        self.event.set()
        logger.info("NmSuicide strategy stop requested")
