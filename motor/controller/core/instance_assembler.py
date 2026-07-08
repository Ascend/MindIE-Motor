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
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field, model_validator

from motor.common.resources import (
    InsStatus,
    RegisterMsg,
    StartCmdMsg,
    ReregisterMsg,
    Instance,
    Endpoint,
    DeviceInfo,
    Ranktable,
    PDRole,
)
from motor.common.etcd.etcd_client import EtcdClient
from motor.common.etcd.persistent_state import PersistentState
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.utils.env import Env
from motor.config.controller import ControllerConfig
from motor.config.resolver import BaseConfigResolver
from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient
from motor.controller.core import InstanceManager
from motor.common.logger import get_logger

logger = get_logger(__name__)


class RegisterStatus(str, Enum):
    NOT_REGISTERED = "NOT_REGISTERED"
    ASSEMBLING = "ASSEMBLING"
    ASSEMBLED = "ASSEMBLED"


class AssembleInstanceMetadata(BaseModel):
    """
    Metadata for instance assembly process.
    """

    instance: Instance = Field(..., description="Instance object")
    register_status: RegisterStatus = Field(default=RegisterStatus.NOT_REGISTERED, description="Registration status")
    start_command_send_times: int = Field(default=0, description="Number of times start command was sent")
    register_timestamp: float = Field(default=0.0, description="Registration timestamp")
    is_reregister: bool = Field(default=False, description="Whether this is a re-registration")
    ranktable: Ranktable | None = Field(
        default=None, description="Instance level ranktable, only use in A2, A3. A5 will be None"
    )
    nnodes: int = Field(default=1, description="Expected PCP cross-node count")
    snapshot_dp_master_ip: str | None = Field(
        default=None,
        description="DP master node IP reported by is_master during snapshot restore registration",
    )

    # Non-serializable field (excluded from serialization)
    lock: Any = Field(default=None, exclude=True)

    @model_validator(mode='after')
    def init_lock(self):
        """Initialize lock if not provided"""
        if self.lock is None:
            self.lock = threading.Lock()
        return self


class InstanceAssembler(ThreadSafeSingleton):
    def __init__(self, config: ControllerConfig | None = None) -> None:
        super().__init__()
        # If the instance assembler is already initialized, return.
        if hasattr(self, '_initialized'):
            return

        # Use default config if not provided (for backward compatibility)
        if config is None:
            config = ControllerConfig()

        self.ins_id_cnt = 1
        self.instances: dict[str, AssembleInstanceMetadata] = {}

        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.config_lock = threading.RLock()
        self.work_condition = threading.Condition()

        # Extract required config fields
        self._user_config_path = config.config_path or Env.user_config_path
        self._d2d_enabled_cache: dict[PDRole, bool] = {}

        with self.config_lock:
            self.etcd_config = config.etcd_config
            self.etcd_tls_config = config.etcd_tls_config
            self.instance_assemble_timeout = config.instance_config.instance_assemble_timeout
            self.instance_assembler_check_interval = config.instance_config.instance_assembler_check_interval
            self.instance_assembler_cmd_send_internal = config.instance_config.instance_assembler_cmd_send_interval
            self.send_cmd_retry_times = config.instance_config.send_cmd_retry_times

        # Version control for data persistence
        self._data_version = 0
        self._version_lock = threading.Lock()

        with self.config_lock:
            self.etcd_client = EtcdClient(etcd_config=self.etcd_config, tls_config=self.etcd_tls_config)

        self.assemble_instance_thread = None
        self.start_command_thread = None

        self._initialized = True
        logger.info("InstanceAssembler initialized.")

    def start(self) -> None:
        """Start the instance assembler threads"""
        # Reset stop_event if it was previously set (for singleton reuse)
        if self.stop_event.is_set():
            self.stop_event.clear()

        # Try to restore data from ETCD, if failed,
        # it will start with empty state.
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if enable_persistence and not self.restore_data():
            logger.warning("Failed to restore instance assembler data from ETCD, starting with empty state")

        # Create instance assembler threads
        self.assemble_instance_thread = threading.Thread(
            target=self._instances_assembler_loop, daemon=True, name="InstanceAssemblerLoop"
        )
        self.start_command_thread = threading.Thread(
            target=self._start_commmand_sender, daemon=True, name="StartCommandSender"
        )

        self.assemble_instance_thread.start()
        self.start_command_thread.start()
        logger.info("InstanceAssembler started.")

    def stop(self) -> None:
        self.stop_event.set()
        with self.work_condition:
            self.work_condition.notify_all()
        # Only join threads that have been started
        if (
            hasattr(self, 'assemble_instance_thread')
            and self.assemble_instance_thread is not None
            and self.assemble_instance_thread.is_alive()
        ):
            self.assemble_instance_thread.join()
        if (
            hasattr(self, 'start_command_thread')
            and self.start_command_thread is not None
            and self.start_command_thread.is_alive()
        ):
            self.start_command_thread.join()

        logger.info("InstanceAssembler stopped.")

    def is_alive(self) -> bool:
        """Check if the instance_assembler threads are alive"""
        return (self.assemble_instance_thread is not None and self.assemble_instance_thread.is_alive()) and (
            self.start_command_thread is not None and self.start_command_thread.is_alive()
        )

    def update_config(self, config: ControllerConfig) -> None:
        """Update configuration for the instance assembler"""
        with self.config_lock:
            self._user_config_path = config.config_path or Env.user_config_path
            self._d2d_enabled_cache.clear()
            # Update config fields
            self.etcd_config = config.etcd_config
            self.etcd_tls_config = config.etcd_tls_config
            self.instance_assemble_timeout = config.instance_config.instance_assemble_timeout
            self.instance_assembler_check_interval = config.instance_config.instance_assembler_check_interval
            self.instance_assembler_cmd_send_internal = config.instance_config.instance_assembler_cmd_send_interval
            self.send_cmd_retry_times = config.instance_config.send_cmd_retry_times

            # Update ETCD client with new configuration
            self.etcd_client = EtcdClient(etcd_config=self.etcd_config, tls_config=self.etcd_tls_config)
            logger.info("InstanceAssembler configuration updated")

    def persist_data(self) -> bool:
        """Persist instance assembler data to ETCD with version control and checksum"""
        try:
            with self.lock:
                current_time = time.time()
                next_version = self._get_next_version()

                # Prepare instance assembler data - all data in one dict
                assembler_data = {"ins_id_cnt": self.ins_id_cnt, "instances": {}}
                for job_name, metadata in self.instances.items():
                    assembler_data["instances"][job_name] = metadata.model_dump(mode='json')
                logger.debug("Persisting instance assembler data - full data: %s", assembler_data)

                # Create persistent state with version control and checksum
                persistent_state = PersistentState(
                    data=assembler_data,
                    version=next_version,
                    timestamp=current_time,
                    checksum="",  # Will be calculated
                )
                persistent_state.checksum = persistent_state.calculate_checksum()
                logger.debug(
                    "Persisting instance assembler data - calculated checksum: %s, version: %s, timestamp: %s",
                    persistent_state.checksum,
                    next_version,
                    current_time,
                )

                # Convert PersistentState to dict for etcd storage
                dict_data = {"state": persistent_state.model_dump()}

            # Release lock before ETCD I/O to avoid blocking other operations
            success = self.etcd_client.persist_data("/controller/instance_assembler", dict_data)
            if success:
                logger.info("Successfully persisted instance assembler data with version %d", next_version)
            return success

        except Exception as e:
            logger.error("Error persisting instance assembler data: %s", e)
            return False

    def restore_data(self) -> bool:
        """Restore instance assembler data from ETCD with version control and validation"""
        try:
            persistent_states = self.etcd_client.restore_data("/controller/instance_assembler", PersistentState)
            if persistent_states is None:
                logger.info("No instance assembler data found in ETCD, starting with empty state")
                return True

            logger.info("Restoring instance assembler data from ETCD")

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
                logger.error("Data integrity check failed for instance_assembler, cannot restore")
                return False

            # Update data version
            with self._version_lock:
                self._data_version = max(self._data_version, persistent_state.version)

            with self.lock:
                self.instances.clear()

                # Restore ins_id_cnt
                self.ins_id_cnt = persistent_state.data.get("ins_id_cnt", 0)
                logger.info("Restored ins_id_cnt: %d (v%d)", self.ins_id_cnt, persistent_state.version)

                # Restore instances metadata
                instances_data = persistent_state.data.get("instances", {})
                valid_instances, invalid_instances = 0, 0

                for job_name, metadata_data in instances_data.items():
                    try:
                        metadata = AssembleInstanceMetadata.model_validate(metadata_data)
                        # After Controller restart, all subsequent registrations from pods
                        # will be reregister (not register). Mark restored instances as
                        # reregister so pods can rejoin assembly without being rejected.
                        metadata.is_reregister = True
                        self.instances[job_name] = metadata
                        logger.info(
                            "Restored instance assembler state for %s (v%d, is_reregister=True)",
                            job_name,
                            persistent_state.version,
                        )
                        valid_instances += 1
                    except Exception as e:
                        logger.error("Error reconstructing instance assembler state %s: %s", job_name, e)
                        invalid_instances += 1
                        continue

                logger.info(
                    "Successfully restored instance assembler data: %d valid instances, %d invalid instances skipped",
                    valid_instances,
                    invalid_instances,
                )
                return True
        except Exception as e:
            logger.error("Error restoring instance assembler data: %s", e)
            return False

    def register(self, msg: RegisterMsg) -> int:
        """
        Each node manager(nm) will register to instance assembler when it starts,
        and instance assembler will create or update the instance, then check
        wether the instance is ready to be start. If ready, notify the relative
        node manager to start inference engine and handle this instance to the
        instance manager to manager instance's status.
        """
        with self.lock:
            status = self._eval_register_status(msg.job_name)
            if status == RegisterStatus.ASSEMBLED:
                logger.info("Instance %s already registered, no need to register again.", msg.job_name)
                return -1
            elif status == RegisterStatus.NOT_REGISTERED:
                instance = Instance(
                    job_name=msg.job_name,
                    model_name=msg.model_name,
                    engine_type=msg.engine_type,
                    dispatch_capabilities=msg.dispatch_capabilities,
                    id=self.ins_id_cnt,
                    role=msg.role,
                    parallel_config=msg.parallel_config,
                    enable_multi_endpoints=msg.enable_multi_endpoints,
                )
                metadata = AssembleInstanceMetadata(
                    instance=instance,
                    register_timestamp=time.time(),
                    nnodes=msg.nnodes,
                )
                self.instances[msg.job_name] = metadata
                self.ins_id_cnt += 1
                logger.info("New instance %s(id:%d) created and added.", msg.job_name, instance.id)
            elif status == RegisterStatus.ASSEMBLING:
                metadata = self.instances[msg.job_name]
                if metadata.is_reregister:
                    logger.warning(
                        "Instance %s is being assembled via reregister, rejecting register from %s",
                        msg.job_name,
                        msg.pod_ip,
                    )
                    return -1
                with metadata.lock:
                    metadata.register_timestamp = time.time()

        if metadata.instance.has_node_mgr(msg.pod_ip):
            logger.info("Pod %s already registered in node_managers, skip duplicate registration.", msg.pod_ip)
            return 0

        if msg.is_master:
            with metadata.lock:
                # Only keep the existing master when its node is still registered (a genuine
                # concurrent conflict). If the old master was killed/filtered, let the new
                # is_master take over instead of dispatching a dead pod as master_dp_ip.
                if (
                    metadata.snapshot_dp_master_ip
                    and metadata.snapshot_dp_master_ip != msg.pod_ip
                    and metadata.instance.has_node_mgr(metadata.snapshot_dp_master_ip)
                ):
                    logger.warning(
                        "Instance %s already has snapshot_dp_master_ip=%s, ignoring conflicting is_master from %s",
                        msg.job_name,
                        metadata.snapshot_dp_master_ip,
                        msg.pod_ip,
                    )
                else:
                    metadata.snapshot_dp_master_ip = msg.pod_ip
                    logger.info(
                        "Recorded snapshot_dp_master_ip=%s for instance %s from is_master registration",
                        msg.pod_ip,
                        msg.job_name,
                    )

        metadata.instance.add_node_mgr(msg.pod_ip, msg.nm_port, msg.device_num)
        pod_endpoints = self._build_endpoints(msg, metadata)
        metadata.instance.add_endpoints(msg.pod_ip, pod_endpoints)

        logger.info("Endpoints added for instance %s from pod %s.", msg.job_name, msg.pod_ip)

        # Wake the assembler loop to process the new registration
        with self.work_condition:
            self.work_condition.notify_all()

        # Persist data on state change
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if enable_persistence and not self.persist_data():
            logger.warning("Failed to persist instance assembler data to ETCD")

        return 0

    def reregister(self, msg: ReregisterMsg) -> int:
        """
        When controller restarts, all node manager will re-register to controller,
        instance assembler will recover its instance info and max instance's id and
        max device's cluster id according to the reregister msg.
        """
        with self.lock:
            status = self._eval_register_status(msg.job_name)
            if status == RegisterStatus.ASSEMBLED:
                logger.info("Instance %s already registered, no need to reregister again.", msg.job_name)
                return -1
            elif status == RegisterStatus.NOT_REGISTERED:
                instance = Instance(
                    job_name=msg.job_name,
                    model_name=msg.model_name,
                    engine_type=msg.engine_type,
                    dispatch_capabilities=msg.dispatch_capabilities,
                    id=msg.instance_id,
                    role=msg.role,
                    parallel_config=msg.parallel_config,
                    enable_multi_endpoints=msg.enable_multi_endpoints,
                )
                metadata = AssembleInstanceMetadata(
                    instance=instance, register_timestamp=time.time(), is_reregister=True, nnodes=msg.nnodes
                )
                self.instances[msg.job_name] = metadata
                logger.info("New instance %s(id:%d) created and added by re-registration.", msg.job_name, instance.id)
            elif status == RegisterStatus.ASSEMBLING:
                metadata = self.instances[msg.job_name]
                if not metadata.is_reregister:
                    logger.warning(
                        "Instance %s is being assembled via register, rejecting reregister from %s",
                        msg.job_name,
                        msg.pod_ip,
                    )
                    return -1
                with metadata.lock:
                    metadata.register_timestamp = time.time()

            # recover ins_id_cnt
            self.ins_id_cnt = max(self.ins_id_cnt, msg.instance_id + 1)

        metadata.instance.add_node_mgr(msg.pod_ip, msg.nm_port, msg.device_num)
        # Re-registration carries original node_rank: mark slave endpoints as headless immediately
        for endpoint in msg.endpoints:
            if msg.node_rank != 0:
                endpoint.headless = True
        metadata.instance.add_endpoints(msg.pod_ip, {endpoint.id: endpoint for endpoint in msg.endpoints})
        logger.info("Recovery instance assembler's info, current ins_id_idx is %d.", self.ins_id_cnt)

        # Wake the assembler loop to process the re-registration
        with self.work_condition:
            self.work_condition.notify_all()

        # Persist data on state change
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if enable_persistence and not self.persist_data():
            logger.warning("Failed to persist instance assembler data to ETCD after reregistration")

        return 0

    def _eval_register_status(self, job_name: str) -> RegisterStatus:
        # First check if instance is already managed by InstanceManager (fully registered)
        if InstanceManager().has_active_instance_by_job_name(job_name):
            return RegisterStatus.ASSEMBLED

        # Then check if instance is still being assembled locally
        if job_name in self.instances:
            return RegisterStatus.ASSEMBLING

        # Instance not found anywhere
        return RegisterStatus.NOT_REGISTERED

    def _build_endpoints(self, msg: RegisterMsg, metadata: AssembleInstanceMetadata) -> dict[int, Endpoint]:
        id_offset = metadata.instance.get_endpoints_num()

        if msg.enable_multi_endpoints:
            pod_endpoints = self._build_multi_endpoints(msg, id_offset)
        else:
            pod_endpoints = self._build_single_endpoint(msg, id_offset)

        if msg.ranktable is not None:
            if metadata.ranktable is None:
                metadata.ranktable = msg.ranktable
            else:
                for server_info in msg.ranktable.server_list:
                    metadata.ranktable.server_list.append(server_info)
                metadata.ranktable.server_count = str(len(metadata.ranktable.server_list))

        return pod_endpoints

    def _build_single_endpoint(self, msg: RegisterMsg, id_offset: int) -> dict[int, Endpoint]:
        devices_per_endpoint = msg.parallel_config.local_world_size
        device_infos = self._build_device_infos(msg, 0, devices_per_endpoint, id_offset)

        logger.info("Building single endpoint for pod %s, %d devices per endpoint", msg.pod_ip, devices_per_endpoint)
        return {
            0: Endpoint(
                id=id_offset,
                ip=msg.pod_ip,
                business_port=msg.business_port[0],
                mgmt_port=msg.mgmt_port[0],
                device_infos=device_infos,
            )
        }

    def _build_multi_endpoints(self, msg: RegisterMsg, id_offset: int) -> dict[int, Endpoint]:
        devices_per_endpoint = msg.parallel_config.local_world_size
        total_devices_needed = len(msg.business_port) * devices_per_endpoint
        total_devices_available = msg.device_num

        logger.info(
            "Building multi endpoints: %d ports, %d devices per endpoint, total needed: %d, available: %d",
            len(msg.business_port),
            devices_per_endpoint,
            total_devices_needed,
            total_devices_available,
        )

        if total_devices_needed > total_devices_available:
            logger.warning(
                "Not enough devices: need %d, have %d. Will use available devices.",
                total_devices_needed,
                total_devices_available,
            )
            max_endpoints = total_devices_available // devices_per_endpoint
            actual_ports = msg.business_port[:max_endpoints]
            logger.info("Will create %d endpoints instead of %d", max_endpoints, len(msg.business_port))
        else:
            actual_ports = msg.business_port

        pod_endpoints: dict[int, Endpoint] = {}
        for i, port in enumerate(actual_ports):
            start_idx = devices_per_endpoint * i
            end_idx = start_idx + devices_per_endpoint

            if end_idx > msg.device_num:
                logger.warning("Not enough devices for endpoint %d, skipping", i)
                break

            device_infos = self._build_device_infos(msg, start_idx, devices_per_endpoint, id_offset)
            pod_endpoints[i] = Endpoint(
                id=id_offset + i,
                ip=msg.pod_ip,
                business_port=port,
                mgmt_port=msg.mgmt_port[i],
                device_infos=device_infos,
            )
        logger.debug("Built %d endpoints for pod %s", len(pod_endpoints), msg.pod_ip)
        return pod_endpoints

    def _build_device_infos(
        self, msg: RegisterMsg, start_idx: int, devices_per_endpoint: int, id_offset: int
    ) -> list[DeviceInfo]:
        if isinstance(msg.ranktable, Ranktable):
            return msg.ranktable.server_list[0].device

        device_infos = []
        for j in range(devices_per_endpoint):
            device_idx = start_idx + j
            if device_idx < msg.device_num:
                global_rank_id = id_offset * devices_per_endpoint + device_idx
                device_infos.append(DeviceInfo(device_id=str(device_idx), rank_id=str(global_rank_id)))
        return device_infos

    def _start_commmand_sender(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                job_names = list(self.instances.keys())

            with self.config_lock:
                max_retry_times = self.send_cmd_retry_times

            state_changed = False
            for job_name in job_names:
                with self.lock:
                    if job_name not in self.instances:
                        continue
                    metadata = self.instances[job_name]
                    with metadata.lock:
                        if metadata.register_status != RegisterStatus.ASSEMBLED:
                            continue

                if self._send_start_command(metadata):
                    logger.info("Start command sent for instance %s successfully.", job_name)
                    with self.lock:
                        self.instances.pop(job_name, None)
                    # Persist data on state change (instance removed after successful start command)
                    state_changed = True
                else:
                    retry_times = metadata.start_command_send_times + 1
                    if retry_times < max_retry_times:
                        logger.warning(
                            "Failed to send start command to instance %s with (%d/%d) times.",
                            job_name,
                            retry_times,
                            max_retry_times,
                        )
                        metadata.start_command_send_times = retry_times
                        # Persist data on state change (retry count updated)
                        state_changed = True
                    else:
                        logger.error(
                            "Failed to send start command to instance %s with (%d/%d) times, abort it.",
                            job_name,
                            retry_times,
                            max_retry_times,
                        )
                        with self.lock:
                            self.instances.pop(job_name, None)
                        # Persist data on state change (instance removed after max retries)
                        state_changed = True

            with self.config_lock:
                enable_persistence = self.etcd_config.enable_etcd_persistence
                sleep_interval = self.instance_assembler_cmd_send_internal

            # Persist data if any state changes occurred and persistence is enabled
            if state_changed and enable_persistence and not self.persist_data():
                logger.warning("Failed to persist instance assembler data to ETCD after sending start command")

            with self.work_condition:
                self.work_condition.wait(timeout=sleep_interval)

    def _send_start_command(self, metadata: AssembleInstanceMetadata) -> bool:
        is_succeed = True

        # If current is cold start instance, Master DP IP = first registered node (node_rank=0).
        # get_all_endpoints() filters headless slaves, confirming it's the master.

        # If current is snapshot restored instance, Master DP IP = registered node with is_master=True
        master_dp_ip = metadata.snapshot_dp_master_ip
        node_managers = metadata.instance.get_node_managers()
        if master_dp_ip:
            logger.info(
                "Using snapshot_dp_master_ip=%s as master_dp_ip for instance %s",
                master_dp_ip,
                metadata.instance.job_name,
            )
        elif node_managers:
            master_dp_ip = node_managers[0].pod_ip

        if not master_dp_ip:
            logger.error("Failed to find master DP address for instance %s", metadata.instance.job_name)
            return False

        d2d_enabled = self._is_d2d_enabled_for_role(metadata.instance.role)

        # node_rank within PCP group = registration_index % nnodes.
        # Re-registration not handled here — _start_commmand_sender skips re-registered instances.
        nnodes = metadata.nnodes
        for rank, node_mgr in enumerate(node_managers):
            endpoints = metadata.instance.get_endpoints(node_mgr.pod_ip)
            if not endpoints:
                continue

            d2d_peer_ips = None
            if d2d_enabled:
                endpoint_list = list(endpoints.values())
                d2d_peer_ips = self._collect_d2d_peer_ips(metadata, endpoint_list)
                if d2d_peer_ips:
                    logger.info(
                        "Collected D2D peer IPs for instance %s node %s: %s",
                        metadata.instance.job_name,
                        node_mgr.pod_ip,
                        d2d_peer_ips,
                    )

            start_cmd_msg = StartCmdMsg(
                job_name=metadata.instance.job_name,
                role=metadata.instance.role,
                instance_id=metadata.instance.id,
                endpoints=list(endpoints.values()),
                master_dp_ip=master_dp_ip,
                ranktable=metadata.ranktable,
                d2d_peer_ips=d2d_peer_ips,
                node_rank=rank % nnodes if nnodes > 1 else rank,
            )

            is_succeed = NodeManagerApiClient.send_start_command(node_mgr, start_cmd_msg) and is_succeed
        return is_succeed

    _D2D_SOURCE_SENTINEL = "auto"

    _ROLE_TO_SECTION_KEY = {
        PDRole.ROLE_E: "motor_engine_encode_config",
        PDRole.ROLE_P: "motor_engine_prefill_config",
        PDRole.ROLE_D: "motor_engine_decode_config",
        PDRole.ROLE_U: "motor_engine_union_config",
    }

    def _is_d2d_enabled_for_role(self, role: PDRole) -> bool:
        """Check if D2D is enabled by reading engine_config via resolver.

        Result is cached per role since the engine config does not change
        across individual _send_start_command calls.
        """
        if role in self._d2d_enabled_cache:
            cached = self._d2d_enabled_cache[role]
            logger.info("D2D for role %s: using cached value %s", role, cached)
            return cached
        config_path = self._user_config_path
        if not config_path:
            logger.warning("D2D check skipped for role %s: no user_config_path", role)
            enabled = False
        else:
            section_key = self._ROLE_TO_SECTION_KEY.get(role, "motor_engine_union_config")
            logger.info("Checking D2D for role=%s section=%s config=%s", role, section_key, config_path)
            try:
                resolver = BaseConfigResolver.load_section(config_path, section_key)
                d2d = resolver.get_d2d_config()
                if d2d is None:
                    logger.info("D2D not enabled for role %s: model_loader_extra_config not found or invalid", role)
                    enabled = False
                elif d2d.get("source") != self._D2D_SOURCE_SENTINEL:
                    logger.info(
                        "D2D not enabled for role %s: source=%s (expected '%s')",
                        role,
                        d2d.get("source"),
                        self._D2D_SOURCE_SENTINEL,
                    )
                    enabled = False
                elif d2d.get("listen_port") is None:
                    logger.warning(
                        "D2D SOURCE is 'auto' but LISTEN_PORT is not configured, D2D disabled for role %s", role
                    )
                    enabled = False
                else:
                    logger.info("D2D enabled for role %s: listen_port=%s", role, d2d.get("listen_port"))
                    enabled = True
            except Exception as e:
                logger.warning("Failed to check D2D status for role %s: %s", role, e)
                enabled = False
        self._d2d_enabled_cache[role] = enabled
        return enabled

    def _collect_d2d_peer_ips(
        self, metadata: AssembleInstanceMetadata, endpoint_list: list[Endpoint]
    ) -> list[str] | None:
        """Collect D2D peer entries for engines on one pod.

        Returns encoded endpoint.id:ip list (e.g. ["0:10.0.0.1", "1:10.0.0.3"]) so NM can
        route each entry to the matching engine. Port/device binding is in vllm_config.
        """
        if not endpoint_list:
            return None

        ep_ids = {ep.id for ep in endpoint_list}
        role = metadata.instance.role
        own_job_name = metadata.instance.job_name

        grouped: dict[int, set[str]] = {ep_id: set() for ep_id in ep_ids}
        for inst in InstanceManager().get_instances({InsStatus.ACTIVE}):
            if inst.role != role or inst.job_name == own_job_name:
                continue
            for ep in inst.get_all_endpoints(include_headless=True):
                if ep.id in ep_ids:
                    grouped[ep.id].add(ep.ip)

        encoded: list[str] = []
        for ep in endpoint_list:
            for ip in grouped.get(ep.id, set()):
                encoded.append(f"{ep.id}:{ip}")

        return encoded if encoded else None

    def _instances_assembler_loop(self) -> None:
        # Check all instances in assembling, if one instance is ready,
        # notify relative node manager to start inference engine and
        # handle this instance to instance manager.
        while not self.stop_event.is_set():
            with self.lock:
                keys = list(self.instances.keys())

            logger.debug("Assembling instance... remain %d instances.", len(keys))
            for job_name in keys:
                with self.lock:
                    if job_name not in self.instances:
                        logger.warning("Instance %s is not exist!", job_name)
                        continue
                    metadata = self.instances[job_name]
                    with metadata.lock:
                        if metadata.register_status == RegisterStatus.ASSEMBLED:
                            logger.info("Instance %s is already assembled!", job_name)
                            continue

                self._assemble_instance(metadata)

            with self.config_lock:
                check_interval = self.instance_assembler_check_interval
            with self.work_condition:
                self.work_condition.wait(timeout=check_interval)

    def _assemble_instance(self, metadata: AssembleInstanceMetadata) -> None:
        job_name = metadata.instance.job_name
        logger.debug("Assembling instance %s(id:%d)...", job_name, metadata.instance.id)
        need_persist = False

        # Filter abnormal endpoints before assembling
        self._filter_abnormal_endpoints(metadata.instance)
        # Drop stale snapshot master: if the recorded is_master node was killed
        # (filtered out above), never dispatch a dead pod as master_dp_ip.
        # Cold start never sets this field, so the lock-free guard makes this a no-op there.
        if metadata.snapshot_dp_master_ip:
            with metadata.lock:
                if metadata.snapshot_dp_master_ip and not metadata.instance.has_node_mgr(
                    metadata.snapshot_dp_master_ip
                ):
                    logger.warning(
                        "Clearing stale snapshot_dp_master_ip=%s for instance %s: node manager no longer registered",
                        metadata.snapshot_dp_master_ip,
                        job_name,
                    )
                    metadata.snapshot_dp_master_ip = None
        # Cross-node PCP: when nnodes > 1, each DP group needs nnodes nodes.
        # Total expected nodes = dp_size * nnodes (multi-endpoint) or world_size / device_num (single).
        nnodes = metadata.nnodes
        if isinstance(nnodes, int) and nnodes > 1:
            if metadata.instance.enable_multi_endpoints:
                dp_size = metadata.instance.parallel_config.dp_size if metadata.instance.parallel_config else 1
                expected_nodes = dp_size * nnodes
            else:
                expected_nodes = metadata.instance._get_expected_endpoint_count()
            is_ready = metadata.instance.get_node_managers_num() >= expected_nodes
            logger.debug(
                "Cross-node PCP readiness: %d/%d node managers registered (dp=%d, nnodes=%d)",
                metadata.instance.get_node_managers_num(),
                expected_nodes,
                metadata.instance.parallel_config.dp_size if metadata.instance.parallel_config else 1,
                nnodes,
            )
        else:
            is_ready = metadata.instance.is_endpoints_enough()

        if is_ready:
            # Cross-node PCP: mark slave endpoints as headless (original registration only).
            # Re-registration endpoints already have headless set during reregister().
            if isinstance(nnodes, int) and nnodes > 1 and not metadata.is_reregister:
                node_managers = metadata.instance.get_node_managers()
                for rank, node_mgr in enumerate(node_managers):
                    if rank % nnodes == 0:
                        continue
                    pod_endpoints = metadata.instance.get_endpoints(node_mgr.pod_ip)
                    for endpoint in pod_endpoints.values():
                        endpoint.headless = True
                    logger.info(
                        "Marked %d endpoint(s) as headless for slave node %s (node_rank=%d)",
                        len(pod_endpoints),
                        node_mgr.pod_ip,
                        rank,
                    )
                # Bump version to invalidate get_all_endpoints() cache after headless changes
                metadata.instance.invalidate_endpoints_cache()

            # All endpoints are healthy, assemble successfully
            with metadata.lock:
                metadata.register_status = RegisterStatus.ASSEMBLED
                if metadata.is_reregister:
                    # Reregister instance, just handle it to instance manager.
                    InstanceManager().add_instance(metadata.instance)
                    with self.lock:
                        self.instances.pop(job_name, None)
                    need_persist = True
                else:
                    # Only new registered instance need to send start command
                    # Keep it in instances with ASSEMBLED status for _start_commmand_sender to handle
                    InstanceManager().add_instance(metadata.instance)
                    # No need to persist for new registration until start command is sent

            # Wake the start-command sender — an instance is now ASSEMBLED
            with self.work_condition:
                self.work_condition.notify_all()
        else:
            # Assembling... check if this instance registration is timeout
            with self.config_lock:
                assemble_timeout = self.instance_assemble_timeout
            with metadata.lock:
                if time.time() - metadata.register_timestamp > assemble_timeout:
                    with self.lock:
                        self.instances.pop(job_name, None)
                    need_persist = True
                    logger.warning("Instance %s registration timed out and removed.", job_name)

        # Persist data on state change
        with self.config_lock:
            enable_persistence = self.etcd_config.enable_etcd_persistence
        if need_persist and enable_persistence and not self.persist_data():
            logger.warning("Failed to persist instance assembler data to ETCD")

    def _filter_abnormal_endpoints(self, instance: Instance) -> None:
        """
        Filter abnormal endpoints by checking node managers status.
        Remove any abnormal endpoints found during the check.
        """
        node_managers = instance.get_node_managers()
        if not node_managers:
            logger.warning(
                "No node managers found for instance %s(id:%d), cannot filter endpoints", instance.job_name, instance.id
            )
            return

        for node_mgr in node_managers:
            if not self._is_node_manager_alive(node_mgr, instance):
                instance.del_endpoints(node_mgr.pod_ip)
                instance.del_node_mgr(node_mgr.pod_ip, node_mgr.port)

        logger.info("Endpoint filtering completed for instance %s(id:%d)", instance.job_name, instance.id)

    def _is_node_manager_alive(self, node_mgr, instance: Instance) -> bool:
        """Check if a node manager is alive for instance"""
        try:
            _ = NodeManagerApiClient.query_status(node_mgr)
            # Only check if node manager is reachable and responsive, not endpoint status
            logger.debug(
                "Node manager %s:%s is reachable for instance %s(id:%d)",
                node_mgr.pod_ip,
                node_mgr.port,
                instance.job_name,
                instance.id,
            )
            return True
        except Exception as e:
            logger.warning(
                "Node manager %s:%s is not alive for instance %s(id:%d): %s",
                node_mgr.pod_ip,
                node_mgr.port,
                instance.job_name,
                instance.id,
                e,
            )
            return False

    def _get_next_version(self) -> int:
        """Get next data version for persistence"""
        with self._version_lock:
            self._data_version += 1
            return self._data_version
