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
import time
from collections.abc import Callable

from fastapi import HTTPException

from motor.common.resources import (
    HeartbeatMsg,
    Instance,
    InsStatus,
    InsConditionEvent,
    ReadOnlyInstance,
    EndpointStatus,
    EventType,
    PDRole,
)
from motor.common.etcd.etcd_client import EtcdClient
from motor.common.etcd.persistent_state import PersistentState
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.config.controller import ControllerConfig
from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient
from motor.controller.core import Observer, ObserverEvent
from motor.controller.core.event_pusher import EventPusher
from motor.common.logger import get_logger
from motor.common.alarm.instance_exception_alarm import InstanceExceptionAlarm, InstanceExceptionReason
from motor.common.alarm.coordinator_exception_alarm import CoordinatorExceptionAlarm, CoordinatorExceptionReason
from motor.common.alarm.enums import Cleared


logger = get_logger(__name__)

# Heartbeat handle result code
HEARTBEAT_HANDLER_SUCCESS = 200
HEARTBEAT_HANDLER_ERROR = 500
HEARTBEAT_HANDLER_RE_REGISTER = 503


class InstanceManager(ThreadSafeSingleton):
    """Instance Manager
    Manages all instances including states and heartbeats.
    It is a singleton class and can be accessed by InstanceManager().
    It is responsible for:
    - Adding and removing instances
    - Managing instance states
    - Managing instance heartbeats
    - Managing instance separation and recovery
    - Managing instance state transitions
    - Managing instance notifications
    """

    def __init__(self, config: ControllerConfig | None = None) -> None:
        super().__init__()
        # If the instance manager is already initialized, return.
        if hasattr(self, '_initialized'):
            return

        if config is None:
            config = ControllerConfig()

        self.instances: dict[int, Instance] = {}
        self.observers: list[Observer] = []

        # Track instances that are forcibly separated and should not
        # be reactivated by heartbeats
        self.forced_separated_instances: set[int] = set()

        self.stop_event = threading.Event()
        self.ins_lock = threading.Lock()
        self.config_lock = threading.RLock()

        # Extract required config fields
        with self.config_lock:
            self.etcd_config = config.etcd_config
            self.etcd_tls_config = config.etcd_tls_config
            self.instance_manager_check_interval = config.instance_config.instance_manager_check_interval

        # Version control for data persistence
        self._data_version = 0
        self._version_lock = threading.Lock()

        with self.config_lock:
            self.etcd_client = EtcdClient(etcd_config=self.etcd_config, tls_config=self.etcd_tls_config)

        # self.states: dict[InsStatus, Callable]: State handle function mapping
        self.states: dict[InsStatus, Callable] = {
            InsStatus.INITIAL: self._handle_initial,
            InsStatus.ACTIVE: self._handle_active,
            InsStatus.INACTIVE: self._handle_inactive,
            InsStatus.PAUSED: self._handle_paused,
            InsStatus.DELETED: self._handle_deleted,
        }

        """
        State transition rule: FROM status + event -> TO status
        """
        self.transitions: dict[tuple[InsStatus, InsConditionEvent], InsStatus] = {
            (InsStatus.INITIAL, InsConditionEvent.INSTANCE_INIT): InsStatus.INITIAL,
            (InsStatus.INITIAL, InsConditionEvent.INSTANCE_NORMAL): InsStatus.ACTIVE,
            (InsStatus.INITIAL, InsConditionEvent.INSTANCE_ABNORMAL): InsStatus.INACTIVE,
            (InsStatus.INITIAL, InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT): InsStatus.DELETED,
            (InsStatus.ACTIVE, InsConditionEvent.INSTANCE_NORMAL): InsStatus.ACTIVE,
            (InsStatus.ACTIVE, InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT): InsStatus.INACTIVE,
            (InsStatus.ACTIVE, InsConditionEvent.INSTANCE_ABNORMAL): InsStatus.INACTIVE,
            (InsStatus.INACTIVE, InsConditionEvent.INSTANCE_ABNORMAL): InsStatus.INACTIVE,
            (InsStatus.INACTIVE, InsConditionEvent.INSTANCE_NORMAL): InsStatus.ACTIVE,
            (InsStatus.INACTIVE, InsConditionEvent.INSTANCE_INIT): InsStatus.INITIAL,
            (InsStatus.INACTIVE, InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT): InsStatus.DELETED,
            (InsStatus.ACTIVE, InsConditionEvent.INSTANCE_PAUSED): InsStatus.PAUSED,
            (InsStatus.PAUSED, InsConditionEvent.INSTANCE_PAUSED): InsStatus.PAUSED,
            (InsStatus.PAUSED, InsConditionEvent.INSTANCE_RESUMED): InsStatus.ACTIVE,
            (InsStatus.PAUSED, InsConditionEvent.INSTANCE_NORMAL): InsStatus.ACTIVE,
            (InsStatus.PAUSED, InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT): InsStatus.DELETED,
            (InsStatus.PAUSED, InsConditionEvent.INSTANCE_ABNORMAL): InsStatus.INACTIVE,
        }

        self.instances_management_thread = None

        self._initialized = True
        logger.info("InstanceManager initialized.")

    def start(self) -> None:
        """Start the instance heartbeat timeout management thread"""
        # Reset stop_event if it was previously set (for singleton reuse)
        if self.stop_event.is_set():
            self.stop_event.clear()

        # Try to restore data from ETCD, if failed,
        # it will start with empty state.
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if enable_persistence and not self.restore_data():
            logger.warning("Failed to restore instance manager data from ETCD, starting with empty state")
        elif enable_persistence is False:
            # when presistence is off, we should also refresh heartbeat timestamp
            current_time = time.time()
            with self.ins_lock:
                for instance in self.instances.values():
                    self._maybe_refresh_heartbeat(instance, current_time, self._data_version, should_notify=False)

        # Create instance heartbeat timeout management thread
        self.instances_management_thread = threading.Thread(
            target=self._instances_management_loop, daemon=True, name="InstancesManagementLoop"
        )
        self.instances_management_thread.start()

        logger.info("InstanceManager started.")

    def stop(self) -> None:
        self.stop_event.set()
        # Only join thread that have been started
        if self.instances_management_thread and self.instances_management_thread.is_alive():
            self.instances_management_thread.join()
        logger.info("InstanceManager stopped.")

    def is_alive(self) -> bool:
        """Check if the instances_management threads are alive"""
        return self.instances_management_thread is not None and self.instances_management_thread.is_alive()

    def update_config(self, config: ControllerConfig) -> None:
        """Update configuration for the instance manager"""
        with self.config_lock:
            # Update config fields
            self.etcd_config = config.etcd_config
            self.etcd_tls_config = config.etcd_tls_config
            self.instance_manager_check_interval = config.instance_config.instance_manager_check_interval

            # Update ETCD client with new configuration
            self.etcd_client = EtcdClient(etcd_config=self.etcd_config, tls_config=self.etcd_tls_config)
            logger.info("InstanceManager configuration updated")

    def attach(self, observer: Observer) -> None:
        # For observer pattern
        if observer not in self.observers:
            self.observers.append(observer)

    # notify all observers with read-only instance
    def notify(self, instance: Instance, event: ObserverEvent) -> None:
        readonly_instance = ReadOnlyInstance(instance)
        for observer in self.observers:
            observer.update(readonly_instance, event)

    def persist_data(self) -> bool:
        """Persist instance manager data to ETCD with version control and checksum"""
        try:
            with self.ins_lock:
                current_time = time.time()
                next_version = self._get_next_version()

                # Prepare instance manager data - all instances in one dict
                instances_data = {}
                for ins_id, instance in self.instances.items():
                    instances_data[str(ins_id)] = instance.model_dump(mode='json')
                logger.debug("Persisting instance manager data - full data: %s", instances_data)

                # Create persistent state with version control and checksum
                persistent_state = PersistentState(
                    data=instances_data,
                    version=next_version,
                    timestamp=current_time,
                    checksum="",  # Will be calculated
                )
                persistent_state.checksum = persistent_state.calculate_checksum()
                logger.debug(
                    "Persisting instance manager data - calculated checksum: %s, version: %s, timestamp: %s",
                    persistent_state.checksum,
                    next_version,
                    current_time,
                )

                # Convert PersistentState to dict for etcd storage
                dict_data = {"state": persistent_state.model_dump()}
                logger.debug("Persistence data being saved to ETCD: %s", dict_data)

            # Release ins_lock before ETCD I/O to avoid blocking heartbeat lookups
            success = self.etcd_client.persist_data("/controller/instance_manager", dict_data)
            if success:
                logger.info("Successfully persisted %d instances with version %d", len(instances_data), next_version)
            return success

        except Exception as e:
            logger.error("Error persisting instance manager data: %s", e)
            return False

    def restore_data(self) -> bool:
        """Restore instance manager data from ETCD with version control and validation"""
        try:
            persistent_states = self.etcd_client.restore_data("/controller/instance_manager", PersistentState)
            if persistent_states is None:
                logger.info("No instance data found in ETCD, starting with empty state")
                return True

            logger.info("Restoring instance manager data from ETCD")

            persistent_state = persistent_states.get("state")
            if persistent_state is None:
                logger.warning(
                    "Expected 'state' key not found in persistent states, found keys: %s",
                    list(persistent_states.keys()),
                )
                return False
            if not isinstance(persistent_state, PersistentState):
                logger.error("Invalid persistent state format, expected PersistentState instance")
                return False

            # Validate data integrity
            if not persistent_state.is_valid():
                logger.error("Data integrity check failed for instances, cannot restore")
                return False

            # Update data version
            with self._version_lock:
                self._data_version = max(self._data_version, persistent_state.version)

            # Restore instances
            with self.ins_lock:
                self.instances.clear()
                current_time = time.time()
                valid_instances, invalid_instances = 0, 0

                # Restore all instances from data
                for ins_id_str, instance_data in persistent_state.data.items():
                    try:
                        instance = Instance(**instance_data)
                        # Maybe refresh heartbeat for restored instance
                        self._maybe_refresh_heartbeat(instance, current_time, persistent_state.version)
                        self.instances[instance.id] = instance
                        valid_instances += 1
                    except Exception as e:
                        logger.error("Error reconstructing instance %s: %s", ins_id_str, e)
                        invalid_instances += 1
                        continue

                # Notify Coordinator that Controller just restarted,
                # it should refresh all instance status.
                for observer in self.observers:
                    if isinstance(observer, EventPusher):
                        observer.push_event(EventType.SET)
                        break

                logger.info(
                    "Successfully restored %d valid instances, %d invalid instances skipped",
                    valid_instances,
                    invalid_instances,
                )
                return True
        except Exception as e:
            logger.error("Error restoring instance manager data: %s", e)
            return False

    def get_instance_num(self) -> int:
        with self.ins_lock:
            return len(self.instances)

    def get_active_instances(self) -> list[ReadOnlyInstance]:
        active_instances = []
        with self.ins_lock:
            for instance in self.instances.values():
                if instance.status == InsStatus.ACTIVE:
                    active_instances.append(ReadOnlyInstance(instance))
        return active_instances

    def get_initial_instances(self) -> list[ReadOnlyInstance]:
        initial_instances = []
        with self.ins_lock:
            for instance in self.instances.values():
                if instance.status == InsStatus.INITIAL:
                    initial_instances.append(ReadOnlyInstance(instance))
        return initial_instances

    def get_instances(self, statuses: set[InsStatus] | None = None) -> list[ReadOnlyInstance]:
        """Return instances matching any of the given statuses.

        If statuses is None, returns all non-DELETED instances.
        """
        if statuses is None:
            statuses = {InsStatus.INITIAL, InsStatus.ACTIVE, InsStatus.INACTIVE}
        result = []
        with self.ins_lock:
            for instance in self.instances.values():
                if instance.status in statuses:
                    result.append(ReadOnlyInstance(instance))
        return result

    def get_inactive_instances(self) -> list[ReadOnlyInstance]:
        inactive_instances = []
        with self.ins_lock:
            for instance in self.instances.values():
                if instance.status == InsStatus.INACTIVE:
                    inactive_instances.append(ReadOnlyInstance(instance))
        return inactive_instances

    def get_instances_by_role(self, role: PDRole | str) -> list[Instance]:
        """Return live instances matching the given PD role."""
        role_value = role.value if isinstance(role, PDRole) else role
        matched: list[Instance] = []
        with self.ins_lock:
            for instance in self.instances.values():
                if instance.role == role_value:
                    matched.append(instance)
        return matched

    def add_instance(self, ins: Instance):
        if ins is None or not isinstance(ins, Instance):
            logger.error("Invalid instance provided to add_instance.")
            return

        with self.ins_lock:
            if ins.id in self.instances:
                logger.error("Instance %s(id:%d) already exists.", ins.job_name, ins.id)
                return
            self.instances[ins.id] = ins

            # Refresh heartbeat for all endpoints with initial status
            timestamp = time.time()
            for pod_ip in ins.endpoints.keys():
                # Create initial status dict for all endpoints in this pod
                initial_status = {endpoint.id: EndpointStatus.INITIAL for endpoint in ins.endpoints[pod_ip].values()}
                if ins.update_heartbeat(pod_ip, timestamp, initial_status):
                    logger.debug(
                        "Refreshed heartbeat for pod_ip %s in instance %s(id:%d) with initial status",
                        pod_ip,
                        ins.job_name,
                        ins.id,
                    )
                else:
                    logger.warning(
                        "Failed to refresh heartbeat for pod_ip %s in instance %s(id:%d)", pod_ip, ins.job_name, ins.id
                    )

            logger.info(
                "Instance %s(id:%d) role:%s added, total %d instances",
                ins.job_name,
                ins.id,
                ins.role,
                len(self.instances),
            )
            self.notify(ins, ObserverEvent.INSTANCE_INITIAL)

    def del_instance(self, ins_id: int):
        with self.ins_lock:
            if ins_id in self.instances:
                job_name = self.instances[ins_id].job_name
                role = self.instances[ins_id].role
                self.instances.pop(ins_id)
                # Also remove from forced separated set if present
                self.forced_separated_instances.discard(ins_id)
                logger.info(
                    "Instance %s(id:%d) role:%s removed, total %d instances",
                    job_name,
                    ins_id,
                    role,
                    len(self.instances),
                )
            else:
                logger.error("Instance %d not found.", ins_id)

    def get_instance(self, ins_id: int) -> Instance | None:
        with self.ins_lock:
            if ins_id in self.instances:
                return self.instances[ins_id]
            else:
                logger.error("Instance %d not found.", ins_id)
                return None

    def get_instance_by_podip(self, pod_ip: str) -> Instance | None:
        with self.ins_lock:
            for _, instance in self.instances.items():
                if instance.is_ip_in_endpoints(pod_ip):
                    logger.info("found instance contain %s.", pod_ip)
                    return instance

            logger.error("Instance %s not found.", pod_ip)
            return None

    def separate_instance(self, instance_id: int) -> None:
        """
        Separate a specific instance by its ID, marking it as INACTIVE and notifying observers.
        Only notifies when instance status actually changes from non-INACTIVE to INACTIVE.
        """
        try:
            instance = self.get_instance(instance_id)
            if instance is not None:
                # Always add to forced separated set to prevent later heartbeats from
                # bringing it back to ACTIVE.
                with self.ins_lock:
                    self.forced_separated_instances.add(instance.id)

                # Only transition ACTIVE -> INACTIVE and notify to avoid status flapping
                # (e.g., INITIAL -> INACTIVE -> INITIAL) and noisy logs.
                if instance.status == InsStatus.ACTIVE:
                    old_status = instance.status
                    instance.update_instance_status(InsStatus.INACTIVE)
                    self.notify(instance, ObserverEvent.INSTANCE_SEPERATED)

                    # Trigger persistence on state change
                    with self.config_lock:
                        enable_persistence = self.etcd_config.enable_etcd_persistence
                    if enable_persistence:
                        logger.info(
                            "Instance %d state changed from %s to %s via separate_instance, triggering persistence",
                            instance.id,
                            old_status,
                            instance.status,
                        )
                        if not self.persist_data():
                            logger.error(
                                "Failed to persist instance %d state change from %s to %s via separate_instance",
                                instance.id,
                                old_status,
                                instance.status,
                            )
                logger.info(
                    "Successfully separated instance %s (id:%d) in state %s",
                    instance.job_name,
                    instance.id,
                    instance.status,
                )
            else:
                logger.warning("No instance found for instance ID %d", instance_id)
        except Exception as e:
            logger.error("Error separating instance %d: %s", instance_id, e)

    def is_instance_separated(self, instance_id: int) -> bool:
        """Check if instance is in forced separated state."""
        try:
            instance = self.get_instance(instance_id)
            if instance is not None:
                with self.ins_lock:
                    return instance.id in self.forced_separated_instances
            return False
        except Exception as e:
            logger.error("Error checking separation status for instance %d: %s", instance_id, e)
            return False

    def recover_instance(self, instance_id: int) -> None:
        """
        Recover a specific instance by its ID, removing it from forced separation list.
        Instance will naturally transition back to ACTIVE state via heartbeat if healthy.
        """
        try:
            instance = self.get_instance(instance_id)
            if instance is None:
                logger.warning("No instance found for instance ID %d", instance_id)
                return

            # Check if a newer instance with the same job_name already exists (stale instance)
            newer_instance = self._find_newer_instance_by_job_name(instance.job_name, instance.id)
            if newer_instance is not None:
                logger.info(
                    "Instance %s (id:%d) is stale — newer instance %s (id:%d) already exists, "
                    "removing from forced separation without recovery",
                    instance.job_name,
                    instance.id,
                    newer_instance.job_name,
                    newer_instance.id,
                )
                with self.ins_lock:
                    self.forced_separated_instances.discard(instance.id)
                return

            if instance.id in self.forced_separated_instances:
                # Remove from forced separated set to allow natural heartbeat recovery
                with self.ins_lock:
                    self.forced_separated_instances.discard(instance.id)
                logger.info("Successfully recovered instance %s (id:%d)", instance.job_name, instance.id)
            else:
                logger.warning(
                    "Instance %s (id:%d) is not in forced separated list, no need to recover",
                    instance.job_name,
                    instance.id,
                )
        except Exception as e:
            logger.error("Error recovering instance %d: %s", instance_id, e)

    def _find_newer_instance_by_job_name(self, job_name: str, current_id: int) -> Instance | None:
        """Find an instance with the same job_name but a higher instance_id."""
        with self.ins_lock:
            for instance in self.instances.values():
                if instance.job_name == job_name and instance.id > current_id:
                    return instance
        return None

    def has_instance_by_job_name(self, job_name: str) -> bool:
        """Check if instance exists by job name"""
        with self.ins_lock:
            for _, instance in self.instances.items():
                if instance.job_name == job_name:
                    return True
            return False

    def has_active_instance_by_job_name(self, job_name: str) -> bool:
        """Check if active instance exists by job name"""
        with self.ins_lock:
            for _, instance in self.instances.items():
                if instance.job_name == job_name and instance.status == InsStatus.ACTIVE:
                    return True
            return False

    def handle_heartbeat(self, heartbeat_msg: HeartbeatMsg) -> tuple[bool, str]:
        """
        Handle heartbeat and update instance status
        Returns:
            tuple[bool, str]: Whether handle is successful and the handle result code
        """
        if heartbeat_msg is None or not isinstance(heartbeat_msg, HeartbeatMsg):
            logger.error("Invalid heartbeat message.")
            return False, HEARTBEAT_HANDLER_ERROR

        ins_id = heartbeat_msg.ins_id
        pod_ip = heartbeat_msg.ip
        timestamp = time.time()

        # Retrieve instances from instances and update their status and heartbeat
        with self.ins_lock:
            instance = self.instances.get(ins_id, None)

        if instance is None:
            logger.error("Instance %d not exists, need to re-register.", ins_id)
            raise HTTPException(HEARTBEAT_HANDLER_RE_REGISTER)

        if instance.update_heartbeat(pod_ip, timestamp, heartbeat_msg.status):
            logger.debug("Heartbeat received successfully  for instance %d from IP %s.", ins_id, pod_ip)
        else:
            logger.error("Failed to update heartbeat for instance %d.", ins_id)
            raise HTTPException(HEARTBEAT_HANDLER_ERROR)

        if self._handle_state_transition(instance):
            return True, HEARTBEAT_HANDLER_SUCCESS
        else:
            logger.error("Failed to handle state transition for instance %d.", ins_id)
            raise HTTPException(HEARTBEAT_HANDLER_ERROR)

    # State transition callback function

    def _instances_management_loop(self) -> None:
        """Instance management loop"""
        while not self.stop_event.is_set():
            with self.ins_lock:
                # use copy to avoid modifying the dictionary while iterating
                cur_instances = list(self.instances.values())

            for instance in cur_instances:
                if instance.status == InsStatus.DELETED:
                    continue
                if instance.is_all_endpoints_alive():
                    continue
                # Use _handle_state_transition with heartbeat timeout event to ensure persistence is triggered
                if not self._handle_state_transition(instance, InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT):
                    logger.error("Failed to handle state transition for instance %d on heartbeat timeout", instance.id)

            with self.config_lock:
                check_interval = self.instance_manager_check_interval
            time.sleep(check_interval)

    def _handle_initial(self, from_state: InsStatus, condition_event: InsConditionEvent, instance: Instance) -> None:
        if from_state == InsStatus.INITIAL:
            return

        # Sometimes an instance may briefly enter an abnormal state
        # during initialization, then revert to the initial state.
        if from_state == InsStatus.INACTIVE and condition_event == InsConditionEvent.INSTANCE_INIT:
            instance.update_instance_status(InsStatus.INITIAL)
        return

    def _handle_active(self, from_state: InsStatus, condition_event: InsConditionEvent, instance: Instance) -> None:
        if from_state == InsStatus.ACTIVE:
            return
        if condition_event in (InsConditionEvent.INSTANCE_NORMAL, InsConditionEvent.INSTANCE_RESUMED):
            instance.update_instance_status(InsStatus.ACTIVE)
            if condition_event == InsConditionEvent.INSTANCE_RESUMED:
                self.notify(instance, ObserverEvent.INSTANCE_RESUMED)
            else:
                self.notify(instance, ObserverEvent.INSTANCE_READY)
            self._report_inst_alarm(instance, True)
            self._report_coordinator_alarm(instance, True)
        return

    def _handle_inactive(self, from_state: InsStatus, condition_event: InsConditionEvent, instance: Instance) -> None:
        if from_state == InsStatus.INACTIVE:
            return

        if condition_event == InsConditionEvent.INSTANCE_ABNORMAL:
            instance.update_instance_status(InsStatus.INACTIVE)
            self.notify(instance, ObserverEvent.INSTANCE_SEPERATED)
            return

        if condition_event == InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT:
            # When heartbeat times out, actively check the instance status to avoid
            # false positives caused by the controller's own service being unavailable,
            # which prevents node_manager from reporting heartbeats.

            # [This scenario occurs when active-standby mode is enabled, and after the node
            # where the originally primary pod is located is forcibly rebooted, kubelet cannot
            # properly report the probe status (such as readiness) of pods on this node.
            # This causes the active-standby traffic switching logic that relies on kubeproxy to fail.
            # After failure, node_manager easily reports heartbeats to the wrong pod, leading to
            # heartbeat timeout and instance isolation. Therefore, we need the controller to
            # directly query once using ip+port when heartbeat times out to avoid such situations.]
            if self._check_node_managers_status(instance):
                instance.update_instance_status(InsStatus.INACTIVE)
                self.notify(instance, ObserverEvent.INSTANCE_SEPERATED)
                self._report_inst_alarm(instance)
                self._report_coordinator_alarm(instance)
            else:
                # If node managers are all normal, do not set to INACTIVE
                # and we need to refresh the heartbeat to avoid immediate timeout
                for endpoints in instance.endpoints.values():
                    for endpoint in endpoints.values():
                        endpoint.hb_timestamp = time.time()
        return

    def _handle_paused(self, from_state: InsStatus, condition_event: InsConditionEvent, instance: Instance) -> None:
        if condition_event == InsConditionEvent.INSTANCE_PAUSED:
            if from_state != InsStatus.PAUSED:
                instance.update_instance_status(InsStatus.PAUSED)
                self.notify(instance, ObserverEvent.INSTANCE_PAUSED)
                self._report_inst_alarm(instance)

    def _report_inst_alarm(self, instance: Instance, is_cleared: bool = False) -> None:
        from motor.controller.observability.observability import Observability

        alarm = InstanceExceptionAlarm(
            instance_id=instance.job_name,
            reason_id=InstanceExceptionReason.INSTANCE_EXCEPTION,
            is_cleared=Cleared.YES if is_cleared else Cleared.NO,
        )
        Observability().add_alarm(alarm)

    def _report_coordinator_alarm(self, instance: Instance, is_cleared: bool = False) -> None:
        from motor.controller.observability.observability import Observability

        active_instances = self.get_active_instances()

        role_to_partner = {"prefill": "decode", "decode": "prefill"}
        role = role_to_partner.get(instance.role) if is_cleared else instance.role
        if role is None:
            return

        has_role = any(inst.role == role for inst in active_instances)

        if has_role != is_cleared:
            return

        alarm_msg = CoordinatorExceptionAlarm(
            reason_id=CoordinatorExceptionReason.INSTANCE_MISSING, is_cleared=is_cleared
        )
        Observability().add_alarm(alarm_msg)

    def _check_node_managers_status(self, instance: Instance) -> bool:
        """
        Check status of all node managers in the instance.
        Returns True if any node manager has abnormal endpoints or if check fails,
        meaning the instance should be set to INACTIVE.
        Returns False if all node managers report normal status.
        """
        node_managers = instance.get_node_managers()
        if not node_managers:
            logger.warning(
                "No node managers found for instance %s(id:%d), setting to INACTIVE", instance.job_name, instance.id
            )
            return True

        for node_mgr in node_managers:
            try:
                response = NodeManagerApiClient.query_status(node_mgr)
                if isinstance(response, dict) and "status" in response:
                    is_normal = response.get("status", False)
                    if not is_normal:
                        logger.warning(
                            "Node manager %s:%s reports abnormal endpoints for instance %s(id:%d)",
                            node_mgr.pod_ip,
                            node_mgr.port,
                            instance.job_name,
                            instance.id,
                        )
                        return True
                else:
                    logger.warning(
                        "Invalid response from node manager %s:%s for instance %s(id:%d): %s",
                        node_mgr.pod_ip,
                        node_mgr.port,
                        instance.job_name,
                        instance.id,
                        response,
                    )
                    return True
            except Exception as e:
                logger.warning(
                    "Failed to check node manager %s:%s status for instance %s(id:%d): %s",
                    node_mgr.pod_ip,
                    node_mgr.port,
                    instance.job_name,
                    instance.id,
                    e,
                )
                return True

        logger.info("All node managers report normal status for instance %s(id:%d)", instance.job_name, instance.id)
        return False

    def _handle_deleted(self, from_state: InsStatus, condition_event: InsConditionEvent, instance: Instance) -> None:
        if from_state == InsStatus.DELETED:
            return
        if condition_event in (
            InsConditionEvent.INSTANCE_HEARTBEAT_TIMEOUT,
            InsConditionEvent.INSTANCE_ABNORMAL,
        ):
            instance.update_instance_status(InsStatus.DELETED)
            self.notify(instance, ObserverEvent.INSTANCE_REMOVED)
            self.del_instance(instance.id)
        return

    def _handle_state_transition(self, instance: Instance, event_override: InsConditionEvent | None = None) -> bool:
        """
        Handle state transition based on current state and condition event
        Args:
            instance: Instance to handle state transition for
            event_override: Optional event to use instead of auto-detection
        Returns:
            bool: Whether handle state transition is successful
        """
        from_state = instance.status

        # Use override event if provided, otherwise detect event based on instance status
        if event_override is not None:
            event = event_override
            to_state = self.transitions.get((from_state, event), None)
        elif instance.is_all_endpoints_paused():
            event = InsConditionEvent.INSTANCE_PAUSED
            to_state = self.transitions.get((from_state, event), None)
        elif instance.is_all_endpoints_ready():
            event = InsConditionEvent.INSTANCE_NORMAL
            to_state = self.transitions.get((from_state, event), None)
        elif instance.is_have_one_endpoint_abnormal():
            event = InsConditionEvent.INSTANCE_ABNORMAL
            to_state = self.transitions.get((from_state, event), None)
        else:
            event = InsConditionEvent.INSTANCE_INIT
            to_state = self.transitions.get((from_state, event), None)

        if to_state is None:
            logger.error(
                "No valid state transition for instance %d from %s on event %s.", instance.id, from_state, event
            )
            return False

        # Check if this instance is forcibly separated and prevent reactivation to ACTIVE
        if instance.id in self.forced_separated_instances and to_state == InsStatus.ACTIVE:
            logger.debug(
                "Instance %d (%s) is forcibly separated, preventing reactivation to ACTIVE state",
                instance.id,
                instance.job_name,
            )
            return True  # Return success but skip state transition

        state_handler = self.states.get(to_state, None)
        if state_handler:
            state_handler(from_state, event, instance)

            # Check actual state change after handler execution (handler may not update state)
            with self.config_lock:
                enable_persistence = self.etcd_config.enable_etcd_persistence
            if from_state != instance.status and enable_persistence:
                logger.info(
                    "Instance %d state changed from %s to %s, triggering persistence",
                    instance.id,
                    from_state,
                    instance.status,
                )
                if not self.persist_data():
                    logger.error(
                        "Failed to persist instance %d state change from %s to %s",
                        instance.id,
                        from_state,
                        instance.status,
                    )

            # Remove from forced separated set if transitioning to DELETED
            if to_state == InsStatus.DELETED and instance.id in self.forced_separated_instances:
                with self.ins_lock:
                    self.forced_separated_instances.discard(instance.id)
                logger.info(
                    "Instance %d (%s) transitioned to DELETED, removing from forced separated set",
                    instance.id,
                    instance.job_name,
                )

        return True

    def _maybe_refresh_heartbeat(
        self, instance: Instance, current_time: float, version: int, should_notify: bool = True
    ) -> None:
        """Handle logic for restored instance, including heartbeat refresh and notification"""
        # Refresh heartbeat timestamp for ACTIVE instances to avoid immediate timeout
        if instance.status == InsStatus.ACTIVE:
            # Update heartbeat timestamp for all endpoints
            try:
                for endpoints in instance.endpoints.values():
                    for endpoint in endpoints.values():
                        endpoint.hb_timestamp = current_time
                logger.debug(
                    "Refreshed heartbeat timestamp for instance %d (%s) to %f",
                    instance.id,
                    instance.job_name,
                    current_time,
                )
            except Exception as e:
                logger.error("Error refreshing heartbeat for instance %d: %s", instance.id, e)

            if should_notify:
                self.notify(instance, ObserverEvent.INSTANCE_READY)
            logger.info(
                "Restored ACTIVE instance %d (%s) with refreshed heartbeat (v%d)",
                instance.id,
                instance.job_name,
                version,
            )
        else:
            status_str = instance.status.value if hasattr(instance.status, "value") else str(instance.status)
            logger.info(
                "Restored instance %d (%s) with status %s (v%d)", instance.id, instance.job_name, status_str, version
            )

    def _get_next_version(self) -> int:
        """Get next data version for persistence"""
        with self._version_lock:
            self._data_version += 1
            return self._data_version
