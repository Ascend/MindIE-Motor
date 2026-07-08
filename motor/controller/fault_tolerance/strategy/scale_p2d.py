# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import time
from enum import Enum
from dataclasses import dataclass, field

from motor.common.logger import get_logger
from motor.common.alarm.scale_p2d_event import ScaleP2DEvent, ScaleP2DReason
from motor.controller.fault_tolerance.strategy import StrategyBase
from motor.controller.core.instance_manager import InstanceManager
from motor.common.resources import Instance, PDRole, InsStatus
from motor.controller.fault_tolerance.fault_types import FaultLevel
from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient

logger = get_logger(__name__)


class RecoveryState(str, Enum):
    """States of the ScaleP2D recovery workflow."""

    INIT = "init"
    CHECKING = "checking"
    SELECTING = "selecting"
    KILLING = "killing"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class RecoveryContext:
    """Mutable context carried through a single ScaleP2D recovery run."""

    d_instance_id: int
    d_instance_job_name: str
    d_instance: Instance | None = None
    num_node_per_instance_P: int = 1
    num_node_per_instance_D: int = 1
    num_required_node: int = 1
    selected_p_instances: list[Instance] = field(default_factory=list)
    current_state: RecoveryState = RecoveryState.INIT
    last_error: str | None = None
    start_time: float = field(default_factory=time.time)


class ScaleP2DStrategy(StrategyBase):
    """
    ScaleP2D fault-tolerance strategy.

    When a Decode instance hits L3-L6 hardware faults, reclaim Prefill capacity
    by stopping selected Prefill instances so the Decode instance can recover.

    Recovery steps:
    1. Load the faulty Decode instance and count nodes that need replacement.
    2. Wait until the Decode instance is isolated (non-ACTIVE).
    3. Select Prefill instances to stop (selection algorithm is pluggable).
    4. Stop selected Prefill instances via NodeManager and release their nodes.
    """

    CHECK_D_INSTANCE_STATUS_INTERVAL = 3  # seconds between D-instance status polls

    def __init__(self) -> None:
        super().__init__()
        self.context: RecoveryContext | None = None
        self.d_instance_reinit_wait_timeout = self._resolve_d_instance_reinit_wait_timeout()

    @staticmethod
    def _resolve_d_instance_reinit_wait_timeout() -> int:
        # Local import to avoid circular dependency:
        # fault_tolerance/__init__ → fault_manager → strategy/__init__ → scale_p2d
        from motor.controller.fault_tolerance.fault_manager import FaultManager

        return FaultManager().config.fault_tolerance_config.scale_p2d_d_instance_reinit_wait_timeout

    def execute(self, instance_id: int) -> None:
        """
        Run the ScaleP2D recovery strategy for a faulty Decode instance.

        Args:
            instance_id: ID of the Decode instance to recover.
        """
        self.context = None
        try:
            d_instance = InstanceManager().get_instance(instance_id)
            if d_instance is None:
                logger.error(
                    "ScaleP2D aborted: D instance not found. instance_id=%d, "
                    "reason=instance_not_in_instance_manager, "
                    "check: 1. InstanceManager ETCD sync completed "
                    "2. instance not deleted",
                    instance_id,
                )
                return

            logger.info(
                "ScaleP2D strategy started. instance_id=%d, job_name=%s",
                d_instance.id,
                d_instance.job_name,
            )
            self.context = RecoveryContext(d_instance_id=d_instance.id, d_instance_job_name=d_instance.job_name)

            success = self._execute_recovery_flow()
            if success:
                self.context.current_state = RecoveryState.SUCCESS
                logger.info(
                    "ScaleP2D recovery succeeded. instance_id=%d, job_name=%s, killed_p_count=%d",
                    self.context.d_instance_id,
                    self.context.d_instance_job_name,
                    len(self.context.selected_p_instances),
                )
            else:
                if self.context.current_state != RecoveryState.SUCCESS:
                    self.context.current_state = RecoveryState.FAILED
                logger.error(
                    "ScaleP2D recovery failed. instance_id=%d, job_name=%s, "
                    "state=%s, last_error=%s, "
                    "check: 1. D instance is INACTIVE "
                    "2. cluster has enough Prefill nodes "
                    "3. NodeManager stop API is reachable",
                    self.context.d_instance_id,
                    self.context.d_instance_job_name,
                    self.context.current_state.value,
                    self.context.last_error or "unknown",
                )

        except Exception as e:
            logger.exception(
                "Unexpected error in ScaleP2D strategy. instance_id=%d",
                instance_id,
            )
            if self.context is not None:
                self.context.current_state = RecoveryState.FAILED
                self.context.last_error = str(e)

        finally:
            with self._lock:
                self._is_finished = True
            if self.context is not None:
                elapsed_time = time.time() - self.context.start_time
                logger.info(
                    "ScaleP2D strategy finished. instance_id=%d, job_name=%s, state=%s, elapsed_s=%.2f, last_error=%s",
                    self.context.d_instance_id,
                    self.context.d_instance_job_name,
                    self.context.current_state.value,
                    elapsed_time,
                    self.context.last_error or "none",
                )
                if self.context.current_state == RecoveryState.SUCCESS:
                    self._report_scale_p2d_event()
            else:
                logger.info(
                    "ScaleP2D strategy finished before context init. instance_id=%d",
                    instance_id,
                )

    def _report_scale_p2d_event(self) -> None:
        """Report one ScaleP2D event after the Decode instance recovery succeeds."""
        try:
            from motor.controller.observability.observability import Observability

            event = ScaleP2DEvent(
                reason_id=ScaleP2DReason.D_INSTANCE_RECOVERED_BY_SCALE_P2D,
                d_instance_id=self.context.d_instance_id,
                d_instance_job_name=self.context.d_instance_job_name,
                killed_p_instance_ids=[inst.id for inst in self.context.selected_p_instances],
            )
            Observability().add_alarm(event)
            logger.info(
                "Reported ScaleP2D recovery event. instance_id=%d, job_name=%s",
                self.context.d_instance_id,
                self.context.d_instance_job_name,
            )
        except Exception:
            logger.exception(
                "Failed to report ScaleP2D recovery event. instance_id=%d",
                self.context.d_instance_id if self.context else -1,
            )

    def _execute_recovery_flow(self) -> bool:
        """
        Run the full recovery pipeline.

        Returns:
            True if all steps succeed; False otherwise.
        """
        try:
            # Step 1: load Decode instance and resource requirement.
            if not self._get_d_instance():
                return False

            # Step 2: ensure Decode instance is isolated before preemption.
            if not self._check_d_instance_status():
                return False

            # Step 3: re-count faulty nodes with the latest FaultManager snapshot
            # after the wait window, so multi-node failures arriving late are included.
            if not self._refresh_faulty_node_count_before_kill():
                return False

            # Step 4: pick Prefill instances to stop.
            if not self._select_p_instances_to_kill():
                return False

            # Step 5: stop Prefill instances and release nodes.
            if not self._kill_and_release_p_instances():
                return False

            return True

        except Exception as e:
            logger.exception(
                "Recovery flow failed with exception. instance_id=%d, state=%s",
                self.context.d_instance_id,
                self.context.current_state.value,
            )
            self.context.last_error = f"Error in recovery flow: {e}"
            self.context.current_state = RecoveryState.FAILED
            return False

    def _get_d_instance(self) -> bool:
        """Load the faulty Decode instance and compute how many nodes are required."""
        self.context.current_state = RecoveryState.INIT
        logger.debug(
            "Fetching D instance info. instance_id=%d",
            self.context.d_instance_id,
        )

        try:
            d_instance = InstanceManager().get_instance(self.context.d_instance_id)

            if d_instance is None:
                self.context.last_error = f"D instance {self.context.d_instance_id} not found in InstanceManager"
                logger.error(
                    "Failed to get D instance. instance_id=%d, reason=instance_not_found, "
                    "check: instance exists in InstanceManager",
                    self.context.d_instance_id,
                )
                return False

            self.context.d_instance = d_instance
            self.context.num_node_per_instance_D = len(d_instance.get_node_managers())

            # Count nodes on this Decode instance with L3+ hardware faults.
            self.context.num_required_node = self._get_faulty_node_count(d_instance)
            logger.info(
                "D instance info loaded. instance_id=%d, job_name=%s, node_count=%d, faulty_node_count=%d",
                self.context.d_instance_id,
                d_instance.job_name,
                self.context.num_node_per_instance_D,
                self.context.num_required_node,
            )

            return True

        except Exception as e:
            logger.exception(
                "Failed to get D instance. instance_id=%d",
                self.context.d_instance_id,
            )
            self.context.last_error = f"Failed to get D instance: {e}"
            return False

    def _get_faulty_node_count(self, d_instance: Instance) -> int:
        """
        Count Decode nodes that need replacement (L3+ faults).

        Delegates to FaultManager.get_node_fault_levels() which returns the
        highest fault level per node for the given instance.
        Nodes not tracked by FaultManager are conservatively counted as faulty.
        """
        node_managers = d_instance.get_node_managers()
        if not node_managers:
            return 0

        try:
            # Local import to avoid circular dependency:
            # fault_tolerance/__init__ → fault_manager → strategy/__init__ → scale_p2d
            from motor.controller.fault_tolerance.fault_manager import FaultManager

            node_fault_levels = FaultManager().get_node_fault_levels(d_instance.id)

            if not node_fault_levels:
                logger.warning(
                    "FaultManager returned no node data for instance, "
                    "falling back to all nodes as faulty. instance_id=%d, node_count=%d",
                    d_instance.id,
                    len(node_managers),
                )
                return len(node_managers)

            faulty_node_count = sum(1 for level in node_fault_levels.values() if level >= FaultLevel.L3)

            logger.info(
                "Faulty node count computed. instance_id=%d, faulty=%d, total=%d",
                d_instance.id,
                faulty_node_count,
                len(node_fault_levels),
            )

            return faulty_node_count

        except Exception:
            logger.exception(
                "Failed to count faulty nodes, falling back to all nodes. instance_id=%d, "
                "node_count=%d, "
                "check: FaultManager is running and nodes data is valid",
                d_instance.id,
                len(node_managers),
            )
            return len(node_managers)

    def _refresh_faulty_node_count_before_kill(self) -> bool:
        """
        Re-count faulty Decode nodes immediately before Prefill selection.

        The initial count in _get_d_instance() may be stale when multiple nodes
        fail within the D-instance isolation wait window. Refresh here so the
        protect-D decision uses the latest hardware fault snapshot.
        """
        previous_required_node = self.context.num_required_node

        try:
            d_instance = InstanceManager().get_instance_by_job_name(self.context.d_instance_job_name)
            if d_instance is None:
                self.context.last_error = (
                    f"D instance not found for job_name {self.context.d_instance_job_name} before Prefill selection"
                )
                logger.error(
                    "Failed to refresh faulty node count. instance_id=%d, job_name=%s, reason=instance_not_found",
                    self.context.d_instance_id,
                    self.context.d_instance_job_name,
                )
                return False

            self.context.d_instance = d_instance
            self.context.d_instance_id = d_instance.id
            self.context.num_node_per_instance_D = len(d_instance.get_node_managers())
            self.context.num_required_node = self._get_faulty_node_count(d_instance)

            if self.context.num_required_node == 0:
                self.context.last_error = "No faulty Decode nodes remain, ScaleP2D preemption not needed"
                logger.info(
                    "ScaleP2D preemption skipped after refresh. instance_id=%d, job_name=%s, reason=no_faulty_nodes",
                    self.context.d_instance_id,
                    self.context.d_instance_job_name,
                )
                return False

            if self.context.num_required_node != previous_required_node:
                logger.info(
                    "Faulty node count updated after wait window. instance_id=%d, job_name=%s, "
                    "previous_required_nodes=%d, current_required_nodes=%d, d_node_count=%d",
                    self.context.d_instance_id,
                    self.context.d_instance_job_name,
                    previous_required_node,
                    self.context.num_required_node,
                    self.context.num_node_per_instance_D,
                )
            else:
                logger.info(
                    "Faulty node count unchanged after wait window. instance_id=%d, job_name=%s, "
                    "required_nodes=%d, d_node_count=%d",
                    self.context.d_instance_id,
                    self.context.d_instance_job_name,
                    self.context.num_required_node,
                    self.context.num_node_per_instance_D,
                )

            return True

        except Exception as e:
            logger.exception(
                "Failed to refresh faulty node count before ScaleP2D preemption. instance_id=%d",
                self.context.d_instance_id,
            )
            self.context.last_error = f"Failed to refresh faulty node count: {e}"
            return False

    def _check_d_instance_status(self) -> bool:
        """
        Wait until the Decode instance is isolated and safe for ScaleP2D preemption.

        The instance is looked up by job_name because redundant-node recovery may assign
        a new instance id while preserving the job_name.

        Returns False if the current D instance is INITIAL/ACTIVE (recovered, ScaleP2D
        not needed), missing, times out while still operational, or the strategy stops.
        """
        self.context.current_state = RecoveryState.CHECKING
        logger.info(
            "Checking D instance status before ScaleP2D. instance_id=%d, job_name=%s, timeout_s=%d",
            self.context.d_instance_id,
            self.context.d_instance_job_name,
            self.d_instance_reinit_wait_timeout,
        )

        try:
            start_time = time.time()
            poll_count = 0

            while time.time() - start_time < self.d_instance_reinit_wait_timeout:
                if self.event.is_set():
                    self.context.last_error = "Strategy stopped during D instance status check"
                    logger.warning(
                        "D instance status check interrupted. instance_id=%d, job_name=%s, reason=strategy_stopped",
                        self.context.d_instance_id,
                        self.context.d_instance_job_name,
                    )
                    return False

                d_instance = InstanceManager().get_instance_by_job_name(self.context.d_instance_job_name)
                if d_instance is None:
                    self.context.last_error = (
                        f"D instance not found for job_name {self.context.d_instance_job_name} during status check"
                    )
                    logger.error(
                        "D instance disappeared during status check. instance_id=%d, job_name=%s, "
                        "reason=instance_not_found",
                        self.context.d_instance_id,
                        self.context.d_instance_job_name,
                    )
                    return False

                if d_instance.status in (InsStatus.INITIAL, InsStatus.ACTIVE):
                    logger.info(
                        "D instance recovered, ScaleP2D not needed. trigger_instance_id=%d, "
                        "current_instance_id=%d, job_name=%s, status=%s",
                        self.context.d_instance_id,
                        d_instance.id,
                        self.context.d_instance_job_name,
                        d_instance.status.value,
                    )
                    return False

                poll_count += 1
                # Throttle debug logs: first poll and every 5th poll.
                if poll_count == 1 or poll_count % 5 == 0:
                    logger.debug(
                        "Waiting for D instance isolation. trigger_instance_id=%d, current_instance_id=%d, "
                        "job_name=%s, status=%s, poll_count=%d, elapsed_s=%.1f",
                        self.context.d_instance_id,
                        d_instance.id,
                        self.context.d_instance_job_name,
                        d_instance.status.value,
                        poll_count,
                        time.time() - start_time,
                    )
                time.sleep(self.CHECK_D_INSTANCE_STATUS_INTERVAL)

            d_instance = InstanceManager().get_instance_by_job_name(self.context.d_instance_job_name)
            if d_instance is None:
                self.context.last_error = (
                    f"D instance not found for job_name {self.context.d_instance_job_name} after wait"
                )
                logger.error(
                    "D instance not found after status wait. instance_id=%d, job_name=%s",
                    self.context.d_instance_id,
                    self.context.d_instance_job_name,
                )
                return False

            if d_instance.status not in (InsStatus.INITIAL, InsStatus.ACTIVE):
                logger.info(
                    "D instance ready for ScaleP2D. trigger_instance_id=%d, current_instance_id=%d, "
                    "job_name=%s, status=%s, waited_s=%.1f",
                    self.context.d_instance_id,
                    d_instance.id,
                    self.context.d_instance_job_name,
                    d_instance.status.value,
                    time.time() - start_time,
                )
                return True

            self.context.last_error = (
                f"D instance {self.context.d_instance_job_name} did not become INACTIVE "
                f"within {self.d_instance_reinit_wait_timeout}s"
            )
            logger.error(
                "D instance status check timed out. instance_id=%d, job_name=%s, "
                "current_instance_id=%d, status=%s, timeout_s=%d, "
                "check: 1. fault isolation has set D instance to INACTIVE "
                "2. InstanceManager status sync is not delayed",
                self.context.d_instance_id,
                self.context.d_instance_job_name,
                d_instance.id,
                d_instance.status.value,
                self.d_instance_reinit_wait_timeout,
            )
            return False

        except Exception as e:
            logger.exception(
                "Failed to check D instance status. instance_id=%d, job_name=%s",
                self.context.d_instance_id,
                self.context.d_instance_job_name,
            )
            self.context.last_error = f"Failed to check D instance status: {e}"
            return False

    def _select_p_instances_to_kill(self) -> bool:
        """
        Select Prefill instances to stop so enough nodes are freed for the Decode instance.

        Returns:
            True if a sufficient set of Prefill instances was selected.
        """
        self.context.current_state = RecoveryState.SELECTING
        logger.info(
            "Selecting P instances to kill. instance_id=%d, required_nodes=%d",
            self.context.d_instance_id,
            self.context.num_required_node,
        )

        try:
            all_p_instances = InstanceManager().get_instances_by_role(PDRole.ROLE_P)
            # only INITIAL/ACTIVE instances are still operational and reachable via stop API.
            operational_p_instances = [
                inst for inst in all_p_instances if inst.status in (InsStatus.INITIAL, InsStatus.ACTIVE)
            ]
            if not operational_p_instances:
                self.context.last_error = "No operational Prefill instances (INITIAL/ACTIVE) available"
                logger.error(
                    "No operational Prefill instances available. instance_id=%d, "
                    "total_p_count=%d, reason=no_operational_p_instances, "
                    "check: ROLE_P instances in INITIAL or ACTIVE status exist in the cluster",
                    self.context.d_instance_id,
                    len(all_p_instances),
                )
                return False

            if len(operational_p_instances) < len(all_p_instances):
                stale_inactive_ids = [
                    inst.id for inst in all_p_instances if inst.status not in (InsStatus.INITIAL, InsStatus.ACTIVE)
                ]
                logger.info(
                    "Skipped INACTIVE P instances pending InstanceManager purge. instance_id=%d, "
                    "total_p_count=%d, operational_count=%d, stale_inactive_ids=%s",
                    self.context.d_instance_id,
                    len(all_p_instances),
                    len(operational_p_instances),
                    stale_inactive_ids,
                )

            self.context.num_node_per_instance_P = len(operational_p_instances[0].get_node_managers())

            # num_required_node was set in _get_d_instance() via _get_faulty_node_count().
            # Reserve one Prefill instance; the rest contribute available nodes.
            num_available_node = self.context.num_node_per_instance_P * (len(operational_p_instances) - 1)
            logger.info(
                "P instance pool evaluated. instance_id=%d, p_instance_count=%d, "
                "operational_p_count=%d, nodes_per_p=%d, required_nodes=%d, available_nodes=%d",
                self.context.d_instance_id,
                len(all_p_instances),
                len(operational_p_instances),
                self.context.num_node_per_instance_P,
                self.context.num_required_node,
                num_available_node,
            )

            if num_available_node < self.context.num_required_node:
                self.context.last_error = "Not enough available prefill nodes to satisfy the requirement."
                logger.error(
                    "Insufficient Prefill nodes for ScaleP2D. instance_id=%d, "
                    "required_nodes=%d, available_nodes=%d, p_instance_count=%d",
                    self.context.d_instance_id,
                    self.context.num_required_node,
                    num_available_node,
                    len(operational_p_instances),
                )
                return False

            selected_instances = self._select_instances_algorithm(operational_p_instances)

            if not selected_instances:
                self.context.last_error = "Failed to select suitable P instances"
                logger.error(
                    "P instance selection returned empty. instance_id=%d, reason=selection_algorithm_failed",
                    self.context.d_instance_id,
                )
                return False

            self.context.selected_p_instances = selected_instances
            selected_ids = [inst.id for inst in selected_instances]
            logger.info(
                "P instances selected for kill. instance_id=%d, selected_count=%d, selected_ids=%s",
                self.context.d_instance_id,
                len(selected_instances),
                selected_ids,
            )

            return True

        except Exception as e:
            logger.exception(
                "Failed to select P instances. instance_id=%d",
                self.context.d_instance_id,
            )
            self.context.last_error = f"Failed to select P instances: {e}"
            return False

    def _select_instances_algorithm(
        self,
        available_instances: list[Instance],
    ) -> list[Instance]:
        """
        Pluggable Prefill instance selection (placeholder implementation).

        TODO: Implement a production selector, e.g.:
        - Score instances by load, priority, uptime, etc.

        Args:
            available_instances: Candidate Prefill instances in the cluster.

        Returns:
            Subset of instances to stop.
        """
        logger.warning(
            "Using placeholder P instance selection algorithm. instance_id=%d, reason=algorithm_not_implemented",
            self.context.d_instance_id,
        )

        required_num_instance_P = self.context.num_required_node // self.context.num_node_per_instance_P
        if self.context.num_required_node % self.context.num_node_per_instance_P != 0:
            required_num_instance_P += 1

        # Prefer INITIAL over ACTIVE, then sort by ID; production code should sort by cost score.
        def _selection_key(inst: Instance) -> tuple[int, int]:
            status_priority = 0 if inst.status == InsStatus.INITIAL else 1
            return (status_priority, inst.id)

        sorted_instances = sorted(available_instances, key=_selection_key)

        return sorted_instances[:required_num_instance_P]

    def _kill_and_release_p_instances(self) -> bool:
        """Stop selected Prefill instances via NodeManager and release their nodes."""
        self.context.current_state = RecoveryState.KILLING
        selected = self.context.selected_p_instances
        logger.info(
            "Starting P instance kill and release. instance_id=%d, p_count=%d",
            self.context.d_instance_id,
            len(selected),
        )

        killed_count = 0
        try:
            for p_instance in selected:
                logger.debug(
                    "Stopping P instance. d_instance_id=%d, p_instance_id=%d, p_job_name=%s",
                    self.context.d_instance_id,
                    p_instance.id,
                    p_instance.job_name,
                )

                for node_mgr in p_instance.get_node_managers():
                    response = NodeManagerApiClient.stop(node_mgr)
                    if not response:
                        self.context.last_error = (
                            f"Failed to stop P instance {p_instance.id} (pod_ip={node_mgr.pod_ip})"
                        )
                        logger.error(
                            "Failed to stop P instance node. d_instance_id=%d, "
                            "p_instance_id=%d, p_job_name=%s, pod_ip=%s, "
                            "check: 1. NodeManager process is alive "
                            "2. stop API network is reachable "
                            "3. the Pod is not deleted",
                            self.context.d_instance_id,
                            p_instance.id,
                            p_instance.job_name,
                            node_mgr.pod_ip,
                        )
                        return False

                killed_count += 1
                logger.debug(
                    "P instance stopped. d_instance_id=%d, p_instance_id=%d",
                    self.context.d_instance_id,
                    p_instance.id,
                )

            logger.info(
                "All selected P instances stopped. instance_id=%d, killed_count=%d, killed_ids=%s",
                self.context.d_instance_id,
                killed_count,
                [inst.id for inst in selected],
            )
            return True

        except Exception as e:
            logger.exception(
                "Failed to kill P instances. instance_id=%d, killed_so_far=%d",
                self.context.d_instance_id,
                killed_count,
            )
            self.context.last_error = f"Failed to kill P instances: {e}"
            return False

    def stop(self) -> None:
        """Signal the strategy thread to exit and mark execution as finished."""
        self.event.set()
        with self._lock:
            self._is_finished = True
        instance_id = self.context.d_instance_id if self.context else None
        logger.info(
            "ScaleP2D strategy stop requested. instance_id=%s",
            instance_id if instance_id is not None else "unknown",
        )
