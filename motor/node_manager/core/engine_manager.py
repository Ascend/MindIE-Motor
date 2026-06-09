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

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.http_msg_spec import Ranktable, RegisterMsg, StartCmdMsg, ReregisterMsg
from motor.common.utils.env import Env
from motor.common.logger import get_logger
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.config.node_manager import HardwareType, NodeManagerConfig
from motor.node_manager.api_client.controller_api_client import ControllerApiClient
from motor.node_manager.core.fault_reporter import FaultReporter

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
        from motor.node_manager.api_server.node_manager_api import NodeManagerAPI

        logger.info("Waiting for NodeManagerAPI to be ready before registering...")
        if not NodeManagerAPI.wait_until_ready(timeout=30.0):
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
                server_list_key = "server_list"
                device_key = "device"
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
