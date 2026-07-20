# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import os
import signal
import threading
import time
import shutil

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.http_msg_spec import Ranktable, RegisterMsg, StartCmdMsg, ReregisterMsg
from motor.common.utils.env import Env
from motor.common.logger import get_logger
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.config.node_manager import HardwareType, NodeManagerConfig
from motor.node_manager.api_client.controller_api_client import ControllerApiClient
from motor.node_manager.core.fault_reporter import FaultReporter
from motor.node_manager.core.api_ready_event import wait_until_api_ready
from motor.common.utils.snapshot_utils import (
    load_snapshot_metadata,
    update_snapshot_metadata,
    get_pod_ip,
    is_restored_from_host_side_snapshot,
    MOTOR_SNAPSHOT_WORKSPACE_DIR,
    MOTOR_SNAPSHOT_METADATA_PATH,
    MOTOR_SNAPSHOT_WEIGHT_DIR,
    MOTOR_SNAPSHOT_CONFIGMAP_DIR,
)

logger = get_logger(__name__)


class EngineManager(ThreadSafeSingleton):
    def __init__(self, config: NodeManagerConfig | None = None) -> None:
        if hasattr(self, "_initialized"):
            return

        self.endpoints: list[Endpoint] = []
        if config is None:
            config = NodeManagerConfig.from_json()
        self._config = config
        self.config_lock = threading.RLock()
        self.ranktable: Ranktable = None
        self.instance_ranktable: Ranktable = None
        self.instance_id: int = 0
        self.d2d_peer_ips: list[str] | None = None
        self.node_rank: int = 0
        self.is_working = False

        # for snapshot restore, should be recorded during a snapshot-enabled cold start
        self.is_snapshot_master = False

        self._fault_reporter = FaultReporter(config)

        self._register_thread = threading.Thread(target=self._register, daemon=True, name="engine_register")
        self._register_thread.start()

        self._initialized = True
        logger.info("Engine Manager module initialized.")

    def start(self) -> None:
        """Start engine manager background threads."""
        self._fault_reporter.start(self.endpoints)
        logger.info("EngineManager started.")

    def update_config(self, config: NodeManagerConfig) -> None:
        """Update configuration for the engine manager.

        Supports dynamically enabling/disabling the fault reporting thread
        when enable_fault_tolerance changes.
        """
        with self.config_lock:
            self._config = config

        self._fault_reporter.update_config(config, self.endpoints)
        logger.info("EngineManager configuration updated.")

    def get_snapshot_metadata_path(self) -> str:
        # if snapshot_metadata_path is set, return it, otherwise using configmap mounted snapshot_metadata.json and return default MOTOR_SNAPSHOT_METADATA_PATH
        if self._config.snapshot_config.snapshot_metadata_path != "":
            return self._config.snapshot_config.snapshot_metadata_path

        # Configmap mounted snapshot_metadata.json is read-only
        # Copy new snapshot_metadada.json from /snapshot/configmap/data/ to /snapshot
        mounted_snapshot_metadata = os.path.join(MOTOR_SNAPSHOT_CONFIGMAP_DIR, "snapshot_metadata.json")
        if os.path.exists(mounted_snapshot_metadata):
            shutil.copy(mounted_snapshot_metadata, MOTOR_SNAPSHOT_METADATA_PATH)

        return MOTOR_SNAPSHOT_METADATA_PATH

    def engine_suspend_prepare(self) -> None:
        if not self._config.snapshot_config.enable_snapshot:
            return
        snapshot_dirs = [MOTOR_SNAPSHOT_WORKSPACE_DIR, MOTOR_SNAPSHOT_WEIGHT_DIR]
        for path in snapshot_dirs:
            os.makedirs(path, exist_ok=True)

        snapshot_metadata_path = self.get_snapshot_metadata_path()

        if not os.path.exists(snapshot_metadata_path):
            with open(snapshot_metadata_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

        try:
            model_save_path = load_snapshot_metadata(snapshot_metadata_path, "model_save_path")
            logger.info("[snapshot] Keep existing model_save_path from snapshot metadata: %s", model_save_path)
        except Exception:
            if self._config.snapshot_config.snapshot_metadata_path != "":
                return
            update_snapshot_metadata(snapshot_metadata_path, "model_save_path", MOTOR_SNAPSHOT_WEIGHT_DIR)
            logger.info(
                "[snapshot] Updated default model_save_path to snapshot metadata: %s", MOTOR_SNAPSHOT_WEIGHT_DIR
            )

    def is_engine_checkpoint_done(self) -> bool:
        if not self._config.snapshot_config.enable_snapshot:
            return True

        snapshot_metadata_path = self.get_snapshot_metadata_path()

        try:
            checkpoint = load_snapshot_metadata(snapshot_metadata_path, "checkpoint")
            if checkpoint == "done":
                return True
            return False
        except Exception as e:
            # Failed to read checkpoint field or snapshot metadata file format error, consider checkpoint not done
            logger.debug("[snapshot] Container checkpoint is not done: %s", e)
            return False

    def register_prepare_after_restore(self) -> None:
        """Update configuration for the engine manager after restore from host side snapshot"""
        if not self._config.snapshot_config.enable_snapshot:
            return

        snapshot_metadata_path = self.get_snapshot_metadata_path()
        if not os.path.exists(snapshot_metadata_path):
            logger.error("[snapshot] Snapshot metadata file do not exist when restore from host side snapshot")
            return

        restored_job_name = load_snapshot_metadata(snapshot_metadata_path, "job_name")
        restored_namespace = None
        try:
            restored_namespace = load_snapshot_metadata(snapshot_metadata_path, "namespace")
        except Exception:
            controller_host = (
                ControllerApiClient.controller_config.api_config.controller_api_dns or Env.controller_service or ""
            )
            if controller_host.endswith(".svc.cluster.local"):
                raise

        with self.config_lock:
            # Refresh job_name
            self._config.basic_config.job_name = restored_job_name
            logger.info("[snapshot] Refreshed job_name after restore: %s", restored_job_name)
            # Refresh pod_ip
            self._config.api_config.pod_ip = get_pod_ip()
            logger.info("[snapshot] Refreshed pod_ip after restore: %s", self._config.api_config.pod_ip)
            # Refresh controller_api_dns
            if restored_namespace is not None:
                dns = ControllerApiClient.controller_config.api_config.controller_api_dns or Env.controller_service
                if dns:
                    host = dns.split(".", 1)[0]
                    ControllerApiClient.controller_config.api_config.controller_api_dns = (
                        f"{host}.{restored_namespace}.svc.cluster.local"
                    )
                    logger.info(
                        "[snapshot] Refreshed controller_api_dns after restore: %s",
                        ControllerApiClient.controller_config.api_config.controller_api_dns,
                    )

    def engine_resume_prepare(self, start_msg: StartCmdMsg) -> None:
        """Append data_parallel_master_ip to snapshot metadata for engine resume after restore from host side snapshot"""
        if not self._config.snapshot_config.enable_snapshot:
            return

        snapshot_metadata_path = self.get_snapshot_metadata_path()

        try:
            model_load_path = load_snapshot_metadata(snapshot_metadata_path, "model_load_path")
            logger.info("[snapshot] Keep existing model_load_path from snapshot metadata: %s", model_load_path)
        except Exception:
            if self._config.snapshot_config.snapshot_metadata_path != "":
                return
            update_snapshot_metadata(snapshot_metadata_path, "model_load_path", MOTOR_SNAPSHOT_WEIGHT_DIR)
            logger.info(
                "[snapshot] Updated default model_load_path to snapshot metadata: %s", MOTOR_SNAPSHOT_WEIGHT_DIR
            )

        try:
            master_ip = load_snapshot_metadata(snapshot_metadata_path, "data_parallel_master_ip")
            logger.info("[snapshot] Keep existing data_parallel_master_ip from snapshot metadata: %s", master_ip)
        except Exception:
            update_snapshot_metadata(snapshot_metadata_path, "data_parallel_master_ip", start_msg.master_dp_ip)
            logger.info(
                "[snapshot] Updated data_parallel_master_ip in start_msg to snapshot metadata: %s",
                start_msg.master_dp_ip,
            )

    def post_register_msg(self) -> bool | None:
        register_msg = self._gen_register_msg()
        if register_msg is None:
            return False
        logger.debug("register_msg is %s", register_msg)

        return ControllerApiClient.register(register_msg)

    def post_reregister_msg(self) -> bool | None:
        reregister_msg = self._gen_reregister_msg()
        if reregister_msg is None:
            return False
        logger.debug("reregister_msg is %s", reregister_msg)

        return ControllerApiClient.re_register(reregister_msg)

    def parse_start_cmd(self, start_cmd: StartCmdMsg):
        if not self._check_cmd_para(start_cmd):
            return False
        logger.info("start_cmd is %s", start_cmd)
        self.instance_id = start_cmd.instance_id
        self.endpoints = start_cmd.endpoints
        self.d2d_peer_ips = start_cmd.d2d_peer_ips
        self.node_rank = start_cmd.node_rank

        if (
            self._config.snapshot_config.enable_snapshot
            and not is_restored_from_host_side_snapshot()
            and self.node_rank == 0
        ):
            self.is_snapshot_master = True

        self._write_ranktable_to_file(start_cmd.ranktable)
        return True

    def stop(self) -> None:
        self._fault_reporter.stop()
        try:
            if hasattr(self, "_register_thread") and self._register_thread.is_alive():
                self._register_thread.join(timeout=2.0)
        except Exception as e:
            logger.error("Failed to stop engine manager: %s", e)

    def _write_ranktable_to_file(self, ins_ranktable: Ranktable | None):
        """Write the instance's ranktable to a local JSON file."""
        if ins_ranktable is None:
            logger.info("Ranktable is None, skip writing to file")
            return

        output_path = Env.ranktable_path
        if output_path is None:
            logger.warning("RANKTABLE_PATH env is not set, skip writing ranktable to file")
            return

        try:
            # If ranktable is Ranktable type, use model_dump; otherwise, use as list[DeviceInfo]
            if isinstance(ins_ranktable, Ranktable):
                rk_dump = ins_ranktable.model_dump(exclude_none=True)
            else:
                rk_dump = ins_ranktable

            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(rk_dump, f, ensure_ascii=False, indent=2)

            logger.info("Ranktable written to %s", output_path)
        except Exception as e:
            logger.error("Failed to write ranktable to file: %s", e)

    def _check_cmd_para(self, start_cmd: StartCmdMsg) -> bool:
        # Read config values under lock protection
        with self.config_lock:
            job_name = self._config.basic_config.job_name
            endpoint_num = self._config.endpoint_config.endpoint_num
            pod_ip = self._config.api_config.pod_ip

        if start_cmd.job_name != job_name or len(start_cmd.endpoints) != endpoint_num:
            logger.error("check job_name:%s, endpoint_num:%d error", job_name, endpoint_num)
            return False
        for endpoint in start_cmd.endpoints:
            if endpoint.ip != pod_ip:
                logger.error("check pod_ip %s error", pod_ip)
                return False
        return True

    def _register(self) -> None:
        # Wait for NodeManagerAPI to be ready before registering
        # Import here to avoid circular import

        logger.info("Waiting for NodeManagerAPI to be ready before registering...")
        if not wait_until_api_ready(timeout=30.0):
            logger.error("NodeManagerAPI did not become ready within timeout, registration may fail")
        else:
            logger.info("NodeManagerAPI is ready, proceeding with registration")

        max_retries = 5
        retry_interval = 2
        retries = 0

        while retries < max_retries:
            logger.info("Attempting registration (Attempt %d of %d)...", retries + 1, max_retries)
            success = self.post_register_msg()

            if success:
                return
            else:
                retries += 1
                if retries < max_retries:
                    logger.warning("Registration attempt %d failed. Retrying in %d seconds...", retries, retry_interval)
                    time.sleep(retry_interval)
                    retry_interval = retry_interval * 2
                else:
                    logger.error("Registration failed after maximum retries.")

        logger.error("Failed to register after 5 attempts.")
        try:
            # triggering the signal handler in main using a process signal
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception as e:
            logger.error("failed to send SIGTERM after registration failure: %s", e)

    def _check_config_paras(self) -> bool:
        # Read config values under lock protection
        with self.config_lock:
            job_name = self._config.basic_config.job_name

        if job_name is None:
            logger.error("job name is None, please check")
            return False
        return True

    def _get_ranktable(self) -> Ranktable | None:
        """Get ranktable from HCCL file"""
        with self.config_lock:
            hardware_type = str(self._config.basic_config.hardware_type)
        if HardwareType.is_a5(hardware_type):
            logger.info("A5 platform does not require ranktable, skip loading from HCCL file")
            return None
        try:
            with open(Env.hccl_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if self._config.single_container_config.single_container_flag:
                device_offset = self._config.single_container_config.device_offset
                device_num = self._config.single_container_config.device_num
                server_list_key = 'server_list'
                device_key = 'device'
                if (
                    server_list_key in data
                    and len(data[server_list_key]) > 0
                    and device_key in data[server_list_key][0]
                ):
                    data[server_list_key][0][device_key] = data[server_list_key][0][device_key][
                        device_offset : device_offset + device_num
                    ]
            return Ranktable(**data)
        except Exception as e:
            logger.error("Failed to load ranktable from %s: %s", Env.hccl_path, e)
            return None

    def _gen_register_msg(self) -> RegisterMsg | None:
        if not self._check_config_paras():
            return None

        self.ranktable = self._get_ranktable()

        # Read config values under lock protection
        with self.config_lock:
            job_name = self._config.basic_config.job_name
            model_name = self._config.basic_config.model_name
            engine_type = self._config.basic_config.engine_type
            dispatch_capabilities = self._config.basic_config.dispatch_capabilities
            role = self._config.basic_config.role
            pod_ip = self._config.api_config.pod_ip
            business_port = self._config.endpoint_config.service_ports
            mgmt_port = self._config.endpoint_config.mgmt_ports
            node_manager_port = self._config.api_config.node_manager_port
            parallel_config = self._config.basic_config.parallel_config
            enable_multi_endpoints = self._config.basic_config.enable_multi_endpoints
            device_num = self._config.basic_config.device_num
            nnodes = self._config.basic_config.nnodes

        register_msg = RegisterMsg(
            job_name=job_name,
            model_name=model_name,
            engine_type=engine_type,
            dispatch_capabilities=dispatch_capabilities,
            role=role,
            pod_ip=pod_ip,
            business_port=business_port,
            mgmt_port=mgmt_port,
            nm_port=str(node_manager_port),
            parallel_config=parallel_config,
            enable_multi_endpoints=enable_multi_endpoints,
            device_num=device_num,
            ranktable=self.ranktable,
            nnodes=nnodes,
            is_master=self.is_snapshot_master,
        )
        return register_msg

    def _gen_reregister_msg(self) -> ReregisterMsg | None:
        if not self._check_config_paras():
            return None
        if len(self.endpoints) == 0 or self.instance_id <= 0:
            logger.error(
                "para check fail for reregister, please checklen[endpoints]:%d, instance_id:%s",
                len(self.endpoints),
                type(self.instance_id),
            )
            return None

        # Read config values under lock protection
        with self.config_lock:
            job_name = self._config.basic_config.job_name
            model_name = self._config.basic_config.model_name
            engine_type = self._config.basic_config.engine_type
            dispatch_capabilities = self._config.basic_config.dispatch_capabilities
            role = self._config.basic_config.role
            pod_ip = self._config.api_config.pod_ip
            node_manager_port = self._config.api_config.node_manager_port
            parallel_config = self._config.basic_config.parallel_config
            enable_multi_endpoints = self._config.basic_config.enable_multi_endpoints
            device_num = self._config.basic_config.device_num
            nnodes = self._config.basic_config.nnodes

        reregister_msg = ReregisterMsg(
            job_name=job_name,
            model_name=model_name,
            engine_type=engine_type,
            dispatch_capabilities=dispatch_capabilities,
            role=role,
            pod_ip=pod_ip,
            nm_port=str(node_manager_port),
            parallel_config=parallel_config,
            enable_multi_endpoints=enable_multi_endpoints,
            device_num=device_num,
            instance_id=self.instance_id,
            endpoints=self.endpoints,
            nnodes=nnodes,
            node_rank=self.node_rank,
        )
        return reregister_msg
