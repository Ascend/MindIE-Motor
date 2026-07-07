# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Resource management mixin for FaultManager: node sync, ownership swap, and resource monitoring.

All attributes referenced via self (nodes, instances, lock, k8s_client, resource_monitors, etc.)
are provided by FaultManager.__init__, not redeclared here.
"""

from motor.common.logger import get_logger
from motor.common.resources import InsStatus, ReadOnlyInstance
from motor.controller.fault_tolerance.fault_types import (
    FaultCategory,
    FaultInfo,
    FaultLevel,
    HardwareFaultType,
    InstanceMetadata,
    NodeMetadata,
    NodeStatus,
    OriginFaultLevel,
    SpecialFaultCode,
)
from motor.controller.fault_tolerance.k8s.resource_monitor import ResourceMonitor

logger = get_logger(__name__)


class _ResourceManagerMixin:
    """Node/instance sync, ownership swap, and resource monitoring for FaultManager."""

    # --- Node name resolution ---

    def _build_node_name_mapping(self, node_managers: list) -> tuple[dict[str, str], set[str]]:
        """Build pod_ip->node_name and current_node_names from node managers."""
        pod_to_node_name: dict[str, str] = {}
        current_node_names: set[str] = set()

        for node_mgr in node_managers:
            node_name = self.k8s_client.get_node_hostname_by_pod_ip(node_mgr.pod_ip)
            if not node_name:
                logger.warning("Failed to resolve node name for pod_ip %s", node_mgr.pod_ip)
                continue
            pod_to_node_name[node_mgr.pod_ip] = node_name
            current_node_names.add(node_name)

        return pod_to_node_name, current_node_names

    # --- Node metadata helpers ---

    def _ensure_node_metadata(
        self,
        node_name: str,
        pod_ip: str,
        instance_id: int,
        job_name: str = "",
    ) -> tuple[NodeMetadata, bool]:
        """Ensure a NodeMetadata entry exists for the given node_name.

        If the node already exists in self.nodes, add this instance to its
        per-instance maps.  If it does not exist, create a new NodeMetadata.

        Args:
            node_name: Kubernetes node name (stable identifier, never changes).
            pod_ip: Current pod IP address for this instance.
            instance_id: Instance ID to associate with this node.
            job_name: Job name of the owning instance.

        Returns:
            (node_metadata, is_newly_created) — is_newly_created is True only for
            brand-new nodes that did not previously exist in self.nodes.
        """
        if node_name in self.nodes:
            existing_node = self.nodes[node_name]
            old_pod_ip = existing_node.instance_pod_ips.get(instance_id)
            existing_node.instance_pod_ips[instance_id] = pod_ip
            existing_node.instance_ids.add(instance_id)
            existing_node.instance_job_names[instance_id] = job_name
            if old_pod_ip and old_pod_ip != pod_ip:
                logger.info(
                    "Updated node %s pod_ip for instance %d from %s to %s",
                    node_name,
                    instance_id,
                    old_pod_ip,
                    pod_ip,
                )
            return existing_node, False
        new_node = NodeMetadata(
            node_name=node_name,
            instance_ids={instance_id},
            instance_pod_ips={instance_id: pod_ip},
            instance_job_names={instance_id: job_name},
        )
        logger.info("Added new node %s for instance %d (%s)", node_name, instance_id, job_name)
        return new_node, True

    # --- Instance/node sync ---

    def _sync_instance_nodes(self, instance: ReadOnlyInstance) -> None:
        """Synchronize self.nodes with the current state of an instance.

        Resolves pod_ip -> node_name via Kubernetes, then dispatches to either
        _sync_existing_instance_nodes (for instances already tracked in
        self.instances) or _add_new_instance_with_nodes (for new instances).
        Called on INSTANCE_INITIAL and during startup catch-up (update_instances).
        """
        current_node_managers = instance.get_node_managers()
        pod_to_node_name, current_node_names = self._build_node_name_mapping(current_node_managers)

        with self.lock:
            if instance.id in self.instances:
                self._sync_existing_instance_nodes(instance, pod_to_node_name, current_node_names)
            else:
                self._add_new_instance_with_nodes(instance, pod_to_node_name)

    def _sync_existing_instance_nodes(
        self,
        instance: ReadOnlyInstance,
        pod_to_node_name: dict[str, str],
        current_node_names: set[str],
    ) -> None:
        """Sync nodes for an existing instance: remove stale, update pod_ip, add new, manage monitors."""
        existing_nodes = {n: node for n, node in self.nodes.items() if instance.id in node.instance_ids}
        existing_node_names = set(existing_nodes.keys())

        # Remove this instance from nodes no longer in the current pod list
        removed_node_names = existing_node_names - current_node_names
        for node_name in removed_node_names:
            node_meta = self.nodes[node_name]
            node_meta.instance_ids.discard(instance.id)
            node_meta.instance_pod_ips.pop(instance.id, None)
            node_meta.instance_job_names.pop(instance.id, None)
            if not node_meta.instance_ids:
                self.nodes.pop(node_name, None)
                self._stop_resource_monitor_for_node(node_name)
                logger.info("Removed node %s (no remaining instances)", node_name)
            else:
                logger.info(
                    "Removed instance %d from node %s (%d instances remain)",
                    instance.id,
                    node_name,
                    len(node_meta.instance_ids),
                )

        for pod_ip, node_name in pod_to_node_name.items():
            node_metadata, is_new = self._ensure_node_metadata(
                node_name,
                pod_ip,
                instance.id,
                instance.job_name,
            )
            self.nodes[node_name] = node_metadata
            if is_new:
                self._create_resource_monitor_for_node(node_name)

    def _add_new_instance_with_nodes(self, instance: ReadOnlyInstance, pod_to_node_name: dict[str, str]) -> None:
        """Add a brand-new instance with its nodes, handling cross-job node transfers.

        A new instance is one whose instance_id is NOT already in self.instances.
        Its nodes may already exist in self.nodes (preserved from a previous
        INSTANCE_REMOVED) with one of two origins:

        - Same job_name: the instance is restarting (new instance_id, same role).
          Simply update instance_id on the existing NodeMetadata entries.
        - Different job_name (foreign) with NO active instances on the node:
          the node was transferred from another job type (e.g., scale_p2d).
          These "foreign" nodes are handed to _swap_node_ownership for equal exchange.
        - Different job_name but node still has ACTIVE instances:
          multi-instance sharing (e.g., Prefill + Decode on same physical node).
          The new instance is simply added to the node's sets — no swap.

        After ownership resolution, all nodes get their instance_id added,
        InstanceMetadata is created, and per-node ResourceMonitors are started.
        """
        logger.debug("Adding new instance %d (%s) to fault manager", instance.id, instance.job_name)
        ins_metadata = InstanceMetadata(instance_id=instance.id)
        self.instances[instance.id] = ins_metadata

        new_job_name = instance.job_name
        new_instance_id = instance.id

        # Foreign nodes: nodes in this instance's pod list that already exist in
        # self.nodes, have NO active instances, and have a different job_name.
        foreign_nodes: dict[str, NodeMetadata] = {}
        for pod_ip, node_name in pod_to_node_name.items():
            if node_name not in self.nodes:
                continue
            node = self.nodes[node_name]
            # Check for active instances on this node
            active_instances = [iid for iid in node.instance_ids if iid in self.instances]
            if active_instances:
                continue  # shared with active instances → add to sets, don't swap
            # All instances on this node are removed; check if foreign
            other_jobs = {jn for jn in node.instance_job_names.values() if jn and jn != new_job_name}
            if other_jobs:
                foreign_nodes[node_name] = node

        if foreign_nodes:
            # Orphaned nodes: nodes with matching job_name, no active instances,
            # not in the new instance's pod list → waiting to be reclaimed.
            orphaned_nodes: dict[str, NodeMetadata] = {}
            for name, meta in self.nodes.items():
                if name in pod_to_node_name.values():
                    continue
                active = [iid for iid in meta.instance_ids if iid in self.instances]
                if active:
                    continue
                if new_job_name in meta.instance_job_names.values():
                    orphaned_nodes[name] = meta
            self._swap_node_ownership(foreign_nodes, orphaned_nodes, new_job_name, new_instance_id)

        node_metadatas: dict[str, NodeMetadata] = {}
        for pod_ip, node_name in pod_to_node_name.items():
            node_metadata, _ = self._ensure_node_metadata(
                node_name,
                pod_ip,
                new_instance_id,
                new_job_name,
            )
            node_metadatas[node_name] = node_metadata

        self.nodes.update(node_metadatas)
        logger.info("Added instance %d (%s) with %d nodes", new_instance_id, new_job_name, len(node_metadatas))

        for node_name in node_metadatas:
            self._create_resource_monitor_for_node(node_name)

    def _swap_node_ownership(
        self,
        foreign_nodes: dict[str, NodeMetadata],
        orphaned_nodes: dict[str, NodeMetadata],
        new_job_name: str,
        new_instance_id: int,
    ) -> None:
        """Swap node ownership: pair foreign nodes with orphaned nodes equally.

        foreign_nodes are being received from other job types.  orphaned_nodes
        are nodes of new_job_name that were loaned out (pre-computed by caller).
        For each matched pair, instance_ids and instance_job_names are exchanged.
        Unmatched foreign nodes are taken unilaterally; unmatched orphans wait.

        Adapted for multi-instance data model: each node's instance_ids and
        instance_job_names are cleared and reassigned rather than swapped
        field-by-field, since a node in the swap path always has exactly one
        logical owner (the removed instance it belonged to).
        """
        foreign_list = list(foreign_nodes.items())
        orphaned_list = list(orphaned_nodes.items())
        swap_count = min(len(foreign_list), len(orphaned_list))

        for i in range(swap_count):
            foreign_name, foreign_meta = foreign_list[i]
            orphaned_name, orphaned_meta = orphaned_list[i]

            # Snapshot old foreign identity (first entry is the only one for swap nodes)
            old_foreign_inst = next(iter(foreign_meta.instance_ids))
            old_foreign_job = foreign_meta.instance_job_names.get(old_foreign_inst, "")

            # Foreign node → receives new instance identity, clear old faults
            foreign_meta.instance_ids = {new_instance_id}
            foreign_meta.instance_job_names = {new_instance_id: new_job_name}
            foreign_meta.software_fault_infos.clear()
            # pod_ip will be set by _ensure_node_metadata after swap

            # Orphaned node → receives old foreign identity, preserves its pod_ip
            orphaned_pod_ip = next(iter(orphaned_meta.instance_pod_ips.values()))
            orphaned_meta.instance_ids = {old_foreign_inst}
            orphaned_meta.instance_job_names = {old_foreign_inst: old_foreign_job}
            orphaned_meta.instance_pod_ips = {old_foreign_inst: orphaned_pod_ip}
            orphaned_meta.software_fault_infos.clear()

            logger.info(
                "Swap   %s: %s/%d -> %s/%d",
                foreign_name,
                old_foreign_job,
                old_foreign_inst,
                new_job_name,
                new_instance_id,
            )
            logger.info(
                "       %s: %s/%d -> %s/%d",
                orphaned_name,
                new_job_name,
                next(iter(orphaned_meta.instance_ids)),
                old_foreign_job,
                old_foreign_inst,
            )

        for i in range(swap_count, len(foreign_list)):
            foreign_name, foreign_meta = foreign_list[i]
            old_job = next(iter(foreign_meta.instance_job_names.values()))
            old_inst = next(iter(foreign_meta.instance_ids))
            foreign_meta.instance_ids = {new_instance_id}
            foreign_meta.instance_job_names = {new_instance_id: new_job_name}
            foreign_meta.software_fault_infos.clear()
            logger.info(
                "Takeover %s: %s/%d -> %s/%d",
                foreign_name,
                old_job,
                old_inst,
                new_job_name,
                new_instance_id,
            )

    # --- Resource monitoring ---

    def _create_resource_monitor_for_node(self, node_name: str) -> None:
        """Create (or reconfigure) a ResourceMonitor that watches a node's ConfigMap.

        If a monitor already exists for this node and is running with matching
        configuration (namespace and configmap prefix), the call is a no-op.
        If configuration changed, the existing monitor is stopped and recreated.
        The monitor feeds hardware fault updates to _handle_fault_info_update
        and node status changes to _handle_node_status_update.
        """
        with self.config_lock:
            namespace = self.configmap_namespace
            configmap_prefix = self.configmap_prefix

        with self.resource_monitors_lock:
            if node_name in self.resource_monitors:
                existing_monitor = self.resource_monitors[node_name]

                if existing_monitor.is_alive():
                    config_matches = (
                        existing_monitor.namespace == namespace
                        and existing_monitor.configmap_name_prefix == configmap_prefix
                    )
                    if config_matches:
                        logger.debug(
                            "Resource monitor for node %s already exists and is running "
                            "with same configuration, skipping recreation",
                            node_name,
                        )
                        return
                    logger.info(
                        "Resource monitor configuration changed for node %s, stopping existing monitor", node_name
                    )
                    existing_monitor.stop_monitoring()
                else:
                    logger.debug("Resource monitor for node %s exists but not alive, will recreate", node_name)

        logger.info("Creating Resource monitor for node %s", node_name)

        resource_monitor = ResourceMonitor(
            node_name=node_name,
            namespace=namespace,
            configmap_name_prefix=configmap_prefix,
            node_change_handler=self._handle_node_status_update,
            configmap_change_handler=self._handle_fault_info_update,
        )

        with self.resource_monitors_lock:
            self.resource_monitors[node_name] = resource_monitor

        resource_monitor.start_monitoring()

    def _stop_resource_monitor_for_node(self, node_name: str) -> None:
        """Stop Resource monitor for a specific node"""
        with self.resource_monitors_lock:
            if node_name in self.resource_monitors:
                monitor = self.resource_monitors[node_name]
                monitor.stop_monitoring()
                del self.resource_monitors[node_name]
                logger.info("Stopped Resource monitor for node %s", node_name)

    def _node_has_active_instances(self, node_metadata: NodeMetadata) -> bool:
        """Check whether any instance on this node is currently INITIAL or ACTIVE.

        Used to dynamically adjust the fault level of PreSeparateNPU faults:
        - active instances on the node → downgrade to L2 (business is running).
        - no active instances → keep L6 (safe to isolate the NPU).

        Args:
            node_metadata: The node to check.

        Returns:
            True if at least one instance on the node is INITIAL or ACTIVE.
        """
        from motor.controller.core.instance_manager import InstanceManager

        for iid in list(node_metadata.instance_ids):
            if iid not in self.instances:
                continue
            inst = InstanceManager().get_instance(iid)
            if inst is not None and inst.status in (InsStatus.INITIAL, InsStatus.ACTIVE):
                return True
        return False

    def _handle_fault_info_update(self, fault_infos: list[FaultInfo], node_name: str) -> None:
        """Handle a hardware fault information update pushed by a ResourceMonitor.

        Replaces the node's hardware_fault_infos with the incoming fault list.
        Preserves any existing node_reboot fault (managed separately by the node
        status handler), since ConfigMap data does not include reboot faults.

        For PreSeparateNPU faults, dynamically adjusts the fault level based on
        whether the node still hosts INITIAL/ACTIVE instances (L2 if yes, L6 if no).

        After updating, triggers _refresh_instance_fault_level for ALL instances
        on this node.
        """
        node_metadata = None
        with self.lock:
            node_metadata = self.nodes.get(node_name)

        if node_metadata is None:
            logger.warning("Node with node_name %s not found, cannot process fault info update", node_name)
            return

        # Group faults by fault_code, collecting all affected NPU names
        grouped: dict[int, list[FaultInfo]] = {}
        for info in fault_infos:
            code = int(info.fault_code)
            grouped.setdefault(code, []).append(info)

        # Log unique fault codes with aggregated NPU names.
        # Use the highest fault_level among entries sharing the same fault_code
        # so that a severe fault on one NPU is not masked by milder faults on others.
        for idx, (code, infos) in enumerate(grouped.items(), start=1):
            representative = max(infos, key=lambda i: i.fault_level.value)
            npu_names = [i.npu_name for i in infos if i.npu_name]
            if npu_names:
                if len(npu_names) == 1:
                    npu_segment = f", NPU: {npu_names[0]}"
                elif len(npu_names) <= 4:
                    npu_segment = f", NPU: {', '.join(npu_names)}"
                else:
                    npu_segment = f", NPU: {', '.join(npu_names[:3])}, ... ({len(npu_names)} total)"
            else:
                npu_segment = ""
            logger.info(
                "Fault[%d/%d] detected - Type: %s%s, Code: 0x%x, Level: %s(%s)",
                idx,
                len(grouped),
                representative.fault_type.value if representative.fault_type else "N/A",
                npu_segment,
                code,
                representative.fault_level.name,
                representative.origin_fault_level.value if representative.origin_fault_level else "N/A",
            )
        if len(fault_infos) > len(grouped):
            logger.debug(
                "Deduplicated: %d raw fault entries → %d unique fault codes for node %s",
                len(fault_infos),
                len(grouped),
                node_name,
            )

        node_reboot_key = int(SpecialFaultCode.NODE_REBOOT)

        # Pre-resolve active-instance status once for this node so every
        # PreSeparateNPU fault in the batch uses the same snapshot.
        node_has_active = self._node_has_active_instances(node_metadata)

        with self.lock:
            node_reboot_fault = node_metadata.hardware_fault_infos.get(node_reboot_key)

            node_metadata.hardware_fault_infos.clear()
            for code, infos in grouped.items():
                info = max(infos, key=lambda i: i.fault_level.value)
                info.fault_category = FaultCategory.HARDWARE

                # Dynamically adjust PreSeparateNPU fault level based on
                # whether any INITIAL/ACTIVE instance still runs on this node.
                if info.origin_fault_level == OriginFaultLevel.PRE_SEPARATE_NPU:
                    if node_has_active:
                        info.fault_level = FaultLevel.L2
                        logger.info(
                            "PreSeparateNPU fault 0x%x downgraded to L2: node %s still has active instances",
                            code,
                            node_name,
                        )
                    else:
                        info.fault_level = FaultLevel.L6
                        logger.info(
                            "PreSeparateNPU fault 0x%x kept at L6: node %s has no active instances, safe to isolate",
                            code,
                            node_name,
                        )

                # Preserve NPU info in stored entry: note count when multiple NPUs
                # share the same fault code so downstream consumers can see the scope.
                npu_names = [i.npu_name for i in infos if i.npu_name]
                if len(npu_names) > 1:
                    if len(npu_names) <= 4:
                        info.npu_name = ", ".join(npu_names)
                    else:
                        info.npu_name = f"{', '.join(npu_names[:3])}, ... ({len(npu_names)} total)"
                node_metadata.hardware_fault_infos[code] = info

            if node_reboot_fault:
                node_metadata.hardware_fault_infos[node_reboot_key] = node_reboot_fault

        logger.info(
            "Updated node %s with %d hardware fault infos (preserved node_reboot: %s)",
            node_name,
            len(grouped),
            node_reboot_fault is not None,
        )

        # Refresh fault levels for ALL LIVE instances on this node (skip stale/removed ones)
        affected_ids = [iid for iid in node_metadata.instance_ids if iid in self.instances]
        stale_count = len(node_metadata.instance_ids) - len(affected_ids)
        if stale_count > 0:
            logger.debug(
                "Skipping %d stale instance(s) on node %s that no longer exist in fault manager",
                stale_count,
                node_name,
            )
        for instance_id in affected_ids:
            self._refresh_instance_fault_level(instance_id)

        # Wake the strategy center — hardware fault data changed
        with self.work_condition:
            self.work_condition.notify_all()

    def _handle_node_status_update(self, status: NodeStatus, node_name: str) -> None:
        """Handle a node status change pushed by a ResourceMonitor.

        When a node transitions to NOT_READY, a node_reboot fault (L6) is added
        to its hardware_fault_infos.  When it transitions back to READY, the
        node_reboot fault is removed.  After the status change, triggers
        _refresh_instance_fault_level for ALL instances on this node.
        """
        logger.info("Processing Node status update: %s -> %s", node_name, status)

        with self.lock:
            if node_name not in self.nodes:
                logger.warning("Node with node_name %s not found, cannot process node info update", node_name)
                return

            node_metadata = self.nodes[node_name]
            old_status = node_metadata.node_status
            node_metadata.node_status = status
            logger.info("Updated node %s node status to %s", node_name, status)

            if old_status != status:
                if status == NodeStatus.NOT_READY:
                    node_reboot_key = int(SpecialFaultCode.NODE_REBOOT)
                    node_reboot_fault = FaultInfo(
                        fault_category=FaultCategory.HARDWARE,
                        fault_type=HardwareFaultType.NODE_UNHEALTHY,
                        npu_name="",
                        fault_code=SpecialFaultCode.NODE_REBOOT,
                        fault_level=FaultLevel.L6,
                    )
                    self.nodes[node_name].hardware_fault_infos[node_reboot_key] = node_reboot_fault
                    logger.info("Added node reboot fault for node %s", node_name)
                elif status == NodeStatus.READY:
                    node_reboot_key = int(SpecialFaultCode.NODE_REBOOT)
                    if node_reboot_key in self.nodes[node_name].hardware_fault_infos:
                        del self.nodes[node_name].hardware_fault_infos[node_reboot_key]
                        logger.info("Removed node reboot fault for node %s", node_name)
                    else:
                        logger.debug("Node reboot fault not found for node %s", node_name)

        # Refresh fault levels for ALL instances on this node
        affected_ids = list(node_metadata.instance_ids)
        for instance_id in affected_ids:
            self._refresh_instance_fault_level(instance_id)

        # Wake the strategy center — node status changed
        with self.work_condition:
            self.work_condition.notify_all()
