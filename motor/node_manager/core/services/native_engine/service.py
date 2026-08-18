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
import threading
import time

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import PDRole
from motor.common.logger import get_logger
from motor.common.utils.net import format_address
from motor.node_manager.core.services.native_engine.factory import get_backend
from motor.node_manager.core.services.native_engine.models import LaunchContext, RuntimeState
from motor.node_manager.core.services.native_engine.supervisor import ProcessSupervisor
from motor.node_manager.core.services.registry import SERVICE_ENGINE, register_service

logger = get_logger(__name__)


def _create_native_engine(hardware_type: str, config):
    """Factory for NativeEngineService — keeps constructor details out of the daemon."""
    del hardware_type
    return NativeEngineService(
        engine_type=config.basic_config.engine_type,
        config_path=config.config_path,
        device_num=config.basic_config.device_num,
        parallel_config=config.basic_config.parallel_config,
        enable_multi_endpoints=config.basic_config.enable_multi_endpoints,
        single_container_flag=config.single_container_config.single_container_flag,
        device_offset=config.single_container_config.device_offset,
        kv_port=config.single_container_config.kv_port,
        lookup_rpc_port=config.single_container_config.lookup_rpc_port,
        dp_rpc_port=config.single_container_config.dp_rpc_port,
    )


@register_service(SERVICE_ENGINE, backend="engine", factory=_create_native_engine)
class NativeEngineService:
    """Manage engine subprocess lifecycle: start, track PIDs, stop."""

    def __init__(
        self,
        engine_type: str,
        config_path: str,
        device_num: int,
        parallel_config,
        enable_multi_endpoints: bool,
        single_container_flag: bool = False,
        device_offset: int = 0,
        kv_port: int | None = None,
        lookup_rpc_port: int | None = None,
        dp_rpc_port: int | None = None,
    ):
        self.engine_type = str(engine_type).strip().lower()
        self.config_path = config_path
        self.device_num = device_num
        self.parallel_config = parallel_config
        self.enable_multi_endpoints = enable_multi_endpoints
        self.single_container_flag = single_container_flag
        self.device_offset = device_offset
        self.kv_port = kv_port
        self.lookup_rpc_port = lookup_rpc_port
        self.dp_rpc_port = dp_rpc_port
        self.backend = get_backend(self.engine_type)
        self.supervisor = ProcessSupervisor()
        self._pull_lock = threading.Lock()
        # Number of engine relaunches performed in this container's lifetime;
        # used to label the log separators between successive engine launches.
        self._restart_count = 0

    def pull(
        self,
        pd_role_info: PDRole,
        endpoints_info: list[Endpoint],
        instance_id: int,
        master_dp_ip: str,
        d2d_peer_ips: list[str] | None = None,
        node_rank: int = 0,
    ):
        """Launch native engine subprocesses for every endpoint on this node."""
        with self._pull_lock:
            self._pull(pd_role_info, endpoints_info, instance_id, master_dp_ip, d2d_peer_ips, node_rank)

    def _pull(
        self,
        pd_role_info: PDRole,
        endpoints_info: list[Endpoint],
        instance_id: int,
        master_dp_ip: str,
        d2d_peer_ips: list[str] | None,
        node_rank: int,
    ) -> None:
        started_endpoint_ids: list[int] = []
        try:
            base_env = os.environ.copy()
            pod_ip = base_env.get("POD_IP")
            if pod_ip and not base_env.get("VLLM_HOST_IP"):
                base_env["VLLM_HOST_IP"] = pod_ip
            if base_env.get("MOONCAKE_ASCEND_IPV6_EXPERIMENT") == "1":
                base_env["MC_USE_IPV6"] = base_env.get("MC_USE_IPV6", "1")
            device_size = self.device_num
            for i, endpoint in enumerate(endpoints_info):
                env = base_env.copy()
                if self.enable_multi_endpoints:
                    device_ids_str = self._calc_visible_device_ids(i, device_size)
                    logger.info("Device IDs: %s", device_ids_str)
                    env["ASCEND_RT_VISIBLE_DEVICES"] = device_ids_str

                peer_ips = self._get_d2d_peer_ips(endpoint.id, d2d_peer_ips)
                if d2d_peer_ips:
                    logger.info("D2D peer IPs for ep_id %s: %s", endpoint.id, list(peer_ips))

                context = LaunchContext(
                    role=pd_role_info,
                    instance_id=instance_id,
                    dp_rank=endpoint.id,
                    node_rank=node_rank,
                    host=endpoint.ip,
                    business_port=int(endpoint.business_port),
                    mgmt_port=int(endpoint.mgmt_port),
                    config_path=self.config_path,
                    master_dp_ip=master_dp_ip,
                    kv_port=self.kv_port if self.single_container_flag else None,
                    lookup_rpc_port=self.lookup_rpc_port if self.single_container_flag else None,
                    dp_rpc_port=self.dp_rpc_port if self.single_container_flag else None,
                    d2d_peer_ips=peer_ips,
                    environment=env,
                    headless=endpoint.headless,
                )
                launch_spec = self.backend.prepare(context)
                cmd = list(launch_spec.command.argv)
                logger.info(" ".join(cmd))
                if self.supervisor.start(endpoint.id, launch_spec.command, launch_spec.probe):
                    started_endpoint_ids.append(endpoint.id)

        except Exception as e:
            for endpoint_id in reversed(started_endpoint_ids):
                self.supervisor.stop(endpoint_id)
            raise RuntimeError(f"Failed to pull engine: {e}") from e

    def stop(self) -> list[int]:
        """Gracefully stop all native process groups, then force-kill on timeout."""
        return self.supervisor.stop_all()

    def pid_list(self) -> list[int]:
        return self.supervisor.pid_list()

    def runtime_state(self, endpoint: Endpoint) -> RuntimeState:
        return self.supervisor.state(endpoint.id, endpoint.ip, int(endpoint.business_port))

    def metrics_target(self, endpoint: Endpoint) -> str | None:
        """Return the native metrics URL for a routable local endpoint."""
        if endpoint.headless:
            return None
        probe = self.supervisor.probe_spec(endpoint.id)
        if probe is None:
            return None
        scheme = "https" if probe.tls_config and probe.tls_config.enable_tls else "http"
        return f"{scheme}://{format_address(endpoint.ip, endpoint.business_port)}/metrics"

    def restart(
        self,
        pd_role_info: PDRole,
        endpoints_info: list[Endpoint],
        instance_id: int,
        master_dp_ip: str,
        d2d_peer_ips: list[str] | None = None,
        node_rank: int = 0,
    ) -> None:
        """Relaunch the native engines in place: stop the old process groups,
        then pull fresh ones. Owns the whole relaunch lifecycle (the Daemon
        stays engine-agnostic).

        The engines inherit this process's stdout/stderr, so every relaunch
        appends to the same container log file — a prominent separator is
        printed between the old and the new engine logs to mark the relaunch
        number.
        """
        logger.info("Restarting native engines for instance %d (stop -> pull)", instance_id)
        self.stop()
        self._log_restart_separator(instance_id)
        self.pull(pd_role_info, endpoints_info, instance_id, master_dp_ip, d2d_peer_ips, node_rank)

    def _log_restart_separator(self, instance_id: int) -> None:
        """Print a prominent separator marking the N-th engine relaunch.

        Engine subprocesses inherit this process's stdout/stderr, so their
        logs accumulate in the same container log file. This banner (printed
        between the old engines' stop and the new engines' pull) makes each
        relaunch clearly delimited and greppable: ``[ENGINE RELAUNCH #N]``.
        """
        self._restart_count += 1
        banner = (
            "\n"
            f"{'=' * 24} [ENGINE RELAUNCH #{self._restart_count}] "
            f"instance_id={instance_id} {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"{'=' * 24}\n"
        )
        # Straight to the container stdout stream (k8s collects it as the
        # container log file) — independent of this process's logger setup.
        print(banner, flush=True)
        logger.info("Engine relaunch #%d separator printed to container log", self._restart_count)

    def wait_ready(self, endpoints: list[Endpoint], timeout: float = 60.0) -> None:
        """Wait until every native engine's readiness probe succeeds.

        The probe hits the engine's business port (the OpenAI API), which only
        accepts connections once the model finished loading — so this waits
        out the (potentially long) model load. Returns on timeout as well:
        the HeartbeatManager's STARTING-preserves-status semantics keep the
        loading window from being misreported as a death.
        """
        deadline = time.monotonic() + timeout
        for endpoint in endpoints:
            address = format_address(endpoint.ip, endpoint.business_port)
            logger.info("Waiting for native engine at %s to become ready...", address)
            while time.monotonic() < deadline:
                if self.supervisor.state(endpoint.id, endpoint.ip, int(endpoint.business_port)) == RuntimeState.READY:
                    logger.info("Native engine at %s is ready.", address)
                    break
                time.sleep(1)

    def health_check(self) -> list:
        """Return deaths ``[(pid, endpoint_id)]`` for the Daemon's death handling.

        Whether to freeze suicide arbitration and wait for an in-place
        relaunch, or let the pod self-terminate (k8s restarts the container),
        is the Daemon's decision — gated by ``enable_engine_relaunch`` in the
        NodeManager config. This service only surfaces dead PIDs.
        """
        dead = self.supervisor.dead_pids()
        if not dead:
            return []
        logger.warning("Engine PIDs %s died", [pid for pid, _ in dead])
        return dead

    def _calc_visible_device_ids(self, index: int, device_size: int) -> str:
        local_world_size = self.parallel_config.local_world_size
        start_device_id = index * local_world_size % device_size
        end_device_id = start_device_id + local_world_size
        if end_device_id > device_size:
            device_ids = list(range(start_device_id, device_size)) + list(range(end_device_id - device_size))
        else:
            device_ids = list(range(start_device_id, end_device_id))
        if self.single_container_flag:
            device_ids = [x + self.device_offset for x in device_ids]
        return ",".join(map(str, device_ids))

    @staticmethod
    def _get_d2d_peer_ips(endpoint_id: int, d2d_peer_ips: list[str] | None) -> tuple[str, ...]:
        if not d2d_peer_ips:
            return ()
        endpoint_id_text = str(endpoint_id)
        peers = []
        for entry in d2d_peer_ips:
            encoded_endpoint_id, ip = entry.split(":", 1)
            if encoded_endpoint_id == endpoint_id_text:
                peers.append(ip)
        return tuple(peers)
