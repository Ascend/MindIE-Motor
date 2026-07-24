# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass

from motor.common.logger import get_logger
from motor.common.utils.net import detect_family, format_address, split_address
from motor.config.coordinator import CoordinatorConfig
from motor.config.controller import ControllerConfig
from motor.config.node_manager import NodeManagerConfig
from motor.config.port_allocator_config import PortAllocatorConfig

logger = get_logger(__name__)

_MATRIX_PREFIX = "[Port Matrix]"


@dataclass(frozen=True)
class PortRow:
    component: str
    bind_host: str
    port: int
    proto: str
    strategy: str
    purpose: str


def print_matrix(rows: list[PortRow]) -> None:
    if not rows:
        return
    logger.info("%s ================================================================", _MATRIX_PREFIX)
    logger.info(
        "%s Component     Bind Host       Port    Proto   Strategy    Purpose",
        _MATRIX_PREFIX,
    )
    logger.info("%s ----------------------------------------------------------------", _MATRIX_PREFIX)
    for row in rows:
        logger.info(
            "%s %-13s %-15s %-7d %-7s %-11s %s",
            _MATRIX_PREFIX,
            row.component,
            row.bind_host,
            row.port,
            row.proto,
            row.strategy,
            row.purpose,
        )
    logger.info("%s ================================================================", _MATRIX_PREFIX)


class PortConflictError(RuntimeError):
    """Raised when a port cannot be allocated under the chosen strategy."""


def _socket_host(host: str) -> str:
    """Normalize a host literal for socket bind/connect (strip URL brackets)."""
    if host.startswith("[") and host.endswith("]"):
        return host[1:-1]
    return host


class PortAllocator:
    @staticmethod
    def probe_tcp(host: str, port: int, timeout: float = 0.5) -> bool:
        bind_host = _socket_host(host)
        sock = socket.socket(detect_family(bind_host), socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.settimeout(timeout)
            sock.bind((bind_host, port))
            sock.listen(1)
            return True
        except OSError:
            return False
        finally:
            sock.close()

    @staticmethod
    def allocate_strict(host: str, port: int, name: str, timeout: float = 0.5) -> int:
        if PortAllocator.probe_tcp(host, port, timeout=timeout):
            return port
        raise PortConflictError(f"[Port] {name} port {port} is in use on {host}; this port must be exclusive.")

    @staticmethod
    def allocate_auto(
        host: str,
        port: int,
        name: str,
        scan_range: int = 100,
        timeout: float = 0.5,
        skip_ports: set[int] | None = None,
    ) -> int:
        blocked = skip_ports or set()
        for candidate in range(port, port + scan_range):
            if candidate in blocked:
                continue
            if PortAllocator.probe_tcp(host, candidate, timeout=timeout):
                if candidate != port:
                    logger.warning(
                        "[Port] %s: preferred port %d busy, using %d on %s",
                        name,
                        port,
                        candidate,
                        host,
                    )
                return candidate
        raise PortConflictError(f"[Port] {name}: no free port in [{port}, {port + scan_range - 1}] on {host}.")

    @staticmethod
    def allocate_auto_broadcast(
        host: str,
        port: int,
        name: str,
        broadcast_fn: Callable[[int], None],
        scan_range: int = 100,
        timeout: float = 0.5,
    ) -> int:
        chosen = PortAllocator.allocate_auto(host, port, name, scan_range=scan_range, timeout=timeout)
        try:
            broadcast_fn(chosen)
        except Exception as exc:
            raise PortConflictError(f"[Port] {name}: allocated {chosen} but broadcast failed: {exc}") from exc
        return chosen

    @staticmethod
    def check_remote_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
        connect_host = _socket_host(host)
        sock = socket.socket(detect_family(connect_host), socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect((connect_host, port))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    @staticmethod
    def print_matrix(rows: list[PortRow]) -> None:
        print_matrix(rows)


def _row(component: str, bind_host: str, port: int, strategy: str, purpose: str) -> PortRow:
    return PortRow(component, bind_host, port, "TCP", strategy, purpose)


def _allocator(cfg: PortAllocatorConfig) -> tuple[str, int, float, float]:
    return (
        cfg.bind_host,
        cfg.scan_range,
        cfg.probe_timeout_seconds,
        cfg.remote_check_timeout_seconds,
    )


def _parse_host_port(address: str, default_port: int) -> tuple[str, int]:
    if not address:
        return "", default_port
    host, port_str = split_address(address)
    if not port_str:
        return host or "127.0.0.1", default_port
    try:
        return host or "127.0.0.1", int(port_str)
    except ValueError:
        return address, default_port


def apply_coordinator_ports(config: CoordinatorConfig) -> None:
    pac = config.port_allocator_config
    if not pac.enable:
        return

    host, scan_range, probe_timeout, remote_timeout = _allocator(pac)
    rows: list[PortRow] = []
    api = config.api_config

    api.coordinator_api_infer_port = PortAllocator.allocate_strict(
        host,
        api.coordinator_api_infer_port,
        "coordinator_api_infer_port",
        timeout=probe_timeout,
    )
    rows.append(_row("Coordinator", host, api.coordinator_api_infer_port, "strict", "infer API (external)"))

    api.coordinator_api_mgmt_port = PortAllocator.allocate_auto(
        host,
        api.coordinator_api_mgmt_port,
        "coordinator_api_mgmt_port",
        scan_range=scan_range,
        timeout=probe_timeout,
    )
    rows.append(_row("Coordinator", host, api.coordinator_api_mgmt_port, "auto", "mgmt API"))

    api.coordinator_obs_port = PortAllocator.allocate_auto(
        host,
        api.coordinator_obs_port,
        "coordinator_obs_port",
        scan_range=scan_range,
        timeout=probe_timeout,
    )
    rows.append(_row("Coordinator", host, api.coordinator_obs_port, "auto", "observability API"))

    kv_cfg = config.scheduler_config.kv_conductor_config
    if kv_cfg.conductor_service:
        cond_host, cond_port = _parse_host_port(kv_cfg.conductor_service, kv_cfg.http_server_port)
        if cond_host:
            reachable = PortAllocator.check_remote_reachable(cond_host, cond_port, timeout=remote_timeout)
            rows.append(
                _row(
                    "Conductor",
                    cond_host,
                    cond_port,
                    "remote",
                    f"KV pool conductor (reachable={reachable})",
                )
            )
            if not reachable:
                logger.warning(
                    "[Port] Mooncake Conductor %s not reachable at startup",
                    format_address(cond_host, cond_port),
                )

        kv_cfg.http_server_port = PortAllocator.allocate_auto(
            host,
            kv_cfg.http_server_port,
            "http_server_port",
            scan_range=scan_range,
            timeout=probe_timeout,
        )
        rows.append(_row("Coordinator", host, kv_cfg.http_server_port, "auto", "Conductor callback HTTP"))

    PortAllocator.print_matrix(rows)


def apply_controller_ports(config: ControllerConfig) -> None:
    pac = config.port_allocator_config
    if not pac.enable:
        return

    host, scan_range, probe_timeout, _ = _allocator(pac)
    rows: list[PortRow] = []
    api = config.api_config

    api.controller_api_port = PortAllocator.allocate_strict(
        host,
        api.controller_api_port,
        "controller_api_port",
        timeout=probe_timeout,
    )
    rows.append(_row("Controller", host, api.controller_api_port, "strict", "mgmt API (external)"))

    api.observability_api_port = PortAllocator.allocate_auto(
        host,
        api.observability_api_port,
        "observability_api_port",
        scan_range=scan_range,
        timeout=probe_timeout,
    )
    rows.append(_row("Controller", host, api.observability_api_port, "auto", "observability API"))

    PortAllocator.print_matrix(rows)


def apply_node_manager_ports(config: NodeManagerConfig) -> None:
    pac = config.port_allocator_config
    if not pac.enable:
        return

    host, scan_range, probe_timeout, _ = _allocator(pac)
    rows: list[PortRow] = []
    api = config.api_config
    ep = config.endpoint_config

    reserved = {api.node_manager_port} | {int(p) for p in ep.service_ports + ep.mgmt_ports}
    sc = config.single_container_config
    if sc.single_container_flag:
        reserved |= {p for p in (sc.kv_port, sc.lookup_rpc_port, sc.dp_rpc_port) if p}
    allocated: set[int] = set()

    def _auto(pref: int, name: str) -> int:
        p = PortAllocator.allocate_auto(
            host, pref, name, scan_range=scan_range, timeout=probe_timeout, skip_ports=(reserved - {pref}) | allocated
        )
        allocated.add(p)
        return p

    api.node_manager_port = _auto(api.node_manager_port, "node_manager_port")
    rows.append(_row("NodeManager", host, api.node_manager_port, "auto", "NM API"))

    new_service_ports: list[str] = []
    new_mgmt_ports: list[str] = []
    for idx, (svc_pref, mgmt_pref) in enumerate(zip(ep.service_ports, ep.mgmt_ports)):
        svc_port = _auto(int(svc_pref), f"service_ports[{idx}]")
        mgmt_port = _auto(int(mgmt_pref), f"mgmt_ports[{idx}]")
        new_service_ports.append(str(svc_port))
        new_mgmt_ports.append(str(mgmt_port))
        rows.append(_row("EngineServer", host, svc_port, "auto", f"DP{idx} business"))
        rows.append(_row("EngineServer", host, mgmt_port, "auto", f"DP{idx} mgmt"))

    ep.service_ports = new_service_ports
    ep.mgmt_ports = new_mgmt_ports

    if sc.single_container_flag:
        if sc.kv_port is not None:
            sc.kv_port = _auto(sc.kv_port, "kv_port")
            rows.append(_row("EngineServer", host, sc.kv_port, "auto", "KV transfer"))
        if sc.lookup_rpc_port is not None:
            sc.lookup_rpc_port = _auto(sc.lookup_rpc_port, "lookup_rpc_port")
            rows.append(_row("EngineServer", host, sc.lookup_rpc_port, "auto", "KV lookup RPC"))
        if sc.dp_rpc_port is not None:
            sc.dp_rpc_port = _auto(sc.dp_rpc_port, "dp_rpc_port")
            rows.append(_row("EngineServer", host, sc.dp_rpc_port, "auto", "DP RPC"))

    PortAllocator.print_matrix(rows)


def run_port_setup_or_exit(apply_fn, config) -> None:
    try:
        apply_fn(config)
    except PortConflictError as exc:
        logger.error("%s Aborting.", exc)
        sys.exit(1)
