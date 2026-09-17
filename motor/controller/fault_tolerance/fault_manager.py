# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
import concurrent.futures
import threading
import time
from dataclasses import dataclass

from motor.config.controller import ControllerConfig
from motor.common.logger import get_logger
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.resources import InsStatus, Instance, ReadOnlyInstance
from motor.common.etcd.etcd_client import EtcdClient
from motor.controller.core import Observer, ObserverEvent, InstanceManager
from motor.controller.fault_tolerance.strategy import generate_strategy_map
from motor.controller.fault_tolerance.k8s.resource_monitor import ResourceMonitor
from motor.controller.fault_tolerance.k8s.k8s_client import K8sClient
from motor.controller.fault_tolerance.dp_scale_down import (
    FaultNormalizer,
    FtPhase,
    ScaleDownContext,
    get_ft_runtime_store,
)
from motor.controller.fault_tolerance.fault_types import (
    FaultCategory,
    FaultInfo,
    FaultLevel,
    hardware_fault_identity,
    InstanceMetadata,
    NodeMetadata,
    OriginFaultLevel,
    pre_separate_fault_affects_instance,
    software_fault_storage_key,
)
from motor.controller.fault_tolerance.mixin.persistence import _PersistenceMixin
from motor.controller.fault_tolerance.mixin.resource_manager import (
    _ResourceManagerMixin,
)
from motor.controller.fault_tolerance.recovery_planner import build_recovery_plan
from motor.controller.fault_tolerance.serving_overlay import ServingOverlay


logger = get_logger(__name__)


@dataclass(frozen=True)
class _InstanceFaultSnapshot:
    """One filtered view of the fault evidence currently owned by an instance."""

    instance: Instance | ReadOnlyInstance | None
    nodes: tuple[NodeMetadata, ...]
    hardware_faults: tuple[tuple[str, str, NodeMetadata, FaultInfo], ...]
    software_faults: tuple[FaultInfo, ...]


class FaultManager(_PersistenceMixin, _ResourceManagerMixin, ThreadSafeSingleton, Observer):
    """
    Central fault tolerance manager — observes instance lifecycle, tracks node
    faults, and drives recovery strategies.

    Architecture (split across mixins for maintainability):
    - _PersistenceMixin: ETCD save/restore of nodes and instances.
    - _ResourceManagerMixin: node sync and resource monitoring (multi-instance per node).
    - FaultManager itself: lifecycle, config, fault evaluation, strategy processing.

    Key data structures:
    - self.nodes: node_name -> NodeMetadata (fault history per physical node).
      Nodes are preserved across instance removals so fault history survives
      node transfers between instances (e.g., scale_p2d).
    - self.instances: instance_id -> InstanceMetadata (current fault level and
      running strategy per instance).
    """

    def __init__(self, config: ControllerConfig | None = None) -> None:
        super().__init__()
        # If the fault manager is already initialized, return.
        if hasattr(self, "_initialized"):
            return

        if config is None:
            config = ControllerConfig()
        self.config = config
        self.config_lock = threading.RLock()

        # Manage all nodes's status with Kubernetes node_name, when it comes a faulty node,
        # we firstly find out which instance this node belongs to,
        # and then use self.instances to find out all nodes in this instance.
        self.nodes: dict[str, NodeMetadata] = {}
        self.instances: dict[int, InstanceMetadata] = {}
        self.lock = threading.Lock()

        # Version control for data persistence
        self._data_version = 0
        self._version_lock = threading.Lock()

        # Dynamic Resource monitors for per-node monitoring, key is node_name.
        self.resource_monitors: dict[str, ResourceMonitor] = {}
        self.resource_monitors_lock = threading.RLock()

        # Kubernetes client for resolving node_name from pod_ip
        self.k8s_client = K8sClient()

        # Extract required config fields
        with self.config_lock:
            self.etcd_config = config.etcd_config
            self.etcd_tls_config = config.etcd_tls_config
            self.strategy_center_check_interval = config.fault_tolerance_config.strategy_center_check_interval
            # ConfigMap name prefix and namespace for dynamic monitoring
            self.configmap_prefix = config.fault_tolerance_config.configmap_prefix
            self.configmap_namespace = config.fault_tolerance_config.configmap_namespace

        with self.config_lock:
            self.etcd_client = EtcdClient(etcd_config=self.etcd_config, tls_config=self.etcd_tls_config)
        # DP scale-down runtime persistence follows the same switch as the
        # existing Controller/FaultManager persistence.  In standalone mode
        # this keeps scale-down entirely in memory and avoids requiring etcd.
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        get_ft_runtime_store().set_persist_callback(self.persist_data if enable_persistence else None)

        self.stop_event = threading.Event()

        # Condition variable to wake the strategy center thread on-demand
        # instead of busy-waiting on a fixed sleep interval.
        self.work_condition = threading.Condition()
        self._strategy_finished_event = threading.Event()

        # For dual handle function trigger, we use a thread pool executor to handle it.
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

        self.strategies = generate_strategy_map()

        self.ft_strategy_center_thread = None

        self._initialized = True
        logger.info("FaultManager initialized.")

    def start(self) -> None:
        """Start the fault tolerance threads"""
        # Reset stop_event if it was previously set (for singleton reuse)
        if self.stop_event.is_set():
            self.stop_event.clear()

        self.ft_strategy_center_thread = threading.Thread(
            target=self._ft_strategy_center,
            daemon=True,
            name="FaultToleranceStrategyCenter",
        )

        # Try to restore data from ETCD, if failed, it will start with empty state.
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if enable_persistence and not self.restore_data():
            logger.warning("Failed to restore fault manager's data from ETCD, start with empty state")

        # Start Resource monitors for all restored nodes (keyed by node_name)
        with self.lock:
            for node_name in self.nodes:
                self._create_resource_monitor_for_node(node_name)

        self.ft_strategy_center_thread.start()

        logger.info("FaultManager started.")

    def is_alive(self) -> bool:
        """Check if the fault manager threads are alive"""
        return self.ft_strategy_center_thread is not None and self.ft_strategy_center_thread.is_alive()

    def stop(self) -> None:
        self.stop_event.set()
        with self.work_condition:
            self.work_condition.notify_all()

        # Stop all host-specific Resource monitors
        with self.resource_monitors_lock:
            for monitor in self.resource_monitors.values():
                monitor.stop_monitoring()
            self.resource_monitors.clear()

        # Only join threads that have been started
        if self.ft_strategy_center_thread is not None and self.ft_strategy_center_thread.is_alive():
            self.ft_strategy_center_thread.join()

        logger.info("FaultManager stopped.")

    def update_config(self, config: ControllerConfig) -> None:
        """Update config for fault manager, only invoked by config watcher when config changed"""
        with self.config_lock:
            self.config = config

            # Update config fields
            self.etcd_config = config.etcd_config
            self.etcd_tls_config = config.etcd_tls_config
            self.strategy_center_check_interval = config.fault_tolerance_config.strategy_center_check_interval

            # Update ETCD client with new configuration
            self.etcd_client = EtcdClient(etcd_config=self.etcd_config, tls_config=self.etcd_tls_config)
            get_ft_runtime_store().set_persist_callback(
                self.persist_data if self.etcd_config.enable_etcd_persistence else None
            )

            # Check if ConfigMap prefix or namespace configuration changed
            new_configmap_prefix = config.fault_tolerance_config.configmap_prefix
            new_configmap_namespace = config.fault_tolerance_config.configmap_namespace

            config_changed = False
            if self.configmap_prefix != new_configmap_prefix:
                self.configmap_prefix = new_configmap_prefix
                config_changed = True
                logger.info(
                    "ConfigMap prefix configuration updated to: %s",
                    new_configmap_prefix,
                )

            if self.configmap_namespace != new_configmap_namespace:
                self.configmap_namespace = new_configmap_namespace
                config_changed = True
                logger.info(
                    "ConfigMap namespace configuration updated to: %s",
                    new_configmap_namespace,
                )

            if config_changed:
                # Stop all existing node-specific Resource monitors due to configuration change
                with self.resource_monitors_lock:
                    for node_name, monitor in self.resource_monitors.items():
                        monitor.stop_monitoring()
                        logger.info(
                            "Stopped Resource monitor for node %s due to configuration change",
                            node_name,
                        )
                    self.resource_monitors.clear()

                # Restart Resource monitors for all existing nodes with new configuration
                # Get all unique node_names from nodes dictionary
                with self.lock:
                    node_names = {node.node_name for node in self.nodes.values()}
                    for node_name in node_names:
                        self._create_resource_monitor_for_node(node_name)
                        logger.info(
                            "Restarted Resource monitor for node %s with new configuration",
                            node_name,
                        )

                logger.info("Resource configuration updated - all monitors restarted with new config")

            logger.info("FaultManager configuration updated")

    def update(self, instance: ReadOnlyInstance, event: ObserverEvent) -> None:
        """Observer callback invoked by InstanceManager on instance lifecycle events.

        INSTANCE_INITIAL: records job_name mapping and syncs the instance's nodes
        (routing to _sync_instance_nodes which handles both new and existing instances).

        INSTANCE_READY: arms recovery and retires older same-job FT runtimes.

        INSTANCE_REMOVED: removes InstanceMetadata and its FT runtime, but preserves
        nodes in self.nodes so their fault history survives potential transfer.
        """
        logger.info("FaultManager update instance %s with event: %s.", instance.job_name, event)

        if event == ObserverEvent.INSTANCE_INITIAL:
            with self.lock:
                if instance.id in self.instances:
                    logger.debug(
                        "Instance %d already exists in fault manager, skipping add operation.",
                        instance.id,
                    )
                    return

            self._sync_instance_nodes(instance)
        elif event == ObserverEvent.INSTANCE_READY:
            self._mark_instance_recovery_ready(instance.id)
            self._remove_superseded_ft_runtimes(instance)
        elif event == ObserverEvent.INSTANCE_SEPARATED:
            self._refresh_instance_fault_level(instance.id)
        elif event == ObserverEvent.INSTANCE_REMOVED:
            with self.lock:
                removed = self.instances.pop(instance.id, None)
            if removed is not None:
                logger.info(
                    "Removed instance %d (%s) from fault manager, nodes preserved for potential transfer",
                    instance.id,
                    instance.job_name,
                )
            get_ft_runtime_store().remove(instance.id)

        # Wake the strategy center — instance lifecycle changed
        with self.work_condition:
            self.work_condition.notify_all()

    def update_instances(self, instances: list[ReadOnlyInstance]) -> None:
        """
        Update fault manager with existing instances, this func will be invoked
        when fault manager is restarted and needs to catch up with existing instances.
        """
        logger.info("Updating fault manager with %d instances", len(instances))

        for instance in instances:
            logger.debug("Processing instance %s (id: %d)", instance.job_name, instance.id)
            self._sync_instance_nodes(instance)
            if getattr(instance, "status", None) == InsStatus.ACTIVE:
                self._mark_instance_recovery_ready(instance.id)

    def get_node_fault_levels(self, instance_id: int) -> dict[str, FaultLevel]:
        """Return the highest fault level per node for a given instance.

        Args:
            instance_id: The instance whose nodes to query.

        Returns:
            A dict mapping node_name to its highest FaultLevel.
            Nodes with no faults are reported as FaultLevel.HEALTHY.
            Returns an empty dict if the instance is unknown.
        """
        with self.lock:
            if instance_id not in self.instances:
                return {}

            result: dict[str, FaultLevel] = {}
            for node_name, node_metadata in self.nodes.items():
                if instance_id not in node_metadata.instance_ids:
                    continue
                hw_max = max(
                    (f.fault_level for f in node_metadata.hardware_fault_infos.values()),
                    default=FaultLevel.HEALTHY,
                )
                sw_max = max(
                    (
                        f.fault_level
                        for f in node_metadata.software_fault_infos.values()
                        if f.instance_id in (None, instance_id)
                    ),
                    default=FaultLevel.HEALTHY,
                )
                result[node_name] = max(hw_max, sw_max)

            return result

    def report_software_fault(self, fault_info: FaultInfo, pod_ip: str = "", instance_id: int | None = None) -> None:
        """Report a software fault reported by a NodeManager at node granularity.

        Stores the fault on the NodeMetadata identified by pod_ip.

        Args:
            fault_info: Software FaultInfo with fault_category=SOFTWARE.
            pod_ip: Pod IP of the reporting NodeManager.
        """
        if fault_info.fault_category != FaultCategory.SOFTWARE:
            logger.warning(
                "report_software_fault called with non-software fault: %s",
                fault_info.fault_category,
            )
            return

        affected_instance_ids: list[int] = []
        collection_started = False
        with self.lock:
            node_metadata = None
            if pod_ip:
                for n in self.nodes.values():
                    if instance_id is not None:
                        matches = n.instance_pod_ips.get(instance_id) == pod_ip
                    else:
                        matches = pod_ip in n.instance_pod_ips.values()
                    if matches:
                        node_metadata = n
                        break

            if node_metadata is not None:
                if instance_id is not None:
                    if instance_id not in node_metadata.instance_ids:
                        logger.warning(
                            "Software fault instance %d is not registered on node %s",
                            instance_id,
                            node_metadata.node_name,
                        )
                        return
                    fault_info.instance_id = instance_id
                    affected_instance_ids.append(instance_id)
                    instance_metadata = self.instances.get(instance_id)
                    if instance_metadata is not None and fault_info.engine_status in (1, 2):
                        now = time.time()
                        if instance_metadata.fault_collection_started_at is None:
                            instance_metadata.fault_collection_started_at = now
                            collection_started = True
                        if isinstance(fault_info.engine_id, int):
                            observed_at = (
                                instance_metadata.software_dead_observed_at
                                if fault_info.engine_status == 1
                                else instance_metadata.software_unhealthy_observed_at
                            )
                            observed_at.setdefault(fault_info.engine_id, now)
                else:
                    # Backward compatibility for old NodeManagers. This is
                    # ambiguous when multiple instances share a pod IP.
                    for ins_id, ip in node_metadata.instance_pod_ips.items():
                        if ip == pod_ip:
                            affected_instance_ids.append(ins_id)
                node_metadata.software_fault_infos[software_fault_storage_key(fault_info)] = fault_info
                engine_status_name = {
                    0: "HEALTHY",
                    1: "DEAD",
                    2: "UNHEALTHY",
                }.get(fault_info.engine_status, "UNKNOWN")
                logger.info(
                    "Reported software fault for node %s (instances %s): "
                    "dp_rank=%s, type=%s, engine_status=%s(%s), fault_level=%s",
                    node_metadata.node_name,
                    affected_instance_ids,
                    fault_info.engine_id,
                    fault_info.exception_type,
                    fault_info.engine_status,
                    engine_status_name,
                    fault_info.fault_level.name,
                )
            else:
                logger.warning(
                    "Node not found for software fault (pod_ip=%s), cannot report",
                    pod_ip,
                )
                return

        for affected_instance_id in affected_instance_ids:
            self._refresh_instance_fault_level(affected_instance_id)

        if collection_started:
            with self.config_lock:
                timeout = self.config.fault_tolerance_config.cpu_distributed_timeout_seconds
            timer = threading.Timer(timeout, self._on_fault_collection_deadline)
            timer.daemon = True
            timer.start()

        # Wake the strategy center — fault data changed
        with self.work_condition:
            self.work_condition.notify_all()

    def _on_fault_collection_deadline(self) -> None:
        """Wake strategy evaluation when an incomplete FT status round expires."""
        with self.work_condition:
            self.work_condition.notify_all()

    def _record_hardware_fault_event(self, instance_id: int, fault_keys: set[str]) -> None:
        """Open the hardware-first correlation window for an instance.

        Repeated ConfigMap watch events must not extend the window.  An active
        recovery round therefore records only the first actionable hardware
        observation and uses a one-shot wake-up for its deadline.
        """
        now = time.time()
        with self.lock:
            metadata = self.instances.get(instance_id)
        if metadata is None:
            return
        with metadata.lock:
            if not metadata.recovery_ready:
                new_baseline = fault_keys - metadata.ignored_pre_ready_hardware_faults
                metadata.ignored_pre_ready_hardware_faults.update(fault_keys)
                if new_baseline:
                    logger.info(
                        "Instance %d recorded %d pre-ready hardware fault(s) as baseline",
                        instance_id,
                        len(new_baseline),
                    )
                return
            new_faults = fault_keys - metadata.ignored_pre_ready_hardware_faults - metadata.handled_hardware_faults
            if not new_faults:
                return
            if metadata.hardware_fault_observed_at is not None:
                return
            metadata.hardware_fault_observed_at = now
        with self.config_lock:
            timeout = self.config.fault_tolerance_config.hardware_ft_correlation_window_sec
        logger.info(
            "Instance %d opened %.1fs hardware/FT correlation window",
            instance_id,
            timeout,
        )
        timer = threading.Timer(timeout, self._on_fault_collection_deadline)
        timer.daemon = True
        timer.start()

    def _mark_instance_recovery_ready(self, instance_id: int) -> None:
        """Arm recovery after snapshotting hardware faults present at readiness."""
        with self.lock:
            metadata = self.instances.get(instance_id)
            if metadata is None:
                return
            with self.config_lock:
                enable_dp_scale_down = self.config.fault_tolerance_config.enable_dp_scale_down
            baseline = (
                {
                    hardware_fault_identity(node.node_name, fault)
                    for node in self.nodes.values()
                    if instance_id in node.instance_ids
                    for fault in node.hardware_fault_infos.values()
                    if fault.fault_level >= FaultLevel.L4
                }
                if enable_dp_scale_down
                else set()
            )
            for node in self.nodes.values():
                if instance_id not in node.instance_ids:
                    continue
                stale_keys = [
                    key for key, fault in node.software_fault_infos.items() if fault.instance_id in (None, instance_id)
                ]
                for key in stale_keys:
                    del node.software_fault_infos[key]
        with metadata.lock:
            metadata.ignored_pre_ready_hardware_faults.update(baseline)
            metadata.fault_collection_started_at = None
            metadata.hardware_fault_observed_at = None
            metadata.software_dead_observed_at.clear()
            metadata.software_unhealthy_observed_at.clear()
            metadata.recovery_ready = True
        logger.info(
            "Instance %d recovery armed at READY with %d pre-existing hardware fault(s) ignored",
            instance_id,
            len(metadata.ignored_pre_ready_hardware_faults),
        )
        self._refresh_instance_fault_level(instance_id)

    @staticmethod
    def _remove_superseded_ft_runtimes(instance: ReadOnlyInstance) -> None:
        """Retire previous runtime records after the replacement instance is ready."""
        same_job_instances = [
            candidate for candidate in InstanceManager().get_instances() if candidate.job_name == instance.job_name
        ]
        if not same_job_instances or max(candidate.id for candidate in same_job_instances) != instance.id:
            return
        store = get_ft_runtime_store()
        for candidate in same_job_instances:
            if candidate.id != instance.id and store.remove(candidate.id):
                logger.info(
                    "Removed superseded FT runtime %d after replacement instance %d became ready",
                    candidate.id,
                    instance.id,
                )

    def _collect_instance_faults(self, instance_id: int) -> _InstanceFaultSnapshot | None:
        """Collect instance-scoped fault evidence using one ownership filter."""
        instance = InstanceManager().get_instance(instance_id)
        with self.lock:
            metadata = self.instances.get(instance_id)
            if metadata is None:
                return None
            nodes = tuple(
                node
                for node in self.nodes.values()
                if instance_id in node.instance_ids and (node.hardware_fault_infos or node.software_fault_infos)
            )
            hardware_faults = tuple(
                (
                    hardware_fault_identity(node.node_name, fault),
                    node.instance_pod_ips.get(instance_id, ""),
                    node,
                    fault,
                )
                for node in nodes
                for fault in node.hardware_fault_infos.values()
                if FaultNormalizer.hardware_fault_affects_instance(
                    instance,
                    node.instance_pod_ips.get(instance_id, ""),
                    fault,
                )
            )
            software_faults = tuple(
                fault
                for node in nodes
                for fault in node.software_fault_infos.values()
                if fault.instance_id in (None, instance_id)
            )
        return _InstanceFaultSnapshot(
            instance=instance,
            nodes=nodes,
            hardware_faults=hardware_faults,
            software_faults=software_faults,
        )

    def _refresh_instance_fault_level(self, instance_id: int) -> None:
        """Re-evaluate the fault level of an instance from all its nodes' faults.

        Scans hardware_fault_infos and software_fault_infos across every node
        belonging to this instance, picks the highest fault level, and updates
        InstanceMetadata.fault_level/fault_code. Side effects:
        - fault_level > L2: calls InstanceManager.separate_instance (isolation).
        - fault_level <= L2: calls InstanceManager.recover_instance if previously separated.
        - No faults: resets instance to HEALTHY.

        After updating, persists data to ETCD if persistence is enabled.
        """
        instance_metadata = None
        with self.lock:
            instance_metadata = self.instances.get(instance_id)
            if instance_metadata is None:
                logger.warning("Instance %d not found, skipping fault level refresh", instance_id)
                return
        with instance_metadata.lock:
            if not instance_metadata.recovery_ready:
                logger.debug("Instance %d is not ready; fault recovery remains disarmed", instance_id)
                return

        snapshot = self._collect_instance_faults(instance_id)
        if snapshot is None:
            return
        instance = snapshot.instance
        instance_nodes = snapshot.nodes
        active_hardware_faults = {fault_key for fault_key, _, _, _ in snapshot.hardware_faults}
        with instance_metadata.lock:
            instance_metadata.handled_hardware_faults.intersection_update(active_hardware_faults)
            instance_metadata.ignored_pre_ready_hardware_faults.intersection_update(active_hardware_faults)
            handled_hardware_faults = set(instance_metadata.handled_hardware_faults)
            ignored_hardware_faults = set(instance_metadata.ignored_pre_ready_hardware_faults)

        with self.config_lock:
            enable_dp_scale_down = self.config.fault_tolerance_config.enable_dp_scale_down
        hardware_scale_down_candidate = False
        if enable_dp_scale_down:
            if instance is not None:
                hardware_context = [
                    (fault_key, pod_ip, fault)
                    for fault_key, pod_ip, _, fault in snapshot.hardware_faults
                    if fault.fault_level >= FaultLevel.L4
                    and fault_key not in handled_hardware_faults
                    and fault_key not in ignored_hardware_faults
                ]
                hardware_candidates, _ = FaultNormalizer.hardware_dp_ranks(instance, hardware_context)
                hardware_scale_down_candidate = bool(hardware_candidates)

        # Re-evaluate PreSeparateNPU fault levels on every node before
        # computing the instance's overall fault level.  This catches the
        # scenario where all instances have left a node since the last
        # ConfigMap update — the PreSeparateNPU fault should now escalate
        # to L6 (safe to isolate) instead of staying at L2.
        for node_metadata in instance_nodes:
            for code, fault_info in list(node_metadata.hardware_fault_infos.items()):
                if fault_info.origin_fault_level != OriginFaultLevel.PRE_SEPARATE_NPU:
                    continue
                if self._keep_a2_linkdown_at_l6(fault_info, node_metadata):
                    if fault_info.fault_level != FaultLevel.L6:
                        logger.info(
                            "Re-evaluated PreSeparateNPU 0x%x → L6 on node %s (A2 linkdown, keep L6)",
                            code,
                            node_metadata.node_name,
                        )
                        fault_info.fault_level = FaultLevel.L6
                    continue
                if self._node_has_active_instances(node_metadata):
                    if fault_info.fault_level != FaultLevel.L2:
                        logger.info(
                            "Re-evaluated PreSeparateNPU 0x%x → L2 on node %s (active instances present)",
                            fault_info.fault_code,
                            node_metadata.node_name,
                        )
                        fault_info.fault_level = FaultLevel.L2
                else:
                    if fault_info.fault_level != FaultLevel.L6:
                        logger.info(
                            "Re-evaluated PreSeparateNPU 0x%x → L6 on node %s (no active instances)",
                            fault_info.fault_code,
                            node_metadata.node_name,
                        )
                        fault_info.fault_level = FaultLevel.L6

        # Evaluate the instance's fault level from both hardware and software faults
        with instance_metadata.lock:
            # Synchronize PreSeparateNPU levels that became stale between
            # Step 1 (re-evaluation) and Step 2 (this block).  If a node has
            # active instances now but the fault is still L6, downgrade it to
            # L2 before computing the instance fault level — otherwise a stale
            # L6 could trigger an unnecessary separate_instance().
            for node in instance_nodes:
                for fi in node.hardware_fault_infos.values():
                    if (
                        fi.origin_fault_level == OriginFaultLevel.PRE_SEPARATE_NPU
                        and fi.fault_level == FaultLevel.L6
                        and self._node_has_active_instances(node)
                        and not self._keep_a2_linkdown_at_l6(fi, node)
                    ):
                        fi.fault_level = FaultLevel.L2
                        logger.info(
                            "Synchronized PreSeparateNPU 0x%x → L2 on node %s "
                            "(active instances detected after re-evaluation)",
                            fi.fault_code,
                            node.node_name,
                        )

            # PreSeparateNPU L6 on a vacated node is node-only (do not ScaleP2D).
            # A2 linkdown is further filtered to instances that own the named NPU.
            hardware_type = self._hardware_type()

            all_hw_faults = [
                fault
                for fault_key, _, node, fault in snapshot.hardware_faults
                if pre_separate_fault_affects_instance(fault, node, instance, hardware_type)
                and fault_key not in handled_hardware_faults
                and fault_key not in ignored_hardware_faults
            ]
            all_sw_faults = list(snapshot.software_faults)

            highest_hw_fault = max(all_hw_faults, key=lambda f: f.fault_level, default=None)
            highest_sw_fault = max(all_sw_faults, key=lambda f: f.fault_level, default=None)

            # Determine overall highest fault
            if highest_hw_fault and highest_sw_fault:
                target = (
                    highest_hw_fault
                    if highest_hw_fault.fault_level >= highest_sw_fault.fault_level
                    else highest_sw_fault
                )
            elif highest_hw_fault:
                target = highest_hw_fault
            elif highest_sw_fault:
                target = highest_sw_fault
            else:
                target = None

            if target is None:
                # No faults, instance is healthy
                instance_metadata.fault_collection_started_at = None
                instance_metadata.hardware_fault_observed_at = None
                instance_metadata.software_dead_observed_at.clear()
                instance_metadata.software_unhealthy_observed_at.clear()
                if instance_metadata.fault_level != FaultLevel.HEALTHY:
                    instance_metadata.fault_level = FaultLevel.HEALTHY
                    instance_metadata.fault_code = 0x0
                    logger.info("Instance %d reset to healthy state", instance_id)
                    InstanceManager().recover_instance(instance_id)
                return

            # Update instance fault level and code
            if instance_metadata.fault_level != target.fault_level or instance_metadata.fault_code != int(
                target.fault_code
            ):
                instance_metadata.fault_level = target.fault_level
                instance_metadata.fault_code = int(target.fault_code)
                logger.info(
                    "Instance %d fault level updated to %s (code: 0x%x, category: %s)",
                    instance_id,
                    target.fault_level.name,
                    target.fault_code,
                    target.fault_category.value,
                )

            if instance_metadata.fault_level > FaultLevel.L2:
                if target.fault_category == FaultCategory.HARDWARE and hardware_scale_down_candidate:
                    logger.info("Deferring instance %d isolation to hardware DP scale-down", instance_id)
                else:
                    InstanceManager().separate_instance(instance_id)
            else:
                if InstanceManager().is_instance_separated(instance_id):
                    InstanceManager().recover_instance(instance_id)

        # Persist data after instance fault level update
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if enable_persistence and not self.persist_data():
            logger.debug(
                "Failed to persist fault manager data to ETCD after instance fault level refresh for instance %d",
                instance_id,
            )

    def _ft_strategy_center(self) -> None:
        """Background thread: periodically evaluates every instance's fault level and manages strategies."""
        logger.info("Fault tolerance strategy center started")
        while not self.stop_event.is_set():
            instance_ids = []
            with self.lock:
                instance_ids = list(self.instances.keys())

            logger.debug("Processing %d instances in strategy center", len(instance_ids))

            for instance_id in instance_ids:
                self._process_instance_strategy(instance_id)

            with self.config_lock:
                check_interval = self.strategy_center_check_interval
            with self.work_condition:
                if not self._strategy_finished_event.is_set():
                    self.work_condition.wait(timeout=check_interval)
                self._strategy_finished_event.clear()

        logger.info("Fault tolerance strategy center stopped")

    def _build_scale_down_context(
        self,
        instance_id: int,
    ) -> ScaleDownContext:
        """Freeze the authoritative fault candidates before a strategy starts."""
        snapshot = self._collect_instance_faults(instance_id)
        if snapshot is None or snapshot.instance is None:
            return ScaleDownContext()
        instance = snapshot.instance
        with self.lock:
            metadata = self.instances.get(instance_id)
        if metadata is not None:
            with metadata.lock:
                handled_hardware_faults = set(metadata.handled_hardware_faults)
                ignored_hardware_faults = set(metadata.ignored_pre_ready_hardware_faults)
                started_at = metadata.fault_collection_started_at
                hardware_started_at = metadata.hardware_fault_observed_at
                dead_observed_at = dict(metadata.software_dead_observed_at)
                unhealthy_observed_at = dict(metadata.software_unhealthy_observed_at)
        else:
            handled_hardware_faults = set()
            ignored_hardware_faults = set()
            started_at = None
            hardware_started_at = None
            dead_observed_at = {}
            unhealthy_observed_at = {}
        hardware_faults = [
            (fault_key, pod_ip, fault.model_copy(deep=True))
            for fault_key, pod_ip, _, fault in snapshot.hardware_faults
            if fault.fault_level >= FaultLevel.L2
            and fault_key not in handled_hardware_faults
            and fault_key not in ignored_hardware_faults
        ]
        software_faults = [fault.model_copy(deep=True) for fault in snapshot.software_faults]

        registered_ranks = {endpoint.id for endpoint in instance.get_all_endpoints(include_headless=False)}
        runtime = get_ft_runtime_store().get(instance_id)
        committed_ranks = set(runtime["dead_committed"]) if runtime is not None else set()
        active_ranks = registered_ranks - committed_ranks
        dead_ranks = FaultNormalizer.software_dp_ranks(software_faults) & active_ranks
        unhealthy_ranks = {
            fault.engine_id
            for fault in software_faults
            if fault.engine_status == 2 and isinstance(fault.engine_id, int)
        } & active_ranks
        collection_timed_out = started_at is not None and (
            time.time() - started_at >= self.config.fault_tolerance_config.cpu_distributed_timeout_seconds
        )
        # L2 hardware evidence remains observational. Actionable hardware
        # evidence is correlated per fault so a card already removed by a
        # prior round can be acknowledged without affecting active ranks.
        actionable_hardware_faults = [item for item in hardware_faults if item[2].fault_level >= FaultLevel.L4]
        hardware_candidates: set[int] = set()
        mapped_fault_keys: set[str] = set()
        already_removed_fault_keys: set[str] = set()
        has_unmapped_hardware = False
        for hardware_fault in actionable_hardware_faults:
            candidates, keys = FaultNormalizer.hardware_dp_ranks(instance, [hardware_fault])
            if candidates and candidates <= committed_ranks:
                already_removed_fault_keys.update(keys)
                continue
            active_candidates = candidates - committed_ranks
            hardware_candidates.update(active_candidates)
            mapped_fault_keys.update(keys)
            has_unmapped_hardware = has_unmapped_hardware or not candidates

        engine_fault_observed = bool(dead_ranks or unhealthy_ranks)
        all_dp_unhealthy = bool(active_ranks) and not dead_ranks and active_ranks <= unhealthy_ranks
        collection_complete = (
            bool(dead_ranks) and bool(unhealthy_ranks) and active_ranks <= dead_ranks | unhealthy_ranks
        )
        hardware_fault_observed = bool(hardware_candidates or has_unmapped_hardware)
        hardware_first = (
            hardware_fault_observed
            and hardware_started_at is not None
            and (started_at is None or hardware_started_at < started_at)
        )
        with self.config_lock:
            hardware_window = self.config.fault_tolerance_config.hardware_ft_correlation_window_sec
        hardware_dead_observed = bool(hardware_candidates) and hardware_candidates <= dead_ranks
        timely_dead_ranks = {
            rank
            for rank, observed_at in dead_observed_at.items()
            if hardware_started_at is not None and observed_at - hardware_started_at <= hardware_window
        }
        timely_unhealthy_ranks = {
            rank
            for rank, observed_at in unhealthy_observed_at.items()
            if hardware_started_at is not None and observed_at - hardware_started_at <= hardware_window
        }
        hardware_ft_observed = bool(
            hardware_first
            and (
                (all_dp_unhealthy and active_ranks <= timely_unhealthy_ranks)
                or (collection_complete and hardware_dead_observed and hardware_candidates <= timely_dead_ranks)
            )
        )
        hardware_wait_timed_out = bool(
            hardware_first
            and not hardware_ft_observed
            and hardware_started_at is not None
            and time.time() - hardware_started_at >= hardware_window
        )
        return ScaleDownContext(
            pending_removed_ranks=tuple(sorted(dead_ranks)),
            source="hardware" if hardware_first else "software",
            fault_keys=tuple(sorted(mapped_fault_keys)),
            all_dp_unhealthy=all_dp_unhealthy,
            collection_complete=collection_complete,
            collection_timed_out=collection_timed_out,
            engine_fault_observed=engine_fault_observed,
            all_dp_removed=bool(active_ranks) and active_ranks <= dead_ranks,
            hardware_fault_observed=hardware_fault_observed,
            hardware_wait_timed_out=hardware_wait_timed_out,
            hardware_affected_ranks=tuple(sorted(hardware_candidates)),
            hardware_ft_observed=hardware_ft_observed,
            already_removed_fault_keys=tuple(sorted(already_removed_fault_keys)),
            engine_recovery_eligible=instance.status == InsStatus.ACTIVE,
            hardware_mapping_complete=not has_unmapped_hardware,
        )

    def _is_superseded_instance(self, ins_id: int) -> bool:
        """True when assembler already created a newer instance id for the same job."""
        inst = InstanceManager().get_instance(ins_id)
        if inst is None:
            return False
        current = InstanceManager().get_instance_by_job_name(inst.job_name)
        current_id = getattr(current, "id", None) if current is not None else None
        return isinstance(current_id, int) and current_id != ins_id

    def _process_instance_strategy(self, ins_id: int) -> None:
        """
        Generate and manage the recovery strategy for an instance based on fault level.

        One strategy is allowed per recovery round. Evidence arriving while it
        runs is retained and evaluated only after the round completes.

        When a strategy finishes:
        - Clear all software faults for the instance (symptoms resolved by recovery).
        - Trigger re-evaluation of fault level (hardware faults refreshed by ConfigMap).
        """
        logger.debug("Processing strategy for instance %d", ins_id)

        ins_metadata = None
        with self.lock:
            ins_metadata = self.instances.get(ins_id)
            if ins_metadata is None:
                logger.warning("Instance %d not found in instances dict", ins_id)
                return

        # Reap the previous round before looking at fault evidence. Otherwise
        # its still-latched software faults can plan a new WAITING state and
        # overwrite a scale-down that has already committed successfully.
        if self._complete_finished_strategy(ins_id, ins_metadata):
            return

        superseded = self._is_superseded_instance(ins_id)
        plan_started = False
        with ins_metadata.lock:
            has_fault = ins_metadata.fault_level != FaultLevel.HEALTHY
        scale_down_context = ScaleDownContext()
        if has_fault and not superseded:
            scale_down_context = self._build_scale_down_context(ins_id)
            if scale_down_context.already_removed_fault_keys:
                with ins_metadata.lock:
                    ins_metadata.handled_hardware_faults.update(scale_down_context.already_removed_fault_keys)
                if not (scale_down_context.hardware_fault_observed or scale_down_context.engine_fault_observed):
                    self._refresh_instance_fault_level(ins_id)
                    return

        if not has_fault and not superseded:
            runtime = get_ft_runtime_store().get(ins_id)
            if runtime is not None and runtime["phase"] == FtPhase.WAITING_ENGINE_FAULT.value:
                restored = get_ft_runtime_store().resume_after_fault_clear(ins_id)
                instance = InstanceManager().get_instance(ins_id)
                if restored is not None and instance is not None and ServingOverlay.publish(instance):
                    get_ft_runtime_store().mark_serving_published({ins_id})
                return
        with ins_metadata.lock:
            fault_level = ins_metadata.fault_level
            fault_code = ins_metadata.fault_code
            current_strategy = ins_metadata.strategy
            strategy_context: object = scale_down_context

            # Do not preempt an active recovery round. Newly arriving evidence
            # remains recorded and is planned in the next round after success.
            if current_strategy is not None:
                return

            if superseded:
                # Assembler already created a newer id for this job_name; do not
                # stop the replacement's NodeManagers via the stale instance.
                logger.debug(
                    "Skip strategy for superseded instance %d (job already has a newer instance)",
                    ins_id,
                )
                new_strategy_cls = None
            elif fault_level != FaultLevel.HEALTHY and ins_metadata.prev_strategy_failed:
                # Engine FT and relaunch are single-attempt recovery stages;
                # failure advances to whole-instance reconfiguration. Other
                # baseline strategies retain master's optional relaunch stage.
                if ins_metadata.prev_strategy_name == "InstanceReconfigurationStrategy":
                    new_strategy_cls = None
                elif ins_metadata.prev_strategy_name in {
                    "DpScaleDownStrategy",
                    "EngineFastRecoveryStrategy",
                    "EngineRelaunchStrategy",
                }:
                    from motor.controller.fault_tolerance.strategy import InstanceReconfigurationStrategy

                    new_strategy_cls = InstanceReconfigurationStrategy
                elif self.config.fault_tolerance_config.enable_engine_relaunch:
                    from motor.controller.fault_tolerance.strategy import EngineRelaunchStrategy

                    new_strategy_cls = EngineRelaunchStrategy
                else:
                    new_strategy_cls = None
            elif fault_level != FaultLevel.HEALTHY:
                generated_strategy_cls = (
                    self.strategies[fault_level](fault_code, ins_id, self.config)
                    if fault_level != FaultLevel.HEALTHY
                    else None
                )
                plan = build_recovery_plan(
                    ins_id,
                    self.config,
                    scale_down_context,
                    generated_strategy_cls,
                    fault_level,
                    fault_code,
                )
                new_strategy_cls, strategy_context = plan.strategy, plan.context
                if new_strategy_cls is None and plan.evidence.engine_fault_observed:
                    get_ft_runtime_store().transition(ins_id, phase=FtPhase.WAITING_ENGINE_FAULT)
                plan_log = logger.info if new_strategy_cls is not None else logger.debug
                plan_log(
                    "Instance %d recovery plan: source=%s, strategy=%s, fallback=%s, "
                    "removed_ranks=%s, all_unhealthy=%s, collection_complete=%s, "
                    "collection_timed_out=%s, hardware_wait_timed_out=%s",
                    ins_id,
                    plan.source.value,
                    new_strategy_cls.__name__ if new_strategy_cls else "WAIT",
                    plan.fallback.__name__ if plan.fallback else "NONE",
                    plan.evidence.pending_removed_ranks,
                    plan.evidence.all_dp_unhealthy,
                    plan.evidence.collection_complete,
                    plan.evidence.collection_timed_out,
                    plan.evidence.hardware_wait_timed_out,
                )
            else:
                new_strategy_cls = None

            if new_strategy_cls is not None:
                new_strategy = new_strategy_cls()
                new_strategy.bind_config(self.config)
                new_strategy.bind_context(strategy_context)
                logger.info(
                    "Instance %d: strategy %s, level=%s, code=0x%08x",
                    ins_id,
                    new_strategy_cls.__name__,
                    fault_level.name,
                    fault_code,
                )
                future = self.executor.submit(new_strategy.execute, ins_id)
                future.add_done_callback(self._on_strategy_finished)
                ins_metadata.strategy = new_strategy
                ins_metadata.strategy_future = future
                ins_metadata.strategy_fault_level = fault_level
                if not ins_metadata.prev_strategy_failed:
                    ins_metadata.recovery_plan_source = scale_down_context.source
                    ins_metadata.recovery_plan_strategy = new_strategy_cls.__name__
                    ins_metadata.recovery_plan_fallback = "InstanceReconfigurationStrategy"
                plan_started = True

        if plan_started:
            with self.config_lock:
                enable_persistence = self.etcd_config.enable_etcd_persistence
            if enable_persistence and not self.persist_data():
                logger.debug("Failed to persist recovery plan for instance %d", ins_id)

    def _complete_finished_strategy(self, ins_id: int, ins_metadata: InstanceMetadata) -> bool:
        """Finish one returned strategy before evaluating the next recovery round."""
        clear_software_faults = False
        with ins_metadata.lock:
            strategy = ins_metadata.strategy
            future = ins_metadata.strategy_future
            if strategy is None or not strategy.is_finished() or (future is not None and not future.done()):
                return False
            logger.info(
                "Instance %d: strategy %s finished, level=%s",
                ins_id,
                strategy.__class__.__name__,
                ins_metadata.strategy_fault_level.name,
            )
            completed_context = strategy.strategy_context
            if ins_metadata.strategy_preempted:
                ins_metadata.strategy_preempted = False
            else:
                ins_metadata.prev_strategy_failed = strategy.is_failed()
                ins_metadata.prev_strategy_name = strategy.__class__.__name__
                ins_metadata.prev_strategy_fallback = (
                    completed_context.fallback_strategy
                    if ins_metadata.prev_strategy_failed and isinstance(completed_context, ScaleDownContext)
                    else (
                        ins_metadata.recovery_plan_fallback
                        if ins_metadata.prev_strategy_failed
                        and ins_metadata.prev_strategy_name == "EngineFastRecoveryStrategy"
                        else ""
                    )
                )
                if (
                    not ins_metadata.prev_strategy_failed
                    and isinstance(completed_context, ScaleDownContext)
                    and completed_context.fault_keys
                ):
                    ins_metadata.handled_hardware_faults.update(completed_context.fault_keys)
                clear_software_faults = not ins_metadata.prev_strategy_failed
                if clear_software_faults:
                    ins_metadata.recovery_plan_source = ""
                    ins_metadata.recovery_plan_strategy = ""
                    ins_metadata.recovery_plan_fallback = ""
            ins_metadata.strategy = None
            ins_metadata.strategy_future = None
            ins_metadata.strategy_fault_level = FaultLevel.HEALTHY

        # These operations acquire FaultManager locks and must stay outside
        # InstanceMetadata.lock.
        if clear_software_faults:
            self._clear_software_faults(ins_id)
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if enable_persistence and not self.persist_data():
            logger.debug(
                "Failed to persist fault manager data after strategy completion for instance %d",
                ins_id,
            )
        self._refresh_instance_fault_level(ins_id)
        return True

    def _on_strategy_finished(self, _future: concurrent.futures.Future) -> None:
        """Wake the strategy center as soon as an asynchronous strategy returns."""
        self._strategy_finished_event.set()
        with self.work_condition:
            self.work_condition.notify_all()

    def _clear_software_faults(self, instance_id: int) -> None:
        """Clear all software faults from nodes of an instance after strategy completion."""
        cleared = 0
        with self.lock:
            metadata = self.instances.get(instance_id)
            if metadata is not None:
                metadata.fault_collection_started_at = None
                metadata.hardware_fault_observed_at = None
                metadata.software_dead_observed_at.clear()
                metadata.software_unhealthy_observed_at.clear()
            for node_metadata in self.nodes.values():
                if instance_id in node_metadata.instance_ids and node_metadata.software_fault_infos:
                    keys = [
                        key
                        for key, fault in node_metadata.software_fault_infos.items()
                        if fault.instance_id in (None, instance_id)
                    ]
                    for key in keys:
                        del node_metadata.software_fault_infos[key]
                    cleared += len(keys)
        if cleared > 0:
            logger.info(
                "Cleared %d software faults for instance %d after strategy completion",
                cleared,
                instance_id,
            )
