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
import threading
from typing import Any
from collections.abc import Callable
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

from motor.common.logger import get_logger
from motor.controller.fault_tolerance.fault_types import NodeStatus, FaultInfo
from motor.controller.fault_tolerance.k8s.configmap_parser import (
    process_device_info,
    process_switch_info,
    process_manually_separate_npu,
    is_configmap_valid,
)


logger = get_logger(__name__)


class ResourceMonitor:
    """
    Unified resource monitor for Node status and ConfigMap changes monitoring.

    This monitor combines Node and ConfigMap changes monitoring into a single interface,
    providing processed fault information and server status updates to fault manager.
    """

    # Exponential backoff parameters for watch reconnections.
    # On each consecutive failure the delay doubles until it hits _BACKOFF_MAX.
    # A successful event delivery resets the counter to zero.
    _BACKOFF_INITIAL: float = 1.0  # seconds — first retry
    _BACKOFF_MAX: float = 60.0  # seconds — ceiling (2 × default retry_interval)
    _BACKOFF_MULTIPLIER: float = 2.0
    _BACKOFF_410: float = 2.0  # seconds — 410 just needs a fresh LIST, short wait

    def __init__(
        self,
        node_name: str,
        namespace: str,
        configmap_name_prefix: str,
        node_change_handler: Callable[[NodeStatus, str], None] | None = None,
        configmap_change_handler: Callable[[list[FaultInfo], str], None] | None = None,
        retry_interval: int = 30,
    ):
        """Initialize Resource monitor for a specific node

        Args:
            node_name: Kubernetes node name to monitor
            namespace: Namespace for ConfigMap monitoring
            configmap_name_prefix: Prefix for ConfigMap name, will be combined with node_name
            node_change_handler: Handler for node status changes, NodeStatus, node_name
            configmap_change_handler: Handler for processed fault info updates, list[FaultInfo]
            retry_interval: Retry interval in seconds for failed monitoring
        """
        self.node_name = node_name
        self.namespace = namespace
        self.configmap_name_prefix = configmap_name_prefix
        self.node_change_handler = node_change_handler
        self.configmap_change_handler = configmap_change_handler
        self.retry_interval = retry_interval

        self.stop_event = threading.Event()
        self.monitor_threads: list[threading.Thread] = []

        # Cache for last processed fault information
        self.last_fault_infos: list[FaultInfo] | None = None

        # Cache for last processed node status
        self.last_node_status: NodeStatus | None = None

        # Load Kubernetes configuration
        try:
            # Try to load in-cluster config (for Pod environment)
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config for Resource monitoring")
        except Exception as e:
            try:
                config.load_kube_config()
                logger.info("Loaded kubeconfig for Resource monitoring")
            except Exception as e2:
                logger.error("Failed to load Kubernetes config: %s, %s", e, e2)
                return

        self.v1 = client.CoreV1Api()

    @staticmethod
    def _get_node_ready_status(node) -> NodeStatus:
        """Extract the ready status from a Node object"""
        ready_condition = None
        if node.status.conditions:
            ready_condition = next(
                (condition for condition in node.status.conditions if condition.type == "Ready"), None
            )

        # Determine status based on Ready condition
        if ready_condition and ready_condition.status == "True":
            return NodeStatus.READY
        else:
            return NodeStatus.NOT_READY

    def start_monitoring(self) -> None:
        """Start monitoring both Node and ConfigMap for this node"""
        if not hasattr(self, "v1") or self.v1 is None:
            logger.error("Resource monitoring not available for node %s", self.node_name)
            return

        node_name = self.node_name
        logger.info("Starting Resource monitor for node %s", node_name)

        # Start Node monitoring thread
        node_thread = threading.Thread(target=self._monitor_node, daemon=True, name=f"ResourceMonitor-Node-{node_name}")
        node_thread.start()
        self.monitor_threads.append(node_thread)
        logger.info("Started Node monitoring for %s", node_name)

        # Start ConfigMap monitoring thread
        configmap_name = f"{self.configmap_name_prefix}{node_name}"
        cm_thread = threading.Thread(
            target=self._monitor_configmap,
            args=(configmap_name,),
            daemon=True,
            name=f"ResourceMonitor-CM-{self.namespace}-{configmap_name}",
        )
        cm_thread.start()
        self.monitor_threads.append(cm_thread)
        logger.info("Started ConfigMap monitoring for %s/%s", self.namespace, configmap_name)

        logger.info("Resource monitor started for node %s", node_name)

    def stop_monitoring(self) -> None:
        """Stop monitoring for this node"""
        logger.info("Stopping Resource monitor for node %s", self.node_name)
        self.stop_event.set()

        # Wait for all monitoring threads to finish
        for thread in self.monitor_threads:
            if thread.is_alive():
                thread.join(timeout=5.0)
                if thread.is_alive():
                    logger.warning("Thread %s did not stop within timeout", thread.name)

        self.monitor_threads.clear()
        logger.info("Resource monitor stopped for node %s", self.node_name)

    def is_alive(self) -> bool:
        """Check if the Resource monitor is alive and functioning"""
        # Check if Kubernetes client is available
        if not hasattr(self, "v1") or self.v1 is None:
            return False

        # Check if stop event is set (monitor is stopping/stopped)
        if self.stop_event.is_set():
            return False

        # Check if we have monitoring threads and they are alive
        if not self.monitor_threads:
            return False

        # Check if at least one monitoring thread is alive
        return any(thread.is_alive() for thread in self.monitor_threads)

    def _monitor_node(self) -> None:
        """Monitor Node status changes for this node"""
        node_name = self.node_name
        resource_version = None
        consecutive_failures = 0

        while not self.stop_event.is_set():
            try:
                # If we lost the resource_version (410 or first connect), get it from a fresh LIST
                if resource_version is None:
                    try:
                        nodes = self.v1.list_node(field_selector=f"metadata.name={node_name}")
                        if nodes.items:
                            resource_version = nodes.metadata.resource_version
                            logger.debug("Starting Node watch from resource_version: %s", resource_version)
                            # Process current state immediately: the watch only delivers
                            # events AFTER this resource_version, so we'd miss the
                            # initial status without an explicit initial handling.
                            self._handle_node_change("ADDED", nodes.items[0])
                    except ApiException as e:
                        logger.error("Error listing Node %s: %s", node_name, str(e).replace('\r\n', ' '))
                        if not self.stop_event.is_set():
                            delay = self._compute_backoff(consecutive_failures)
                            logger.info("Retrying Node monitoring in %.0f seconds...", delay)
                            time.sleep(delay)
                            consecutive_failures += 1
                        continue

                w = watch.Watch()

                # Monitor Node changes
                stream_kwargs = {"field_selector": f"metadata.name={node_name}"}
                if resource_version:
                    stream_kwargs["resource_version"] = resource_version

                for event in w.stream(self.v1.list_node, **stream_kwargs):
                    if self.stop_event.is_set():
                        w.stop()
                        break

                    event_type = event["type"]
                    node = event["object"]

                    # Update resource_version from each event
                    if node.metadata and node.metadata.resource_version:
                        resource_version = node.metadata.resource_version

                    # Reset backoff on successful event delivery
                    if consecutive_failures > 0:
                        logger.debug("Node watch recovered after %d failures", consecutive_failures)
                        consecutive_failures = 0

                    self._handle_node_change(event_type, node)

            except ApiException as e:
                error_msg = str(e).replace('\r\n', ' ')
                logger.error("Error monitoring Node %s: %s", node_name, error_msg)
                if e.status == 410:
                    logger.info("Resource version expired for Node, will re-list")
                    resource_version = None
                if not self.stop_event.is_set():
                    delay = self._compute_backoff(consecutive_failures, is_410=(e.status == 410))
                    logger.info("Retrying Node monitoring in %.0f seconds...", delay)
                    time.sleep(delay)
                    consecutive_failures += 1

            except Exception as e:
                logger.error("Error monitoring Node %s: %s", node_name, str(e).replace('\r\n', ' '))
                if not self.stop_event.is_set():
                    delay = self._compute_backoff(consecutive_failures)
                    logger.info("Retrying Node monitoring in %.0f seconds...", delay)
                    time.sleep(delay)
                    consecutive_failures += 1

    def _monitor_configmap(self, configmap_name: str) -> None:
        """Monitor ConfigMap changes for this host"""
        resource_version = None
        consecutive_failures = 0

        while not self.stop_event.is_set():
            try:
                # If we lost the resource_version (410 or first connect), get it from a fresh LIST
                if resource_version is None:
                    try:
                        cm_list = self.v1.list_namespaced_config_map(
                            namespace=self.namespace,
                            field_selector=f"metadata.name={configmap_name}",
                        )
                        if cm_list.items:
                            resource_version = cm_list.metadata.resource_version
                            logger.debug("Starting ConfigMap watch from resource_version: %s", resource_version)
                            # Process current state immediately: the watch only delivers
                            # events AFTER this resource_version, so we'd miss the
                            # initial state without an explicit initial handling.
                            self._handle_configmap_change("ADDED", cm_list.items[0], configmap_name)
                    except ApiException as e:
                        logger.error(
                            "Error listing ConfigMap %s/%s: %s",
                            self.namespace,
                            configmap_name,
                            str(e).replace('\r\n', ' '),
                        )
                        if not self.stop_event.is_set():
                            delay = self._compute_backoff(consecutive_failures)
                            logger.info("Retrying ConfigMap monitoring in %.0f seconds...", delay)
                            time.sleep(delay)
                            consecutive_failures += 1
                        continue

                w = watch.Watch()

                # Monitor ConfigMap changes
                stream_kwargs: dict[str, Any] = {
                    "namespace": self.namespace,
                    "field_selector": f"metadata.name={configmap_name}",
                }
                if resource_version:
                    stream_kwargs["resource_version"] = resource_version

                for event in w.stream(self.v1.list_namespaced_config_map, **stream_kwargs):
                    if self.stop_event.is_set():
                        w.stop()
                        break

                    event_type = event["type"]
                    configmap = event["object"]

                    # Update resource_version from each event
                    if configmap.metadata and configmap.metadata.resource_version:
                        resource_version = configmap.metadata.resource_version

                    # Reset backoff on successful event delivery
                    if consecutive_failures > 0:
                        logger.debug("ConfigMap watch recovered after %d failures", consecutive_failures)
                        consecutive_failures = 0

                    self._handle_configmap_change(event_type, configmap, configmap_name)

            except ApiException as e:
                error_msg = str(e).replace('\r\n', ' ')
                logger.error("Error monitoring ConfigMap %s/%s: %s", self.namespace, configmap_name, error_msg)
                if e.status == 410:
                    logger.info("Resource version expired for ConfigMap, will re-list to get latest version")
                    resource_version = None
                if not self.stop_event.is_set():
                    delay = self._compute_backoff(consecutive_failures, is_410=(e.status == 410))
                    logger.info("Retrying ConfigMap monitoring in %.0f seconds...", delay)
                    time.sleep(delay)
                    consecutive_failures += 1

            except Exception as e:
                logger.error(
                    "Error monitoring ConfigMap %s/%s: %s",
                    self.namespace,
                    configmap_name,
                    str(e).replace('\r\n', ' '),
                )
                if not self.stop_event.is_set():
                    delay = self._compute_backoff(consecutive_failures)
                    logger.info("Retrying ConfigMap monitoring in %.0f seconds...", delay)
                    time.sleep(delay)
                    consecutive_failures += 1

    def _compute_backoff(self, consecutive_failures: int, is_410: bool = False) -> float:
        """Compute exponential backoff delay for watch reconnection.

        - 410 (resource version expired): uses a short fixed delay since all we
          need is a fresh LIST call; the counter is still passed so repeated 410s
          escalate, but with a lower ceiling.
        - Other errors: exponential backoff starting from ``_BACKOFF_INITIAL``,
          doubling each consecutive failure up to ``_BACKOFF_MAX``.
        """
        if is_410:
            return min(self._BACKOFF_410 * (self._BACKOFF_MULTIPLIER**consecutive_failures), self._BACKOFF_MAX / 2)
        return min(self._BACKOFF_INITIAL * (self._BACKOFF_MULTIPLIER**consecutive_failures), self._BACKOFF_MAX)

    def _handle_node_change(self, event_type: str, node) -> None:
        """Handle Node change events and call the node change handler
        Args:
            event_type: Event type ('ADDED', 'MODIFIED', 'DELETED')
            node: Node object
        """
        logger.debug("Node %s: %s", event_type, node.metadata.name)

        # Determine node ready status
        node_status = self._get_node_ready_status(node)

        # Call the node change handler
        node_name = self.node_name
        if self.node_change_handler:
            try:
                if event_type in ["ADDED", "MODIFIED"]:
                    # Check if node status has actually changed (ignore duplicate status updates)
                    if node_status != self.last_node_status:
                        logger.info("Node %s status changed to: %s", node_name, node_status)
                        self.last_node_status = node_status  # Update cache
                        self.node_change_handler(node_status, node_name)
                    else:
                        logger.debug("Node status unchanged, skipping duplicate processing")
                elif event_type == "DELETED":
                    logger.warning("Node %s was deleted", node.metadata.name)
                    # Node deletion is always processed (reset cache)
                    self.last_node_status = None
                    # Node deleted means not ready
                    self.node_change_handler(NodeStatus.NOT_READY, node_name)

            except Exception as e:
                logger.error("Error in node status change handler for %s: %s", node_name, e)
        else:
            logger.warning("No node change handler configured for node %s", self.node_name)

    def _handle_configmap_change(self, event_type: str, configmap, configmap_name: str) -> None:
        """
        Handle ConfigMap change events, process the data, and call the configmap change handler

        Args:
            event_type: Event type ('ADDED', 'MODIFIED', 'DELETED')
            configmap: ConfigMap object
        """
        cm_metadata = configmap.metadata
        logger.debug("ConfigMap %s: %s in %s", event_type, cm_metadata.name, cm_metadata.namespace)

        # Call the configmap change handler with processed data
        if self.configmap_change_handler:
            try:
                if event_type in ["ADDED", "MODIFIED"]:
                    data = configmap.data or {}
                    logger.debug("ConfigMap %s changed! changed data keys: %s", cm_metadata.name, list(data.keys()))

                    # Process ConfigMap data to check for changes and handle
                    fault_infos = self._process_configmap_data(data)

                    # Check if fault information has actually changed (ignore time-only updates)
                    if self._has_fault_info_changed(fault_infos):
                        logger.info("Fault information changed, processing ConfigMap update")
                        self.last_fault_infos = fault_infos.copy()  # Update cache
                        self.configmap_change_handler(fault_infos, self.node_name)
                    else:
                        logger.debug("Fault information unchanged, skipping duplicate processing")
                elif event_type == "DELETED":
                    logger.warning("ConfigMap %s was deleted", cm_metadata.name)
                    # ConfigMap deleted means no fault information available
                    self.last_fault_infos = None  # Reset cache on deletion
                    self.configmap_change_handler([], self.node_name)

            except Exception as e:
                logger.error("Error in configmap change handler for %s: %s", cm_metadata.name, e)
        else:
            logger.warning("No configmap change handler configured for node %s", self.node_name)

    def _has_fault_info_changed(self, new_fault_infos: list[FaultInfo]) -> bool:
        """
        Check if the fault information has actually changed compared to the last processed data.

        Comparison is done on the deduplicated dict (keyed by fault_code), matching the
        storage model used by FaultManager.  This prevents spurious "changed" triggers when
        only the set of affected NPU names shifts but the unique fault codes stay the same.
        """
        if self.last_fault_infos is None:
            return True  # First time processing

        def _build_dedup_dict(infos: list[FaultInfo]) -> dict[int, FaultInfo]:
            return {int(f.fault_code): f for f in infos}

        new_dict = _build_dedup_dict(new_fault_infos)
        last_dict = _build_dedup_dict(self.last_fault_infos)

        # Different sets of fault codes → changed
        if new_dict.keys() != last_dict.keys():
            return True

        # Same fault codes: compare level and type (the fields that affect behavior)
        for code in new_dict:
            new_f = new_dict[code]
            old_f = last_dict[code]
            if new_f.fault_level != old_f.fault_level or new_f.fault_type != old_f.fault_type:
                return True

        return False

    def _process_configmap_data(self, config_data: dict[str, Any]) -> list[FaultInfo]:
        """Process ConfigMap configuration data and extract fault information"""
        fault_infos = []

        try:
            # Handle configuration format with DeviceInfoCfg, SwitchInfoCfg, ManuallySeparateNPU
            if is_configmap_valid(config_data):
                # Process DeviceInfoCfg
                device_info_cfg = config_data.get("DeviceInfoCfg", "")
                if device_info_cfg:
                    device_fault_infos = process_device_info(device_info_cfg)
                    fault_infos.extend(device_fault_infos)
                    logger.debug("Processed %d device fault infos from DeviceInfoCfg", len(device_fault_infos))

                # Process SwitchInfoCfg
                switch_info_cfg = config_data.get("SwitchInfoCfg", "")
                if switch_info_cfg:
                    switch_fault_infos = process_switch_info(switch_info_cfg)
                    fault_infos.extend(switch_fault_infos)
                    logger.debug("Processed %d switch fault infos from SwitchInfoCfg", len(switch_fault_infos))

                # Process ManuallySeparateNPU (for future use, not added to fault_infos)
                manually_separate_npu = config_data.get("ManuallySeparateNPU", "")
                if manually_separate_npu:
                    separated_ranks = process_manually_separate_npu(manually_separate_npu)
                    logger.info("Processed manually separated NPU ranks: %s", separated_ranks)
                    # Note: Manually separated NPUs are not treated as faults here
                    # They may be handled separately by the fault manager
            else:
                logger.debug("ConfigMap data is not in expected configuration format")

        except Exception as e:
            logger.error("Error processing ConfigMap data for node %s: %s", self.node_name, e)

        logger.debug("Total processed %d fault infos for node %s", len(fault_infos), self.node_name)
        return fault_infos
