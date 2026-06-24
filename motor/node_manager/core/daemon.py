# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import signal
import ipaddress
import subprocess
import threading

from motor.common.resources.instance import PDRole
from motor.common.resources.endpoint import Endpoint
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.logger import get_logger
from motor.common.utils.env import Env
from motor.config.node_manager import NodeManagerConfig
from motor.common.utils.snapshot_utils import MOTOR_SNAPSHOT_METADATA_PATH


logger = get_logger(__name__)
MAX_PORT = 65535
MIN_PORT = 1024


class Daemon(ThreadSafeSingleton):
    def __init__(self, config: NodeManagerConfig | None = None):
        if hasattr(self, "_initialized"):
            return

        self.engine_pids: list[int] = []
        if config is None:
            config = NodeManagerConfig.from_json()

        # related config
        self.parallel_config = config.basic_config.parallel_config
        self.device_num = config.basic_config.device_num
        self.single_container_flag = config.single_container_config.single_container_flag
        self.enable_multi_endpoints = config.basic_config.enable_multi_endpoints
        self.enable_snapshot = config.snapshot_config.enable_snapshot
        self.snapshot_metadata_path = (
            config.snapshot_config.snapshot_metadata_path
            if config.snapshot_config.snapshot_metadata_path != ""
            else MOTOR_SNAPSHOT_METADATA_PATH
        )
        if self.single_container_flag:
            self.device_offset = config.single_container_config.device_offset
            self.kv_port = config.single_container_config.kv_port
            self.lookup_rpc_port = config.single_container_config.lookup_rpc_port
            self.dp_rpc_port = config.single_container_config.dp_rpc_port

        self._initialized = True
        self._pids_lock = threading.Lock()

    @staticmethod
    def _check_params(params: Endpoint) -> bool:
        try:
            port = int(params.business_port)
            if not (MIN_PORT <= port <= MAX_PORT):
                logger.error("Port %s is out of valid range", port)
                return False
        except ValueError:
            logger.error("Invalid port value: %s", params.business_port)
            return False
        try:
            ipaddress.ip_address(params.ip)
        except ValueError:
            logger.error("Invalid IP address: %s", params.ip)
            return False
        except Exception as e:
            logger.error("Error validating IP address %s: %s", params.ip, e)
            return False

        return True

    @staticmethod
    def _to_engine_role(pd_role_info: PDRole) -> str:
        if pd_role_info == PDRole.ROLE_U:
            return "union"
        return str(pd_role_info.value)

    def pull_engine(
        self,
        pd_role_info: PDRole,
        endpoints_info: list[Endpoint],
        instance_id: int,
        master_dp_ip: str,
        d2d_peer_ips: list[str] | None = None,
        node_rank: int = 0,
    ):
        """
        start engine processes based on the provided role and endpoint information.
        engine_server parameters:
            --dp-rank engine dpGroup rank
            --engine-id
            --role  prefill | decode | union
            --host engine service ip
            --port engine service port
            --mgmt-port endpoint management port
            --master-dp-ip master data parallel node IP address
            --node-rank node rank assigned by Controller (registration order)
            --config-path engine config file path
        """
        try:
            env = os.environ.copy()
            pod_ip = env.get("POD_IP")
            if pod_ip and not env.get("VLLM_HOST_IP"):
                env["VLLM_HOST_IP"] = pod_ip
            if env.get("MOONCAKE_ASCEND_IPV6_EXPERIMENT") == "1":
                env["MC_USE_IPV6"] = env.get("MC_USE_IPV6", "1")
            device_size = self.device_num
            for i, endpoint in enumerate(endpoints_info):
                if not self._check_params(endpoint):
                    raise ValueError("Invalid endpoint parameters")

                if self.enable_multi_endpoints:
                    device_ids_str = self._calc_visible_device_ids(i, device_size)
                    logger.info("Device IDs: %s", device_ids_str)
                    env["ASCEND_RT_VISIBLE_DEVICES"] = device_ids_str

                cmd = [
                    "engine_server",
                    "--dp-rank",
                    str(endpoint.id),
                    "--instance-id",
                    str(instance_id),
                    "--role",
                    self._to_engine_role(pd_role_info),
                    "--host",
                    str(endpoint.ip),
                    "--port",
                    str(int(endpoint.business_port)),
                    "--mgmt-port",
                    str(int(endpoint.mgmt_port)),
                    "--master-dp-ip",
                    master_dp_ip,
                    "--node-rank",
                    str(node_rank),
                    "--config-path",
                    str(Env.user_config_path),
                ]
                if self.enable_snapshot:
                    cmd.extend(["--snapshot-metadata", self.snapshot_metadata_path])
                if self.single_container_flag:
                    cmd.extend(["--kv-port", str(self.kv_port)])
                    cmd.extend(["--dp-rpc-port", str(self.dp_rpc_port)])
                    if self.lookup_rpc_port is not None:
                        cmd.extend(["--lookup-rpc-port", str(self.lookup_rpc_port)])
                if d2d_peer_ips:
                    ep_id = str(endpoint.id)
                    peer_ips = []
                    for entry in d2d_peer_ips:
                        encoded_ep_id, ip = entry.split(":", 1)
                        if encoded_ep_id == ep_id:
                            peer_ips.append(ip)
                    if peer_ips:
                        cmd.extend(["--d2d-peer-ips", ",".join(peer_ips)])
                    logger.info("D2D peer IPs for ep_id %s: %s", endpoint.id, peer_ips)
                logger.info(" ".join(cmd))
                process = subprocess.Popen(cmd, shell=False, env=env)  # pylint: disable=consider-using-with
                if process.poll() is not None:
                    raise RuntimeError("Engine process exited immediately with code %s" % process.returncode)
                with self._pids_lock:
                    self.engine_pids.append(process.pid)
        except Exception as e:
            raise RuntimeError("Failed to pull engine: %s" % e) from e

    def stop(self):
        with self._pids_lock:
            pids = list(self.engine_pids)
            self.engine_pids.clear()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                logger.info("Killed engine process with PID: %s", pid)
            except ProcessLookupError:
                logger.info("Process %s already terminated", pid)
            except PermissionError:
                logger.error("No permission to kill process %s", pid)
            except Exception as e:
                logger.error("Failed to kill process %s: %s", pid, e)

    def _calc_visible_device_ids(self, index: int, device_size: int) -> str:
        """Calculate visible device IDs string for ASCEND_RT_VISIBLE_DEVICES.
        Returns:
            Comma-separated device IDs string, e.g., "0,1,2,3"
        """
        local_world_size = self.parallel_config.local_world_size
        start_device_id = index * local_world_size % device_size
        end_device_id = start_device_id + local_world_size
        if end_device_id > device_size:
            device_ids = list(range(start_device_id, device_size)) + list(range(0, end_device_id - device_size))
        else:
            device_ids = list(range(start_device_id, end_device_id))
        if self.single_container_flag:
            device_ids = [x + self.device_offset for x in device_ids]
        return ",".join(map(str, device_ids))
