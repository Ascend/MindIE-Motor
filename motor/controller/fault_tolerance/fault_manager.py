# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
import threading
import concurrent.futures
from motor.config.controller import ControllerConfig
from motor.common.logger import get_logger
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.resources import ReadOnlyInstance
from motor.common.etcd.etcd_client import EtcdClient
from motor.controller.core import Observer, ObserverEvent, InstanceManager
from motor.controller.fault_tolerance.strategy import generate_strategy_map
from motor.controller.fault_tolerance.k8s.resource_monitor import ResourceMonitor
from motor.controller.fault_tolerance.k8s.k8s_client import K8sClient
from motor.controller.fault_tolerance.fault_types import (
    FaultCategory,
    FaultInfo,
    FaultLevel,
    InstanceMetadata,
    NodeMetadata,
)
from motor.controller.fault_tolerance.mixin.persistence import _PersistenceMixin
from motor.controller.fault_tolerance.mixin.resource_manager import _ResourceManagerMixin


logger = get_logger(__name__)


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

        self.stop_event = threading.Event()

        # Condition variable to wake the strategy center thread on-demand
        # instead of busy-waiting on a fixed sleep interval.
        self.work_condition = threading.Condition()

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
            target=self._ft_strategy_center, daemon=True, name="FaultToleranceStrategyCenter"
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

            # Check if ConfigMap prefix or namespace configuration changed
            new_configmap_prefix = config.fault_tolerance_config.configmap_prefix
            new_configmap_namespace = config.fault_tolerance_config.configmap_namespace

            config_changed = False
            if self.configmap_prefix != new_configmap_prefix:
                self.configmap_prefix = new_configmap_prefix
                config_changed = True
                logger.info("ConfigMap prefix configuration updated to: %s", new_configmap_prefix)

            if self.configmap_namespace != new_configmap_namespace:
                self.configmap_namespace = new_configmap_namespace
                config_changed = True
                logger.info("ConfigMap namespace configuration updated to: %s", new_configmap_namespace)

            if config_changed:
                # Stop all existing node-specific Resource monitors due to configuration change
                with self.resource_monitors_lock:
                    for node_name, monitor in self.resource_monitors.items():
                        monitor.stop_monitoring()
                        logger.info("Stopped Resource monitor for node %s due to configuration change", node_name)
                    self.resource_monitors.clear()

                # Restart Resource monitors for all existing nodes with new configuration
                # Get all unique node_names from nodes dictionary
                with self.lock:
                    node_names = {node.node_name for node in self.nodes.values()}
                    for node_name in node_names:
                        self._create_resource_monitor_for_node(node_name)
                        logger.info("Restarted Resource monitor for node %s with new configuration", node_name)

                logger.info("Resource configuration updated - all monitors restarted with new config")

            logger.info("FaultManager configuration updated")

    def update(self, instance: ReadOnlyInstance, event: ObserverEvent) -> None:
        """Observer callback invoked by InstanceManager on instance lifecycle events.

        INSTANCE_INITIAL: records job_name mapping and syncs the instance's nodes
        (routing to _sync_instance_nodes which handles both new and existing instances).

        INSTANCE_REMOVED: removes InstanceMetadata but preserves nodes in self.nodes
        so their fault history survives potential transfer to another instance.
        """
        logger.info("FaultManager update instance %s with event: %s.", instance.job_name, event)

        if event == ObserverEvent.INSTANCE_INITIAL:
            with self.lock:
                if instance.id in self.instances:
                    logger.debug("Instance %d already exists in fault manager, skipping add operation.", instance.id)
                    return

            self._sync_instance_nodes(instance)
        elif event == ObserverEvent.INSTANCE_SEPERATED:
            self._refresh_instance_fault_level(instance.id)
        elif event == ObserverEvent.INSTANCE_REMOVED:
            with self.lock:
                if instance.id not in self.instances:
                    return
                self.instances.pop(instance.id, None)
                logger.info(
                    "Removed instance %d (%s) from fault manager, nodes preserved for potential transfer",
                    instance.id,
                    instance.job_name,
                )

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
                highest = FaultLevel.HEALTHY
                for fault_info in node_metadata.hardware_fault_infos.values():
                    if fault_info.fault_level > highest:
                        highest = fault_info.fault_level
                for fault_info in node_metadata.software_fault_infos.values():
                    if fault_info.fault_level > highest:
                        highest = fault_info.fault_level
                result[node_name] = highest

            return result

    def report_software_fault(self, fault_info: FaultInfo, pod_ip: str = "") -> None:
        """Report a software fault reported by a NodeManager at node granularity.

        Stores the fault on the NodeMetadata identified by pod_ip.

        Args:
            fault_info: Software FaultInfo with fault_category=SOFTWARE.
            pod_ip: Pod IP of the reporting NodeManager.
        """
        if fault_info.fault_category != FaultCategory.SOFTWARE:
            logger.warning("report_software_fault called with non-software fault: %s", fault_info.fault_category)
            return

        affected_instance_ids: list[int] = []
        with self.lock:
            node_metadata = None
            if pod_ip:
                for n in self.nodes.values():
                    if pod_ip in n.instance_pod_ips.values():
                        node_metadata = n
                        break

            if node_metadata is not None:
                node_metadata.software_fault_infos[int(fault_info.fault_code)] = fault_info
                # Find which instance(s) this pod_ip belongs to
                for ins_id, ip in node_metadata.instance_pod_ips.items():
                    if ip == pod_ip:
                        affected_instance_ids.append(ins_id)
                logger.info(
                    "Reported software fault for node %s (instances %s): type=%s, engine_status=%s, fault_level=%s",
                    node_metadata.node_name,
                    affected_instance_ids,
                    fault_info.exception_type,
                    fault_info.engine_status,
                    fault_info.fault_level.name,
                )
            else:
                logger.warning(
                    "Node not found for software fault (pod_ip=%s), cannot report",
                    pod_ip,
                )
                return

        for instance_id in affected_instance_ids:
            self._refresh_instance_fault_level(instance_id)

        # Wake the strategy center — fault data changed
        with self.work_condition:
            self.work_condition.notify_all()

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

        # Find all nodes belonging to this instance that have any faults
        instance_nodes = []
        with self.lock:
            for node_metadata in self.nodes.values():
                if instance_id in node_metadata.instance_ids:
                    has_faults = (
                        len(node_metadata.hardware_fault_infos) > 0 or len(node_metadata.software_fault_infos) > 0
                    )
                    if has_faults:
                        instance_nodes.append(node_metadata)

        # Evaluate the instance's fault level from both hardware and software faults
        with instance_metadata.lock:
            # Get the best (highest) fault from hardware and software across all nodes
            highest_hw_fault = self._highest_hardware_fault(instance_nodes)
            highest_sw_fault = self._highest_software_fault(instance_nodes)

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
                InstanceManager().separate_instance(instance_id)
            else:
                if InstanceManager().is_instance_separated(instance_id):
                    InstanceManager().recover_instance(instance_id)

        # Persist data after instance fault level update
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if enable_persistence and not self.persist_data():
            logger.warning(
                "Failed to persist fault manager data to ETCD after instance fault level refresh for instance %d",
                instance_id,
            )

    def _highest_hardware_fault(self, instance_nodes: list[NodeMetadata]) -> FaultInfo | None:
        """Get the highest hardware fault across all nodes in an instance."""
        highest = None
        for node in instance_nodes:
            for fault_info in node.hardware_fault_infos.values():
                if highest is None or fault_info.fault_level > highest.fault_level:
                    highest = fault_info
        return highest

    def _highest_software_fault(self, instance_nodes: list[NodeMetadata]) -> FaultInfo | None:
        """Get the highest software fault across all nodes in an instance."""
        highest = None
        for node in instance_nodes:
            for fault_info in node.software_fault_infos.values():
                if highest is None or fault_info.fault_level > highest.fault_level:
                    highest = fault_info
        return highest

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
                self.work_condition.wait(timeout=check_interval)

        logger.info("Fault tolerance strategy center stopped")

    def _process_instance_strategy(self, ins_id: int) -> None:
        """
        Generate and manage the recovery strategy for an instance based on fault level.

        Strategy switch rules (by fault_level comparison, not just strategy class):
        1. new_level > current_level: UPGRADE — stop current strategy, start new one.
        2. new_level == current_level: SAME — keep current strategy (avoid intra-level churn).
        3. new_level < current_level: DOWNGRADE — ignore, keep higher-priority strategy.

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

        with ins_metadata.lock:
            fault_level = ins_metadata.fault_level
            fault_code = ins_metadata.fault_code
            current_strategy = ins_metadata.strategy
            current_level = ins_metadata.strategy_fault_level
            current_cls_name = current_strategy.__class__.__name__ if current_strategy else None

            new_strategy_cls = (
                self.strategies[fault_level](fault_code, ins_id, self.config)
                if fault_level != FaultLevel.HEALTHY
                else None
            )

            if new_strategy_cls is not None:
                should_switch = False
                if current_strategy is None:
                    should_switch = True
                elif fault_level > current_level:
                    current_strategy.stop()
                    ins_metadata.strategy = None
                    should_switch = True
                elif fault_level < current_level:
                    logger.debug(
                        "Instance %d: downgrade %s->%s ignored, keeping %s",
                        ins_id,
                        current_level.name,
                        fault_level.name,
                        current_cls_name,
                    )
                # else same level: no switch, no log

                if should_switch:
                    new_strategy = new_strategy_cls()
                    logger.info(
                        "Instance %d: strategy %s%s, level=%s, code=0x%08x",
                        ins_id,
                        "switch " if current_cls_name else "",
                        "%s->%s" % (current_cls_name, new_strategy_cls.__name__)
                        if current_cls_name
                        else new_strategy_cls.__name__,
                        fault_level.name,
                        fault_code,
                    )
                    self.executor.submit(new_strategy.execute, ins_id)
                    ins_metadata.strategy = new_strategy
                    ins_metadata.strategy_fault_level = fault_level

            # Check if the current strategy is finished
            need_post_completion = False
            if ins_metadata.strategy is not None:
                if ins_metadata.strategy.is_finished():
                    logger.info(
                        "Instance %d: strategy %s finished, level=%s, clearing faults",
                        ins_id,
                        ins_metadata.strategy.__class__.__name__,
                        ins_metadata.strategy_fault_level.name,
                    )
                    ins_metadata.strategy = None
                    ins_metadata.strategy_fault_level = FaultLevel.HEALTHY
                    need_post_completion = True
                else:
                    ins_metadata.fault_level = fault_level
                    ins_metadata.fault_code = fault_code

        # Post-completion actions outside ins_metadata.lock to avoid deadlock:
        # _clear_software_faults acquires self.lock, persist_data acquires self.lock,
        # _refresh_instance_fault_level acquires self.lock then ins_metadata.lock
        if need_post_completion:
            self._clear_software_faults(ins_id)
            with self.config_lock:
                enable_persistence = self.etcd_config.enable_etcd_persistence
            if enable_persistence and not self.persist_data():
                logger.warning(
                    "Failed to persist fault manager data after strategy completion for instance %d",
                    ins_id,
                )
            self._refresh_instance_fault_level(ins_id)

    def _clear_software_faults(self, instance_id: int) -> None:
        """Clear all software faults from nodes of an instance after strategy completion."""
        cleared = 0
        with self.lock:
            for node_metadata in self.nodes.values():
                if instance_id in node_metadata.instance_ids and node_metadata.software_fault_infos:
                    cleared += len(node_metadata.software_fault_infos)
                    node_metadata.software_fault_infos.clear()
        if cleared > 0:
            logger.info(
                "Cleared %d software faults for instance %d after strategy completion",
                cleared,
                instance_id,
            )
