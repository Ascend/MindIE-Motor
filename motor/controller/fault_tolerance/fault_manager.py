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
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from motor.config.controller import ControllerConfig
from motor.common.logger import get_logger
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.resources import InsStatus, Instance, ReadOnlyInstance
from motor.common.etcd.etcd_client import EtcdClient
from motor.controller.core import Observer, ObserverEvent, InstanceManager
from motor.controller.fault_tolerance.strategy import (
    StrategyBase,
    generate_strategy_map,
    NmSuicideStrategy,
    ScaleP2DStrategy,
)
from motor.controller.fault_tolerance.k8s.resource_monitor import ResourceMonitor
from motor.controller.fault_tolerance.k8s.k8s_client import K8sClient
from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.topology import FaultNormalizer, TopologyResolver
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, get_ft_runtime_store
from motor.controller.fault_tolerance.engine_recovery.contracts import (
    RecoveryContext,
    FailurePolicy,
    RecoveryPlan,
    RecoverySource,
    SoftwareFaultEvidence,
)
from motor.controller.fault_tolerance.fault_types import (
    FaultCategory,
    FaultInfo,
    FaultLevel,
    hardware_fault_identity,
    InstanceMetadata,
    instance_software_fault_key,
    NodeMetadata,
    OriginFaultLevel,
    pre_separate_fault_affects_instance,
    SoftwareFaultReportResult,
    software_evidence_signature,
)
from motor.controller.fault_tolerance.mixin.persistence import _PersistenceMixin
from motor.controller.fault_tolerance.mixin.resource_manager import (
    _ResourceManagerMixin,
)
from motor.controller.fault_tolerance.engine_recovery.planner import (
    scale_down_gate,
    engine_recovery_eligible,
    fast_recovery_gate,
    build_failure_plan,
    build_recovery_plan,
)
from motor.controller.fault_tolerance.engine_recovery.serving import ServingOverlay


logger = get_logger(__name__)


@dataclass(frozen=True)
class _InstanceFaultSnapshot:
    """One filtered view of the fault evidence currently owned by an instance."""

    instance: Instance | ReadOnlyInstance | None
    nodes: tuple[NodeMetadata, ...]
    hardware_faults: tuple[tuple[str, str, NodeMetadata, FaultInfo], ...]
    software_faults: tuple[FaultInfo, ...]
    hardware_versions: tuple[tuple[str, str], ...] = ()


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
        self._scale_p2d_job_locks: dict[str, Any] = {}

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
        # Kill→wait-native-scale-down→recycle→scale-up runs here without taking
        # metadata.strategy, so software-fault DpScaleDownStrategy can still run.
        # A not-done placeholder is published under metadata.lock before submit,
        # together with the strategy-slot check.
        self._problem_pod_recover_futures: dict[int, concurrent.futures.Future] = {}

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

    def get_instance_fault_level(self, instance_id: int) -> FaultLevel | None:
        """Read the effective fault level after fault attribution to this instance."""
        with self.lock:
            metadata = self.instances.get(instance_id)
        if metadata is None:
            return None
        with metadata.lock:
            return metadata.fault_level

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
                instance_metadata = self.instances.get(instance_id)
                if instance_metadata is not None and instance_metadata.software_fault_infos:
                    sw_max = max(
                        sw_max,
                        max(f.fault_level for f in instance_metadata.software_fault_infos.values()),
                    )
                result[node_name] = max(hw_max, sw_max)

            return result

    def get_scale_p2d_lock(self, job_name: str) -> Any:
        """Serialize only ScaleP2D workers for the same logical Decode job."""
        with self.lock:
            return self._scale_p2d_job_locks.setdefault(job_name, threading.Lock())

    def get_pending_scale_p2d_nodes(self, instance_id: int, job_name: str | None = None) -> dict[str, str] | None:
        """Return pending node -> episode tokens, independently of DP fault acknowledgement.

        None means no actionable hardware snapshot is available (keep legacy
        behavior); an empty dict means all observed faulty nodes were handled.
        Caller must not hold InstanceMetadata.lock.
        """
        with self.lock:
            faulty_nodes = [
                node
                for node in self.nodes.values()
                if instance_id in node.instance_ids
                and any(fault.fault_level >= FaultLevel.L3 for fault in node.hardware_fault_infos.values())
            ]
            if not faulty_nodes:
                return None
            return {
                node.node_name: node.scale_p2d_fault_generation
                for node in faulty_nodes
                if (job_name or node.instance_job_names.get(instance_id, "")) not in node.scale_p2d_handled_jobs
            }

    def mark_scale_p2d_nodes_handled(self, job_name: str, node_generations: dict[str, str]) -> None:
        """Acknowledge successful preemption without clearing real fault evidence."""
        with self.lock:
            for node_name, generation in node_generations.items():
                node = self.nodes.get(node_name)
                if node is not None and node.scale_p2d_fault_generation == generation:
                    node.scale_p2d_handled_jobs.add(job_name)
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if enable_persistence:
            self.persist_data()

    def _resolve_software_fault_target(
        self,
        fault_info: FaultInfo,
        pod_ip: str,
        instance_id: int | None,
        node_manager_port: str,
    ) -> tuple[int, str] | SoftwareFaultReportResult:
        """Resolve and validate the instance that owns a software fault report.

        Attribution is instance-based so docker-only deployments (no K8s
        NodeMetadata) work identically to Kubernetes ones. ``pod_ip`` and
        ``node_manager_port`` only verify the reporter belongs to the
        instance; they never locate a physical node.
        """
        instance_manager = InstanceManager()
        resolved_instance_id = instance_id
        resolved_nm_port = node_manager_port or ""

        if resolved_instance_id is None:
            # Legacy fallback for old NodeManagers that only send pod_ip.
            # Ambiguous when multiple instances share the pod IP.
            if not pod_ip:
                return SoftwareFaultReportResult(False, "missing instance_id and pod_ip")
            with self.lock:
                matches = {
                    ins_id
                    for node in self.nodes.values()
                    for ins_id, ip in node.instance_pod_ips.items()
                    if ip == pod_ip
                }
            if not matches:
                return SoftwareFaultReportResult(False, "no instance matches pod_ip")
            if len(matches) > 1:
                return SoftwareFaultReportResult(False, "pod_ip matches multiple instances; instance_id required")
            resolved_instance_id = next(iter(matches))

        with self.lock:
            instance_metadata = self.instances.get(resolved_instance_id)
        if instance_metadata is None:
            return SoftwareFaultReportResult(False, "instance not registered for fault tolerance")

        instance = instance_manager.get_instance(resolved_instance_id)
        if instance is None:
            return SoftwareFaultReportResult(False, "instance not found")

        if pod_ip and resolved_nm_port:
            reporter_known = any(
                nm.pod_ip == pod_ip and nm.port == resolved_nm_port for nm in instance.get_node_managers()
            )
            if not reporter_known:
                return SoftwareFaultReportResult(
                    False,
                    "reporter pod_ip/node_manager_port not registered on instance",
                )
        elif pod_ip:
            if not instance.has_node_mgr(pod_ip):
                return SoftwareFaultReportResult(False, "reporter pod_ip not registered on instance")

        engine_id = int(fault_info.engine_id or 0)
        if fault_info.engine_id is not None:
            endpoint_ids = {ep.id for ep in instance.get_all_endpoints(include_headless=True)}
            if endpoint_ids and engine_id not in endpoint_ids:
                return SoftwareFaultReportResult(False, "engine_id not registered on instance")

        fault_key = instance_software_fault_key(
            pod_ip or "",
            resolved_nm_port,
            engine_id,
        )
        return resolved_instance_id, fault_key

    def report_software_fault(
        self,
        fault_info: FaultInfo,
        pod_ip: str = "",
        instance_id: int | None = None,
        node_manager_port: str = "",
    ) -> SoftwareFaultReportResult:
        """Report a software fault at instance granularity.

        Stores the fault on the owning InstanceMetadata after validating the
        reporter identity. Returns whether the report was accepted so the
        NodeManager only treats accepted reports as delivered.
        """
        if fault_info.fault_category != FaultCategory.SOFTWARE:
            logger.warning(
                "report_software_fault called with non-software fault: %s",
                fault_info.fault_category,
            )
            return SoftwareFaultReportResult(False, "fault_category is not SOFTWARE")

        target = self._resolve_software_fault_target(fault_info, pod_ip, instance_id, node_manager_port)
        if isinstance(target, SoftwareFaultReportResult):
            logger.warning(
                "Rejected software fault report (pod_ip=%s, instance_id=%s): %s",
                pod_ip,
                instance_id,
                target.reason,
            )
            return target

        resolved_instance_id, fault_key = target
        with self.lock:
            instance_metadata = self.instances.get(resolved_instance_id)
            if instance_metadata is None:
                return SoftwareFaultReportResult(False, "instance not registered for fault tolerance")
            fault_info.instance_id = resolved_instance_id
            previous = instance_metadata.software_fault_infos.get(fault_key)
            if previous is None or software_evidence_signature(previous) != software_evidence_signature(fault_info):
                instance_metadata.evidence_sequence += 1
                instance_metadata.software_fault_versions[fault_key] = instance_metadata.evidence_sequence
            instance_metadata.software_fault_infos[fault_key] = fault_info.model_copy(deep=True)
            runtime_collection_eligible = instance_metadata.recovery_ready
            if fault_info.engine_status in (1, 2) and runtime_collection_eligible:
                now = time.time()
                if instance_metadata.fault_collection_started_at is None:
                    instance_metadata.fault_collection_started_at = now
                    instance_metadata.fault_collection_deadline = (
                        time.monotonic() + self.config.fault_tolerance_config.fault_collection_timeout_seconds
                    )
                if isinstance(fault_info.engine_id, int):
                    # A newer status supersedes the older one for the same
                    # engine (the storage key no longer carries fault_code),
                    # so the opposite observation timestamp must not survive
                    # the transition.
                    if fault_info.engine_status == 1:
                        instance_metadata.software_dead_observed_at.setdefault(fault_info.engine_id, now)
                        instance_metadata.software_unhealthy_observed_at.pop(fault_info.engine_id, None)
                    else:
                        instance_metadata.software_unhealthy_observed_at.setdefault(fault_info.engine_id, now)
                        instance_metadata.software_dead_observed_at.pop(fault_info.engine_id, None)
            engine_status_name = {
                0: "HEALTHY",
                1: "DEAD",
                2: "UNHEALTHY",
            }.get(fault_info.engine_status, "UNKNOWN")
            logger.debug(
                "Reported software fault for instance %d (key=%s): "
                "dp_rank=%s, type=%s, engine_status=%s(%s), fault_level=%s",
                resolved_instance_id,
                fault_key,
                fault_info.engine_id,
                fault_info.exception_type,
                fault_info.engine_status,
                engine_status_name,
                fault_info.fault_level.name,
            )

        self._refresh_instance_fault_level(resolved_instance_id)

        # Wake the strategy center — fault data changed
        with self.work_condition:
            self.work_condition.notify_all()
        return SoftwareFaultReportResult(True)

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
            instance = InstanceManager().get_instance(instance_id)
            if instance is None or not scale_down_gate(self.config.fault_tolerance_config, instance).allowed:
                return
            metadata.hardware_fault_observed_at = now
            metadata.hardware_correlation_deadline = (
                time.monotonic() + self.config.fault_tolerance_config.hardware_ft_correlation_window_sec
            )

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
            metadata.software_fault_infos.clear()
        with metadata.lock:
            metadata.ignored_pre_ready_hardware_faults.update(baseline)
            metadata.fault_collection_started_at = None
            metadata.fault_collection_deadline = None
            metadata.hardware_correlation_deadline = None
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
            hardware_versions = tuple((key, node.scale_p2d_fault_generation) for key, _, node, _ in hardware_faults)
            software_faults = tuple(metadata.software_fault_infos.values()) + tuple(
                # Legacy node-level software faults written by older NodeManagers.
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
            hardware_versions=hardware_versions,
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
            recovery_ready = instance_metadata.recovery_ready

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
        if (
            enable_dp_scale_down
            and recovery_ready
            and instance is not None
            and scale_down_gate(self.config.fault_tolerance_config, instance).allowed
        ):
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

            all_hw_faults = (
                [
                    fault
                    for fault_key, _, node, fault in snapshot.hardware_faults
                    if pre_separate_fault_affects_instance(fault, node, instance, hardware_type)
                    and fault_key not in handled_hardware_faults
                    and fault_key not in ignored_hardware_faults
                ]
                if recovery_ready
                else []
            )
            all_sw_faults = [fault for fault in snapshot.software_faults if recovery_ready or fault.engine_status == 1]

            highest_hw_fault = max(
                (
                    f
                    for f in all_hw_faults
                    if not (self.config.fault_tolerance_config.enable_fast_recovery and f.fault_level <= FaultLevel.L3)
                ),
                key=lambda f: f.fault_level,
                default=None,
            )
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

            if get_ft_runtime_store().is_reconfiguration_dispatched(instance_id):
                # Stop dispatch ended this instance's in-place recovery. Keep
                # observing real faults, but never revive it or preserve an old
                # L3 code while waiting for the replacement to register.
                instance_metadata.fault_level = target.fault_level if target else FaultLevel.HEALTHY
                instance_metadata.fault_code = int(target.fault_code) if target else 0
                return

            if target is None:
                instance_metadata.fault_collection_started_at = None
                instance_metadata.fault_collection_deadline = None
                instance_metadata.hardware_correlation_deadline = None
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

            if hardware_scale_down_candidate and target.fault_category == FaultCategory.HARDWARE:
                native = self.strategies[target.fault_level](int(target.fault_code), instance_id, self.config)
                if native is NmSuicideStrategy:
                    hardware_scale_down_candidate = False

            if instance_metadata.fault_level > FaultLevel.L2:
                if target.fault_category == FaultCategory.HARDWARE and hardware_scale_down_candidate:
                    logger.info("Deferring instance %d isolation to hardware DP scale-down", instance_id)
                else:
                    InstanceManager().separate_instance(instance_id)
            elif InstanceManager().is_instance_separated(instance_id):
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
                try:
                    self._process_instance_strategy(instance_id)
                except Exception:
                    logger.exception("Failed to process recovery strategy for instance %d", instance_id)

            with self.config_lock:
                check_interval = self.strategy_center_check_interval
            with self.work_condition:
                if not self._strategy_finished_event.is_set():
                    self.work_condition.wait(timeout=check_interval)
                self._strategy_finished_event.clear()

        logger.info("Fault tolerance strategy center stopped")

    def _build_recovery_context(
        self,
        instance_id: int,
    ) -> RecoveryContext:
        """Freeze the authoritative fault candidates before a strategy starts."""
        snapshot = self._collect_instance_faults(instance_id)
        if snapshot is None or snapshot.instance is None:
            return RecoveryContext(engine_recovery_eligible=False)
        instance = snapshot.instance
        with self.lock:
            metadata = self.instances.get(instance_id)
        if metadata is not None:
            with metadata.lock:
                recovery_ready = metadata.recovery_ready
                handled_hardware_faults = set(metadata.handled_hardware_faults)
                ignored_hardware_faults = set(metadata.ignored_pre_ready_hardware_faults)
                collection_deadline = metadata.fault_collection_deadline
                hardware_deadline = metadata.hardware_correlation_deadline
                started_at = metadata.fault_collection_started_at
                hardware_started_at = metadata.hardware_fault_observed_at
                dead_observed_at = dict(metadata.software_dead_observed_at)
                unhealthy_observed_at = dict(metadata.software_unhealthy_observed_at)
        else:
            recovery_ready = False
            handled_hardware_faults = set()
            ignored_hardware_faults = set()
            collection_deadline = None
            hardware_deadline = None
            started_at = None
            hardware_started_at = None
            dead_observed_at = {}
            unhealthy_observed_at = {}
        hardware_faults = [
            (fault_key, pod_ip, fault.model_copy(deep=True))
            for fault_key, pod_ip, _, fault in snapshot.hardware_faults
            if fault.fault_level >= FaultLevel.L1
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
            time.monotonic() >= collection_deadline
            if collection_deadline is not None
            else time.time() - started_at >= self.config.fault_tolerance_config.fault_collection_timeout_seconds
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
            hardware_first and (hardware_candidates & timely_dead_ranks or timely_unhealthy_ranks)
        )
        hardware_wait_timed_out = bool(
            hardware_first
            and not hardware_ft_observed
            and hardware_started_at is not None
            and (
                time.monotonic() >= hardware_deadline
                if hardware_deadline is not None
                else time.time() - hardware_started_at >= hardware_window
            )
        )
        # An abnormal engine heartbeat removes serving eligibility, not necessarily
        # engine-side recovery eligibility. Keep the startup and NM-loss boundaries.
        # Feature switches must not turn an ACTIVE native instance into an ineligible one.
        eligible = instance.status == InsStatus.ACTIVE
        if self.config.fault_tolerance_config.enable_fault_tolerance and (
            self.config.fault_tolerance_config.enable_fast_recovery
            or scale_down_gate(self.config.fault_tolerance_config, instance).allowed
        ):
            eligible = engine_recovery_eligible(
                instance,
                runtime or {},
                recovery_ready=recovery_ready,
                engine_fault_observed=engine_fault_observed,
                separated=InstanceManager().is_instance_separated(instance_id) is True,
            )
        recovery_hardware = []
        for key, pod_ip, node, fault in snapshot.hardware_faults:
            candidates, _ = FaultNormalizer.hardware_dp_ranks(instance, [(key, pod_ip, fault)])
            if candidates and candidates <= committed_ranks:
                continue
            recovery_hardware.append(fault)
        logger.info(
            "Instance %d recovery hardware evidence: faults=%s, handled=%s, ignored=%s, committed_ranks=%s",
            instance_id,
            [
                (key, pod_ip, int(fault.fault_code), fault.fault_level.name, fault.npu_name)
                for key, pod_ip, _, fault in snapshot.hardware_faults
            ],
            sorted(handled_hardware_faults),
            sorted(ignored_hardware_faults),
            sorted(committed_ranks),
        )
        return RecoveryContext(
            pending_removed_ranks=tuple(sorted(dead_ranks)),
            source="hardware" if hardware_first else "software",
            fault_keys=tuple(
                sorted(
                    mapped_fault_keys | {key for key, _, fault in hardware_faults if fault.fault_level <= FaultLevel.L3}
                )
            ),
            hardware_evidence_versions=tuple(
                sorted((key, generation) for key, generation in snapshot.hardware_versions if key in mapped_fault_keys)
            ),
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
            engine_recovery_eligible=eligible,
            fast_recovery_allowed=fast_recovery_gate(self.config.fault_tolerance_config, instance).allowed,
            hardware_fault_codes=tuple(sorted({int(f.fault_code) for f in recovery_hardware})),
            recovery_hardware_severe=any(f.fault_level >= FaultLevel.L4 for f in recovery_hardware),
            hardware_mapping_complete=not has_unmapped_hardware,
            hardware_correlation_deadline=hardware_deadline
            if hardware_deadline is not None
            else (
                time.monotonic() + max(0.0, hardware_window - (time.time() - hardware_started_at))
                if hardware_started_at is not None
                else None
            ),
            readiness_deadline=collection_deadline
            if collection_deadline is not None
            else (
                time.monotonic()
                + max(
                    0.0,
                    self.config.fault_tolerance_config.fault_collection_timeout_seconds - (time.time() - started_at),
                )
                if started_at is not None
                else None
            ),
        )

    def _build_scale_down_context(self, instance_id: int) -> RecoveryContext:
        """Compatibility entry point; common recovery owns evidence construction."""
        return self._build_recovery_context(instance_id)

    def get_recovery_hardware_evidence(self, instance_id: int) -> tuple[tuple[int, ...], bool]:
        """Current owned hardware evidence for strategy-specific invalidation."""
        context = self._build_recovery_context(instance_id)
        if not context.engine_recovery_eligible:
            raise RuntimeError("instance is no longer eligible for in-place fast recovery")
        return context.hardware_fault_codes, context.hardware_fault_observed or context.recovery_hardware_severe

    def get_pending_hbm_uce_faults(self, instance_id: int) -> list[str]:
        """Conservatively verify UCE clearance across all hosting nodes."""
        with self.lock:
            nodes = [node for node in self.nodes.values() if instance_id in node.instance_ids]
            if instance_id not in self.instances or not nodes:
                raise RuntimeError("Cannot verify HBM UCE clearance: missing node evidence")
            return sorted(
                hardware_fault_identity(node.node_name, fault)
                for node in nodes
                for fault in node.hardware_fault_infos.values()
                if fault.fault_code == 0x80E01801
            )

    def _build_engine_recovery_plan(
        self,
        instance_id: int,
        context: RecoveryContext,
        *,
        fault_state: tuple[FaultLevel, int] | None = None,
        scale_down_allowed: bool | None = None,
    ) -> RecoveryPlan:
        """Use the same planning path for initial selection and preparation updates."""
        if fault_state is None:
            with self.lock:
                metadata = self.instances.get(instance_id)
            if metadata is None:
                raise RuntimeError("recovery instance was removed")
            with metadata.lock:
                fault_state = (metadata.fault_level, metadata.fault_code)
        # Initial selection already holds metadata.lock: reuse its snapshot rather than acquire FM.lock beneath it.
        level, code = fault_state
        native_strategy_cls = (
            self.strategies[level](code, instance_id, self.config) if level != FaultLevel.HEALTHY else None
        )
        if scale_down_allowed is None:
            instance = InstanceManager().get_instance(instance_id)
            scale_down_allowed = (
                instance is not None and scale_down_gate(self.config.fault_tolerance_config, instance).allowed
            )
        return build_recovery_plan(
            self.config, context, native_strategy_cls, level, scale_down_allowed=scale_down_allowed
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

        Settle the previous task, update an active recovery, or select and submit
        one new plan. Active tasks evaluate new evidence without overlapping a
        replacement task. Successful settlement consumes only observed software
        evidence and re-evaluates the remaining faults.
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

        if get_ft_runtime_store().is_reconfiguration_dispatched(ins_id):
            with ins_metadata.lock:
                # L4-L6 native handling (notably Decode ScaleP2D) must remain
                # available if fresh severe hardware evidence arrives later.
                if ins_metadata.fault_level <= FaultLevel.L3:
                    return

        self._update_relaunch_relapse_guard(ins_id, ins_metadata)
        evidence = self._capture_software_evidence(ins_id)
        instance = InstanceManager().get_instance(ins_id)
        shrink_allowed = instance is not None and scale_down_gate(self.config.fault_tolerance_config, instance).allowed
        superseded = self._is_superseded_instance(ins_id)
        task_submitted = False
        with ins_metadata.lock:
            # A live strategy still owns recovery, even if hardware clears first.
            has_recovery_fault = ins_metadata.fault_level > FaultLevel.L1
            has_strategy = ins_metadata.strategy is not None
            recovery_failed = ins_metadata.prev_strategy_failed
        scale_p2d_pending = self.get_pending_scale_p2d_nodes(ins_id) if has_recovery_fault else None
        recovery_context = RecoveryContext()
        if has_recovery_fault and not superseded:
            recovery_context = self._build_recovery_context(ins_id)
            if recovery_context.already_removed_fault_keys:
                with ins_metadata.lock:
                    ins_metadata.handled_hardware_faults.update(recovery_context.already_removed_fault_keys)
                if not (recovery_context.hardware_fault_observed or recovery_context.engine_fault_observed):
                    self._refresh_instance_fault_level(ins_id)
                    return

        if not has_recovery_fault and not superseded and not recovery_failed and not has_strategy:
            runtime = get_ft_runtime_store().get(ins_id)
            if runtime is not None and runtime["phase"] == FtPhase.WAITING_ENGINE_FAULT.value:
                get_ft_runtime_store().resume_after_fault_clear(ins_id)
                runtime = get_ft_runtime_store().get(ins_id)
            if (
                runtime is not None
                and runtime["phase"] in {FtPhase.NORMAL.value, FtPhase.SCALED_DOWN_RUNNING.value}
                and not runtime["serving_published"]
            ):
                instance = InstanceManager().get_instance(ins_id)
                if instance is not None and ServingOverlay.publish(instance):
                    logger.info("Instance %d serving restored after fault recovery (HEALTHY/L1)", ins_id)
                else:
                    logger.warning("Instance %d serving publication pending; will retry", ins_id)
                return
        with ins_metadata.lock:
            fault_level = ins_metadata.fault_level
            fault_code = ins_metadata.fault_code
            current_strategy = ins_metadata.strategy

            # Let the current owner evaluate new evidence and invalidate its plan if needed.
            # A replacement task must wait for this strategy and its Future to return.
            if current_strategy is not None:
                current_strategy.evaluate_update(recovery_context)
                return

            # Elastic scale-up in progress (scale → bootstrap → joiners → resume).
            # Joiner faults stay on the scale-up deadline. A survivor hard fault
            # aborts inside the scale-up worker and uses its reconfiguration exit.
            runtime_guard = get_ft_runtime_store().get(ins_id)
            # Recovery failure must still escalate after hardware/software evidence clears.
            scale_up_left_reconfiguring = (
                self.config.fault_tolerance_config.enable_dp_scale_up
                and ins_metadata.failure_result.scale_up_reconfiguration
                and runtime_guard is not None
                and runtime_guard.get("phase") == FtPhase.RECONFIGURING.value
            )
            if runtime_guard is not None and runtime_guard.get("phase") == FtPhase.WARMING_UP.value:
                logger.info(
                    "Instance %d: skip new recovery plan while WARMING_UP (scale-up in progress)",
                    ins_id,
                )
                return

            plan = self._select_recovery_plan(
                ins_id,
                ins_metadata,
                recovery_context,
                fault_state=(fault_level, fault_code),
                superseded=superseded,
                shrink_allowed=shrink_allowed,
                scale_up_left_reconfiguring=scale_up_left_reconfiguring,
                scale_p2d_pending=scale_p2d_pending,
            )
            if plan.strategy is ScaleP2DStrategy and scale_p2d_pending == {}:
                # Suppress repeated Prefill preemption, not fault evaluation or other recovery.
                return
            task_submitted = self._submit_recovery_task(
                ins_id,
                ins_metadata,
                plan,
                evidence,
                fault_state=(fault_level, fault_code),
                runtime_guard=runtime_guard,
            )

        if task_submitted:
            with self.config_lock:
                enable_persistence = self.etcd_config.enable_etcd_persistence
            if enable_persistence and not self.persist_data():
                logger.debug("Failed to persist recovery plan for instance %d", ins_id)

    def _select_recovery_plan(
        self,
        ins_id: int,
        metadata: InstanceMetadata,
        recovery_context: RecoveryContext,
        *,
        fault_state: tuple[FaultLevel, int],
        superseded: bool,
        shrink_allowed: bool,
        scale_up_left_reconfiguring: bool,
        scale_p2d_pending: dict[str, str] | None,
    ) -> RecoveryPlan:
        """Select one complete plan while the caller holds metadata.lock."""
        fault_level, fault_code = fault_state
        if superseded:
            # Most stale-instance strategies may stop the replacement's
            # NodeManagers and must be skipped. ScaleP2D is the exception:
            # it resolves the current D by job name and only releases
            # Prefill capacity, so it can verify whether reconfiguration
            # actually recovered the replacement D.
            native_strategy_cls = (
                self.strategies[fault_level](fault_code, ins_id, self.config) if fault_level >= FaultLevel.L4 else None
            )
            new_strategy_cls = native_strategy_cls if native_strategy_cls is ScaleP2DStrategy else None
            if new_strategy_cls is None:
                logger.debug(
                    "Skip strategy for superseded instance %d (job already has a newer instance)",
                    ins_id,
                )
        elif (
            metadata.relaunch_relapse_guard
            and not metadata.prev_strategy_failed
            and metadata.prev_strategy_name != "InstanceReconfigurationStrategy"
            and recovery_context.engine_fault_observed
            and fault_level <= FaultLevel.L3
        ):
            from motor.controller.fault_tolerance.strategy import InstanceReconfigurationStrategy

            logger.warning("Instance %d fault recurred after fast-recovery fallback relaunch; reconfiguring", ins_id)
            new_strategy_cls = InstanceReconfigurationStrategy
        elif metadata.prev_strategy_failed and (
            fault_level != FaultLevel.HEALTHY
            or scale_up_left_reconfiguring
            or metadata.failure_result.evaluate_without_fault
        ):
            native_after_failure = (
                self.strategies[fault_level](fault_code, ins_id, self.config)
                if metadata.failure_result.failure_policy == FailurePolicy.RECONFIGURATION
                and fault_level >= FaultLevel.L4
                and recovery_context.hardware_fault_observed
                else None
            )
            if scale_up_left_reconfiguring:
                from motor.controller.fault_tolerance.strategy import InstanceReconfigurationStrategy

                new_strategy_cls = InstanceReconfigurationStrategy
            else:
                new_strategy_cls = build_failure_plan(
                    metadata.failure_result,
                    self.config,
                    recovery_context,
                    generated_strategy=native_after_failure,
                )
            logger.info(
                "Instance %d recovery decision evidence: stage=failure_fallback, previous_strategy=%s, "
                "failure_result=%s, enable_engine_relaunch=%s, context=%s, selected=%s",
                ins_id,
                metadata.prev_strategy_name,
                metadata.failure_result,
                self.config.fault_tolerance_config.enable_engine_relaunch,
                recovery_context,
                new_strategy_cls.__name__ if new_strategy_cls else "NONE",
            )
        elif fault_level != FaultLevel.HEALTHY:
            plan = self._build_engine_recovery_plan(
                ins_id,
                recovery_context,
                fault_state=(fault_level, fault_code),
                scale_down_allowed=shrink_allowed,
            )
            new_strategy_cls = plan.strategy
            if new_strategy_cls is None and plan.context.engine_fault_observed and shrink_allowed:
                get_ft_runtime_store().transition(ins_id, phase=FtPhase.WAITING_ENGINE_FAULT)
            repeated_scale_p2d = new_strategy_cls is ScaleP2DStrategy and scale_p2d_pending == {}
            plan_log = logger.info if new_strategy_cls is not None and not repeated_scale_p2d else logger.debug
            plan_log(
                "Instance %d recovery plan: source=%s, strategy=%s, fallback=%s, "
                "removed_ranks=%s, all_unhealthy=%s, collection_complete=%s, "
                "collection_timed_out=%s, hardware_wait_timed_out=%s",
                ins_id,
                plan.source.value,
                new_strategy_cls.__name__ if new_strategy_cls else "WAIT",
                plan.fallback.__name__ if plan.fallback else "NONE",
                plan.context.pending_removed_ranks,
                plan.context.all_dp_unhealthy,
                plan.context.collection_complete,
                plan.context.collection_timed_out,
                plan.context.hardware_wait_timed_out,
            )
            return plan
        else:
            new_strategy_cls = None
        # Direct safety/failure actions have no additional fallback recommendation.
        return RecoveryPlan(RecoverySource(recovery_context.source), new_strategy_cls, recovery_context, None)

    def _submit_recovery_task(
        self,
        ins_id: int,
        metadata: InstanceMetadata,
        plan: RecoveryPlan,
        evidence: SoftwareFaultEvidence,
        *,
        fault_state: tuple[FaultLevel, int],
        runtime_guard: dict[str, Any] | None,
    ) -> bool:
        """Bind and submit the selected plan, rolling back on rejection under metadata.lock."""
        fault_level, fault_code = fault_state
        new_strategy_cls = plan.strategy
        if new_strategy_cls is None:
            return False
        strategy_context = plan.context
        if new_strategy_cls.failure_policy == FailurePolicy.RELAUNCH and metadata.failure_result.protect_relaunch:
            metadata.relaunch_relapse_guard = True
            metadata.relaunch_healthy_since = None
        from motor.controller.fault_tolerance.engine_recovery.runner import EngineRecoveryStrategy

        if new_strategy_cls.runs_under_recovery_owner:
            new_strategy_cls = EngineRecoveryStrategy
            if plan.fallback is not None:
                strategy_context = replace(plan.context, fallback_strategy=plan.fallback.__name__)
        new_strategy = new_strategy_cls()
        new_strategy.bind_config(self.config)
        new_strategy.bind_context(strategy_context)
        preparation_required = new_strategy.requires_recovery_preparation
        logger.info(
            "Instance %d: strategy %s, level=%s, code=0x%08x",
            ins_id,
            new_strategy_cls.__name__,
            fault_level.name,
            fault_code,
        )
        self._prepare_recovery_task(ins_id, new_strategy, evidence)
        if preparation_required:
            new_strategy.context_provider = lambda: self._build_recovery_context(ins_id)
            new_strategy.evidence_provider = lambda: self._capture_software_evidence(ins_id)
            if isinstance(new_strategy, EngineRecoveryStrategy):
                new_strategy.plan_provider = lambda context: self._build_engine_recovery_plan(ins_id, context)
        metadata.strategy = new_strategy
        try:
            future = self.executor.submit(self._execute_strategy, new_strategy, ins_id)
        except Exception:
            metadata.strategy = None
            if preparation_required:
                get_ft_runtime_store().transition_if_owned(
                    ins_id,
                    expected_recovery_id=new_strategy.recovery_id,
                    phase=(runtime_guard or {}).get("phase", FtPhase.NORMAL.value),
                    recovery_paused=bool((runtime_guard or {}).get("recovery_paused", False)),
                    serving_published=bool((runtime_guard or {}).get("serving_published", True)),
                    pending_removed_ranks=list((runtime_guard or {}).get("pending_removed_ranks") or []),
                    stage="failed",
                    last_error="failed to submit engine recovery task",
                )
            raise
        future.add_done_callback(self._on_strategy_finished)
        metadata.strategy = new_strategy
        metadata.strategy_future = future
        metadata.strategy_fault_level = fault_level
        return True

    @staticmethod
    def _engines_normal(ins_id: int) -> bool:
        """Probe strict readiness; loading or unreachable engines are not healthy."""
        from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient

        instance = InstanceManager().get_instance(ins_id)
        nodes = list(instance.get_node_managers()) if instance is not None else []
        try:
            return bool(nodes) and all(NodeManagerApiClient.query_status(node).get("status") for node in nodes)
        except Exception as error:
            logger.debug("Instance %d readiness probe failed: %s", ins_id, error)
            return False

    def _update_relaunch_relapse_guard(self, ins_id: int, metadata: InstanceMetadata) -> None:
        """Keep relapse protection through model loading and a stable healthy window."""
        with metadata.lock:
            if not metadata.relaunch_relapse_guard:
                return
            if metadata.fault_level > FaultLevel.L1 or metadata.prev_strategy_failed:
                metadata.relaunch_healthy_since = None
                return
        healthy = self._engines_normal(ins_id)
        now = time.time()
        window = self.config.fault_tolerance_config.engine_relaunch_stability_sec
        with metadata.lock:
            since = metadata.relaunch_healthy_since
            if not healthy or metadata.fault_level > FaultLevel.L1:
                metadata.relaunch_healthy_since = None
                if since is not None:
                    logger.info("Instance %d relaunch stability window reset", ins_id)
            elif since is None:
                metadata.relaunch_healthy_since = now
                logger.info("Instance %d engines NORMAL; observing health for %ss", ins_id, window)
            elif now - since >= window:
                metadata.relaunch_relapse_guard = False
                metadata.relaunch_healthy_since = None
                logger.info("Instance %d healthy for %ss; relaunch relapse protection released", ins_id, window)

    def _complete_finished_strategy(self, ins_id: int, ins_metadata: InstanceMetadata) -> bool:
        """Finish one returned strategy before evaluating the next recovery round."""
        clear_software_faults = False
        with self.lock:
            hardware_versions = {
                hardware_fault_identity(node.node_name, fault): node.scale_p2d_fault_generation
                for node in self.nodes.values()
                if ins_id in node.instance_ids
                for fault in node.hardware_fault_infos.values()
            }
        with ins_metadata.lock:
            strategy = ins_metadata.strategy
            future = ins_metadata.strategy_future
            if (
                ins_metadata.strategy_settling
                or strategy is None
                or not strategy.is_finished()
                or (future is not None and not future.done())
            ):
                return False
            logger.info(
                "Instance %d: strategy %s finished, level=%s",
                ins_id,
                strategy.__class__.__name__,
                ins_metadata.strategy_fault_level.name,
            )
            ins_metadata.strategy_settling = True
            completed_context = strategy.strategy_context
            if ins_metadata.strategy_preempted:
                ins_metadata.strategy_preempted = False
            else:
                ins_metadata.prev_strategy_failed = strategy.is_failed()
                ins_metadata.prev_strategy_name = strategy.completed_strategy_name
                ins_metadata.prev_recovery_result = strategy.recovery_result
                ins_metadata.prev_strategy_fallback = (
                    getattr(strategy, "failure_fallback", "") if ins_metadata.prev_strategy_failed else ""
                )
                if (
                    not ins_metadata.prev_strategy_failed
                    and not isinstance(strategy, ScaleP2DStrategy)
                    and isinstance(completed_context, RecoveryContext)
                    and completed_context.fault_keys
                ):
                    observed_versions = dict(completed_context.hardware_evidence_versions)
                    ins_metadata.handled_hardware_faults.update(
                        key
                        for key in completed_context.fault_keys
                        if not observed_versions or hardware_versions.get(key) == observed_versions.get(key)
                    )
                clear_software_faults = not ins_metadata.prev_strategy_failed
        try:
            # These operations acquire FaultManager locks and must stay outside
            # InstanceMetadata.lock.
            if clear_software_faults:
                self._consume_software_evidence(ins_id, strategy.consumed_evidence)
            with self.config_lock:
                enable_persistence = self.etcd_config.enable_etcd_persistence
            if enable_persistence and not self.persist_data():
                logger.debug(
                    "Failed to persist fault manager data after strategy completion for instance %d",
                    ins_id,
                )
            self._refresh_instance_fault_level(ins_id)
            get_ft_runtime_store().settle_recovery(ins_id, strategy.recovery_id, failed=strategy.is_failed())
        finally:
            with ins_metadata.lock:
                ins_metadata.strategy = None
                ins_metadata.strategy_future = None
                ins_metadata.strategy_fault_level = FaultLevel.HEALTHY
                ins_metadata.strategy_settling = False
        return True

    def _recovery_round_busy(self, instance_id: int, metadata: InstanceMetadata) -> bool:
        """Whether a user recovery round is already committed for this instance.

        Caller holds ``metadata.lock``. The strategy slot and the kill-worker
        future are both read here so the two checks cannot observe a stale
        "idle" state across separate critical sections.
        """
        if metadata.strategy_settling:
            return True
        current = metadata.strategy
        if current is not None:
            return True
        prior = self._problem_pod_recover_futures.get(instance_id)
        return prior is not None and not prior.done()

    def _try_kill_problem_pod_workers(
        self,
        instance_id: int,
        metadata: "InstanceMetadata",
        original: list[int],
        expanded: set[int],
        target_dp: int,
        *,
        phase: Any,
        seed: set[int],
        to_remove: list[int],
    ) -> dict[str, Any] | None:
        """Accept kill→wait-native-scale-down→recycle→scale-up without owning FT strategy.

        Soft-inject / ``execute()`` are intentionally not used here: native
        software-fault planning still owns ``DpScaleDownStrategy``. This helper
        only kicks workers on the problem Pods and then waits for that commit
        before recycling empty shells and elastic scale-up. Returns a result
        dict, or ``None`` when another recovery round is already in progress.

        The strategy slot and ``_problem_pod_recover_futures`` are checked and
        the future is reserved under one ``metadata.lock`` hold. ``transition``
        and ``executor.submit`` stay outside that lock: persistence takes
        ``FaultManager.lock``, and the opposite order deadlocks. The
        reservation is a not-done placeholder, so a concurrent
        ``start_user_recovery`` or user-strategy submit observes this round
        before the real future is swapped in. This path still does not set
        ``metadata.strategy``; native software-fault planning must be able to
        commit scale-down while the kill worker waits.
        """
        from motor.controller.fault_tolerance.strategy import DpScaleDownStrategy

        reservation: concurrent.futures.Future = concurrent.futures.Future()
        with metadata.lock:
            if self._recovery_round_busy(instance_id, metadata):
                return None
            self._problem_pod_recover_futures[instance_id] = reservation

        try:
            strategy = DpScaleDownStrategy()
            strategy.bind_config(self.config)
            get_ft_runtime_store().transition(
                instance_id,
                original_dp_ranks=list(original),
                pending_removed_ranks=sorted(expanded),
                last_error=None,
            )
            future = self.executor.submit(
                strategy.run_problem_pod_recover,
                instance_id,
                list(original),
                expanded,
                target_dp,
                self.config.fault_tolerance_config,
            )
        except Exception:
            with metadata.lock:
                if self._problem_pod_recover_futures.get(instance_id) is reservation:
                    self._problem_pod_recover_futures.pop(instance_id, None)
            raise

        with metadata.lock:
            if self._problem_pod_recover_futures.get(instance_id) is reservation:
                self._problem_pod_recover_futures[instance_id] = future

        logger.info(
            "Instance %d: user recovery action=kill_problem_pod_workers "
            "seed=%s expanded=%s target_dp=%s to_remove=%s "
            "(wait native scale-down, then recycle + scale-up)",
            instance_id,
            sorted(seed),
            sorted(expanded),
            target_dp,
            to_remove,
        )
        return {
            "instance_id": instance_id,
            "action": "kill_problem_pod_workers",
            "ok": True,
            "accepted": True,
            "target_dp_size": target_dp,
            "expanded_dp_ranks": sorted(expanded),
            "removed_dp_ranks": list(to_remove),
            "phase": phase,
        }

    def _accept_empty_shell_scale_up(
        self,
        instance_id: int,
        metadata: "InstanceMetadata",
        instance: Any,
        strategy: Any,
        original: list[int],
        dead: list[int],
        phase: Any,
    ) -> dict[str, Any] | None:
        """Recycle empty shells and elastic scale-up.

        The strategy slot is reserved under one ``metadata.lock`` hold.
        ``transition`` and ``executor.submit`` stay outside that lock:
        persistence takes ``FaultManager.lock``, and the opposite order
        deadlocks. The reservation is a not-done placeholder, so a concurrent
        ``start_user_recovery`` observes this round before the real future is
        swapped in. Returns ``None`` when another recovery round is in progress.
        """
        reservation: concurrent.futures.Future = concurrent.futures.Future()
        with metadata.lock:
            if self._recovery_round_busy(instance_id, metadata):
                return None
            previous = (
                metadata.strategy,
                metadata.strategy_future,
                metadata.strategy_fault_level,
                metadata.prev_strategy_failed,
            )
            metadata.strategy = strategy
            metadata.strategy_future = reservation
            metadata.strategy_fault_level = FaultLevel.L2
            metadata.prev_strategy_failed = False
        recyclable = TopologyResolver.recyclable_pods(instance, set(dead))
        survivors = {int(rank) for rank in original} - {int(rank) for rank in dead}
        try:
            strategy.recovery_action = "SCALE_UP"
            self._prepare_recovery_task(instance_id, strategy, self._capture_software_evidence(instance_id))
            get_ft_runtime_store().transition(instance_id, phase=FtPhase.SCALED_DOWN_RUNNING)
            future = self.executor.submit(
                self._execute_strategy,
                strategy,
                instance_id,
                original,
                set(dead),
                self.config.fault_tolerance_config,
                survivors,
                operation=strategy.run_scale_up_recovery,
            )
            future.add_done_callback(self._on_strategy_finished)
        except Exception:
            with metadata.lock:
                if metadata.strategy_future is reservation:
                    (
                        metadata.strategy,
                        metadata.strategy_future,
                        metadata.strategy_fault_level,
                        metadata.prev_strategy_failed,
                    ) = previous
            raise
        with metadata.lock:
            if metadata.strategy_future is reservation:
                metadata.strategy_future = future
        logger.info(
            "Instance %d: user recovery action=scale_up ranks=%s recyclable=%s",
            instance_id,
            original,
            recyclable,
        )
        return {
            "instance_id": instance_id,
            "action": "scale_up",
            "original_dp_ranks": original,
            "phase": phase,
        }

    def start_user_recovery(self) -> list[dict[str, Any]]:
        """User recover for an instance that has already scaled down.

        Expand the dead seed to the problem Pods. If workers remain, kill them,
        wait for native FT to commit scale-down, recycle empty shells, then
        elastic expand. If the problem Pods are already empty shells, recycle
        and elastic expand only. Survivors keep original DP ids; joiners fill
        the missing ranks.

        A healthy instance with no prior FT shrink is left alone. Killing its
        high-suffix workers from recover was a debug entry and is not part of
        this path.
        """
        from motor.controller.fault_tolerance.strategy import (
            DpScaleDownStrategy,
            InstanceReconfigurationStrategy,
        )

        results: list[dict[str, Any]] = []
        in_progress = 0
        with self.lock:
            instance_ids = list(self.instances)
        for instance_id in instance_ids:
            runtime = get_ft_runtime_store().get(instance_id) or {}
            phase = runtime.get("phase")
            dead = list(runtime.get("dead_committed") or [])
            original = list(runtime.get("original_dp_ranks") or [])
            if phase in (FtPhase.SCALED_DOWN_RUNNING.value, FtPhase.WARMING_UP.value) or dead:
                instance = InstanceManager().get_instance(instance_id)
                if instance is None:
                    continue
                if not original:
                    try:
                        original = TopologyResolver.original_dp_ranks(instance)
                    except ValueError:
                        original = sorted({ep.id for ep in instance.get_all_endpoints(include_headless=False)})
                metadata = self.instances.get(instance_id)
                if metadata is None:
                    continue
                seed = set(dead)
                try:
                    expanded = TopologyResolver.expand_problem_pod_ranks(instance, seed)
                except ValueError:
                    expanded = set(seed)
                to_remove = sorted(expanded - set(dead))
                strategy = DpScaleDownStrategy()
                strategy.bind_config(self.config)
                # Whole problem Pods: kill remaining workers; FT finishes scale-down.
                target_dp = TopologyResolver.elastic_pod_cut_target_dp(original, expanded)
                if to_remove and target_dp is None:
                    logger.warning(
                        "Instance %d: problem Pod ranks %s leave no elastic survivor; "
                        "kill-worker recover unsupported — falling back to reconfigure",
                        instance_id,
                        sorted(expanded),
                    )
                    try:
                        self._submit_user_strategy(
                            metadata,
                            InstanceReconfigurationStrategy(),
                            None,
                        )
                    except RuntimeError:
                        in_progress += 1
                        continue
                    results.append(
                        {
                            "instance_id": instance_id,
                            "action": "reconfigure",
                            "reason": "problem_pod_cut_leaves_no_survivor",
                            "expanded_dp_ranks": sorted(expanded),
                            "phase": phase,
                        }
                    )
                    continue
                if to_remove and target_dp is not None:
                    item = self._try_kill_problem_pod_workers(
                        instance_id,
                        metadata,
                        original,
                        expanded,
                        target_dp,
                        phase=phase,
                        seed=seed,
                        to_remove=to_remove,
                    )
                    if item is None:
                        in_progress += 1
                    else:
                        results.append(item)
                    continue
                # Problem pods already empty shells: recycle + wait for join/warmup.
                # transition/submit stay outside metadata.lock; see _accept_empty_shell_scale_up.
                item = self._accept_empty_shell_scale_up(
                    instance_id,
                    metadata,
                    instance,
                    strategy,
                    original,
                    dead,
                    phase,
                )
                if item is None:
                    in_progress += 1
                    continue
                results.append(item)
                continue

            with self.lock:
                metadata = self.instances.get(instance_id)
            if metadata is None:
                continue
            with metadata.lock:
                faulted = metadata.fault_level != FaultLevel.HEALTHY
            context = self._build_recovery_context(instance_id)
            ranks = set(context.pending_removed_ranks)
            if not faulted and not ranks and not context.engine_fault_observed and not context.hardware_fault_observed:
                continue
            instance = InstanceManager().get_instance(instance_id)
            if instance is None:
                continue
            scale_down = bool(ranks) and TopologyResolver.can_stop_all_problem_pods(instance, ranks)
            if scale_down:
                strategy = DpScaleDownStrategy()
                strategy_context = RecoveryContext(
                    pending_removed_ranks=tuple(sorted(ranks)),
                    source="user",
                    fallback_strategy="InstanceReconfigurationStrategy",
                    fault_keys=context.fault_keys,
                    collection_complete=True,
                    engine_fault_observed=True,
                    hardware_fault_observed=context.hardware_fault_observed,
                )
                action = "scale_down"
            elif not ranks:
                # L1 hardware noise / empty evidence must never trigger whole-instance
                # restart from user recover.
                logger.info(
                    "Instance %d: user recover skipped — no removable ranks "
                    "(faulted=%s, avoid reconfigure on empty evidence)",
                    instance_id,
                    faulted,
                )
                continue
            else:
                strategy = InstanceReconfigurationStrategy()
                strategy_context = None
                action = "reconfigure"
            try:
                self._submit_user_strategy(
                    metadata,
                    strategy,
                    strategy_context,
                )
            except RuntimeError:
                in_progress += 1
                continue
            logger.info(
                "Instance %d: user recovery action=%s ranks=%s",
                instance_id,
                action,
                sorted(ranks),
            )
            runtime = get_ft_runtime_store().get(instance_id) or {
                "instance_id": instance_id,
                "phase": FtPhase.DECIDING.value if scale_down else FtPhase.RECONFIGURING.value,
            }
            results.append({**runtime, "action": action, "removed_dp_ranks": sorted(ranks)})
        if results:
            return results
        if in_progress:
            raise RuntimeError("recovery already in progress")
        raise LookupError("no faulted instance to recover")

    def _submit_user_strategy(
        self,
        metadata: InstanceMetadata,
        strategy: Any,
        context: Any,
    ) -> None:
        evidence = self._capture_software_evidence(metadata.instance_id)
        with metadata.lock:
            if self._recovery_round_busy(metadata.instance_id, metadata):
                raise RuntimeError("recovery already in progress")
            strategy.bind_config(self.config)
            if context is not None:
                strategy.bind_context(context)
            self._prepare_recovery_task(metadata.instance_id, strategy, evidence)
            future = self.executor.submit(self._execute_strategy, strategy, metadata.instance_id)
            future.add_done_callback(self._on_strategy_finished)
            metadata.strategy = strategy
            metadata.strategy_future = future
            metadata.strategy_fault_level = FaultLevel.L2
            metadata.prev_strategy_failed = False

    def _on_strategy_finished(self, _future: concurrent.futures.Future) -> None:
        """Wake the strategy center as soon as an asynchronous strategy returns."""
        self._strategy_finished_event.set()
        with self.work_condition:
            self.work_condition.notify_all()

    def _prepare_recovery_task(self, instance_id: int, strategy: StrategyBase, evidence: SoftwareFaultEvidence) -> None:
        """Reserve a recovery identity before submitting any work to the executor."""
        strategy.recovery_id = uuid.uuid4().hex
        strategy.consumed_evidence = evidence
        previous = get_ft_runtime_store().get(instance_id) or {}
        fallback_reason = (
            previous.get("fallback_reason") or previous.get("last_error")
            if previous.get("stage") in {"failed", "fallback"}
            else None
        )
        preparation = {}
        pending_ranks = []
        if strategy.requires_recovery_preparation:
            pending_ranks = list(getattr(strategy.strategy_context, "pending_removed_ranks", ()))
            # Establish lifecycle protection before the worker can be queued.
            preparation["phase"] = FtPhase.WAITING_ENGINE_FAULT
        get_ft_runtime_store().transition(
            instance_id,
            recovery_id=strategy.recovery_id,
            action=strategy.recovery_action,
            stage="preparing",
            request_id="",
            pending_removed_ranks=pending_ranks,
            recovery_paused=bool(preparation),
            last_error=None,
            fallback_reason=fallback_reason,
            **preparation,
        )

    @staticmethod
    def _execute_strategy(
        strategy: StrategyBase,
        instance_id: int,
        *args: Any,
        operation: Callable[..., Any] | None = None,
    ) -> None:
        """Run a strategy entry point with shared failure recording and completion."""
        try:
            if strategy.recovery_action == "RELAUNCH":
                get_ft_runtime_store().transition(
                    instance_id,
                    expected_recovery_id=strategy.recovery_id or None,
                    phase=FtPhase.RELAUNCHING,
                    dead_committed=[],
                    pending_removed_ranks=[],
                    serving_dp_ranks=[],
                    dp_master_rank=None,
                    dp_master_ip=None,
                    stage="executing",
                    serving_published=False,
                )
            if operation is None:
                strategy.execute(instance_id)
            else:
                operation(instance_id, *args)
        except Exception as error:
            strategy.cleanup_failed = True
            strategy.mark_failed()
            get_ft_runtime_store().transition_if_owned(
                instance_id,
                expected_recovery_id=strategy.recovery_id,
                stage="failed",
                last_error=str(error),
                serving_published=False,
            )
            logger.exception("Unhandled recovery failure for instance %d", instance_id)
        finally:
            if strategy.invalidated:
                strategy.mark_failed()
            strategy.mark_finished()

    def _capture_software_evidence(self, instance_id: int) -> SoftwareFaultEvidence:
        with self.lock:
            metadata = self.instances.get(instance_id)
            result: SoftwareFaultEvidence = {}
            if metadata is not None:
                for key, fault in metadata.software_fault_infos.items():
                    result[(None, key)] = (metadata.software_fault_versions.get(key, 0), fault.model_copy(deep=True))
            for name, node in self.nodes.items():
                if instance_id in node.instance_ids:
                    for key, fault in node.software_fault_infos.items():
                        if fault.instance_id in (None, instance_id):
                            result[(name, key)] = (0, fault.model_copy(deep=True))
            return result

    def _consume_software_evidence(self, instance_id: int, evidence: SoftwareFaultEvidence | None = None) -> None:
        """Consume only evidence observed by the completed attempt; retain newer reports."""
        with self.lock:
            metadata = self.instances.get(instance_id)
            if metadata is not None:
                for key, fault in list(metadata.software_fault_infos.items()):
                    observed = None if evidence is None else evidence.get((None, key))
                    if evidence is None or (
                        observed is not None
                        and observed[0] == metadata.software_fault_versions.get(key, 0)
                        and software_evidence_signature(observed[1]) == software_evidence_signature(fault)
                    ):
                        del metadata.software_fault_infos[key]
                        metadata.software_fault_versions.pop(key, None)
                remaining = {f.engine_id for f in metadata.software_fault_infos.values()}
                metadata.software_dead_observed_at = {
                    rank: observed for rank, observed in metadata.software_dead_observed_at.items() if rank in remaining
                }
                metadata.software_unhealthy_observed_at = {
                    rank: observed
                    for rank, observed in metadata.software_unhealthy_observed_at.items()
                    if rank in remaining
                }
                if metadata.software_fault_infos:
                    observations = [
                        *metadata.software_dead_observed_at.values(),
                        *metadata.software_unhealthy_observed_at.values(),
                    ]
                    metadata.fault_collection_started_at = min(observations) if observations else None
                    metadata.fault_collection_deadline = (
                        time.monotonic()
                        + max(
                            0.0,
                            self.config.fault_tolerance_config.fault_collection_timeout_seconds
                            - (time.time() - min(observations)),
                        )
                        if observations
                        else None
                    )
                if not metadata.software_fault_infos:
                    metadata.fault_collection_started_at = None
                    metadata.fault_collection_deadline = None
                    metadata.hardware_correlation_deadline = None
                    metadata.hardware_fault_observed_at = None
                    metadata.software_dead_observed_at.clear()
                    metadata.software_unhealthy_observed_at.clear()
            for name, node in self.nodes.items():
                if instance_id not in node.instance_ids:
                    continue
                for key, fault in list(node.software_fault_infos.items()):
                    if fault.instance_id in (None, instance_id) and (
                        evidence is None
                        or (
                            evidence.get((name, key)) is not None
                            and software_evidence_signature(evidence[(name, key)][1])
                            == software_evidence_signature(fault)
                        )
                    ):
                        del node.software_fault_infos[key]
