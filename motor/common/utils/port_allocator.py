# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
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
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass

from motor.common.logger import get_logger
from motor.common.utils.env import Env
from motor.common.utils.net import detect_family, format_address, split_address
from motor.common.utils.patch_check import safe_open
from motor.config.coordinator import CoordinatorConfig
from motor.config.controller import ControllerConfig
from motor.config.node_manager import (
    MOTOR_ENGINE_DECODE_CONFIG_KEY,
    MOTOR_ENGINE_ENCODE_CONFIG_KEY,
    MOTOR_ENGINE_PREFILL_CONFIG_KEY,
    MOTOR_ENGINE_UNION_CONFIG_KEY,
    NodeManagerConfig,
)
from motor.config.port_allocator_config import PortAllocatorConfig
from motor.config.resolver import ConfigResolver, normalize_keys

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


def _strip_endpoint_host(endpoint: str) -> str:
    rest = endpoint.split("://", 1)[-1]
    return rest.split("/", 1)[0]


def _alloc_strategy(pac: PortAllocatorConfig, kind: str) -> str:
    return kind if pac.enable else "config"


def _maybe_strict(pac: PortAllocatorConfig, host: str, port: int, name: str, timeout: float) -> int:
    if not pac.enable:
        return port
    return PortAllocator.allocate_strict(host, port, name, timeout=timeout)


def _maybe_auto(
    pac: PortAllocatorConfig,
    host: str,
    port: int,
    name: str,
    scan_range: int,
    timeout: float,
    skip_ports: set[int] | None = None,
) -> int:
    if not pac.enable:
        return port
    return PortAllocator.allocate_auto(host, port, name, scan_range=scan_range, timeout=timeout, skip_ports=skip_ports)


def _append_etcd(rows: list[PortRow], etcd) -> None:
    if not etcd.enable_etcd_persistence:
        return
    rows.append(_row("etcd", etcd.etcd_host, etcd.etcd_port, "remote", "etcd persistence"))


_ENGINE_SECTION_BY_ROLE = {
    "encode": MOTOR_ENGINE_ENCODE_CONFIG_KEY,
    "prefill": MOTOR_ENGINE_PREFILL_CONFIG_KEY,
    "decode": MOTOR_ENGINE_DECODE_CONFIG_KEY,
    "union": MOTOR_ENGINE_UNION_CONFIG_KEY,
    "both": MOTOR_ENGINE_UNION_CONFIG_KEY,
}


def _pod_role(role) -> str:
    """Role key used to pick this pod's engine section.

    ``str(PDRole.ROLE_P)`` is ``PDRole.ROLE_P`` on the runtime that printed the
    2026-09-24 logs, so the enum value is read first.
    """
    if role is not None:
        value = getattr(role, "value", None)
        if isinstance(value, str) and value:
            return value.lower()
        text = str(role)
        suffix = text.rsplit(".", 1)[-1].lower()
        aliases = {
            "role_p": "prefill",
            "role_d": "decode",
            "role_e": "encode",
            "role_u": "union",
            "both": "union",
        }
        return aliases.get(suffix, text.lower())
    return (Env.role or "").lower()


def _role_name(role: str | None) -> str:
    mapping = {
        "encode": "Encode",
        "prefill": "Prefill",
        "decode": "Decode",
        "union": "Union",
        "both": "Union",
    }
    return mapping.get((role or "").lower(), "Engine")


def _as_port(value) -> int | None:
    if value in (None, "", False):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _load_user_config(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    with safe_open(path, "r") as handle:
        raw = json.load(handle)
    return raw if isinstance(raw, dict) else {}


def _kv_transfer_listen_ports(engine_cfg: dict) -> list[tuple[int, str]]:
    kv = engine_cfg.get("kv_transfer_config")
    if not isinstance(kv, dict) or not kv:
        return []
    ports: list[tuple[int, str]] = []
    extra = kv.get("kv_connector_extra_config")
    extra = extra if isinstance(extra, dict) else {}
    connector = kv.get("kv_connector")
    if connector == "MultiConnector":
        connectors = extra.get("connectors")
        if isinstance(connectors, list) and connectors:
            first = connectors[0] if isinstance(connectors[0], dict) else {}
            kv_port = _as_port(first.get("kv_port"))
            if kv_port:
                ports.append((kv_port, "KV transfer"))
            if len(connectors) > 1 and isinstance(connectors[1], dict):
                store = connectors[1]
                if store.get("kv_connector") != "UCMConnector":
                    lookup = store.get("lookup_rpc_port")
                    store_extra = store.get("kv_connector_extra_config")
                    if lookup is None and isinstance(store_extra, dict):
                        lookup = store_extra.get("lookup_rpc_port")
                    lookup_port = _as_port(lookup)
                    if lookup_port:
                        ports.append((lookup_port, "KV lookup RPC"))
        return ports
    kv_port = _as_port(kv.get("kv_port"))
    if kv_port:
        ports.append((kv_port, "KV transfer"))
    lookup_port = _as_port(extra.get("lookup_rpc_port"))
    if lookup_port:
        ports.append((lookup_port, "KV lookup RPC"))
    return ports


def _append_pd_engine_ports(
    rows: list[PortRow],
    config: NodeManagerConfig,
    host: str,
    seen: set[tuple[int, str]],
) -> None:
    role = _pod_role(config.basic_config.role) or (Env.role or "").lower()
    component = _role_name(role)
    section_key = _ENGINE_SECTION_BY_ROLE.get(role)
    raw = _load_user_config(config.config_path)
    section = raw.get(section_key) if section_key else None
    if not isinstance(section, dict):
        return

    def _emit(port, purpose: str) -> None:
        parsed = _as_port(port)
        if parsed is None or (parsed, purpose) in seen:
            return
        seen.add((parsed, purpose))
        rows.append(_row(component, host, parsed, "config", purpose))

    resolver = ConfigResolver(section)
    _emit(resolver.get_parallel_config().get("dp_rpc_port"), "DP RPC")
    engine_cfg = normalize_keys(section.get("engine_config") or {})
    if isinstance(engine_cfg, dict):
        for port, purpose in _kv_transfer_listen_ports(engine_cfg):
            _emit(port, purpose)
        _emit(engine_cfg.get("master_port") or engine_cfg.get("master-port"), "PCP master")
        _emit(
            engine_cfg.get("disaggregation_bootstrap_port") or engine_cfg.get("disaggregation-bootstrap-port"),
            "PD bootstrap",
        )
        ml_extra = engine_cfg.get("model_loader_extra_config")
        if isinstance(ml_extra, str):
            try:
                ml_extra = json.loads(ml_extra)
            except json.JSONDecodeError:
                ml_extra = None
        if isinstance(ml_extra, dict):
            _emit(ml_extra.get("listen_port") or ml_extra.get("LISTEN_PORT"), "D2D listen")


def apply_coordinator_ports(config: CoordinatorConfig) -> None:
    pac = config.port_allocator_config
    host, scan_range, probe_timeout, remote_timeout = _allocator(pac)
    rows: list[PortRow] = []
    api = config.api_config
    kind = _alloc_strategy(pac, "strict")

    api.coordinator_api_infer_port = _maybe_strict(
        pac,
        host,
        api.coordinator_api_infer_port,
        "coordinator_api_infer_port",
        probe_timeout,
    )
    rows.append(_row("Coordinator", host, api.coordinator_api_infer_port, kind, "infer API (external)"))

    kind_auto = _alloc_strategy(pac, "auto")
    api.coordinator_api_mgmt_port = _maybe_auto(
        pac,
        host,
        api.coordinator_api_mgmt_port,
        "coordinator_api_mgmt_port",
        scan_range,
        probe_timeout,
    )
    rows.append(_row("Coordinator", host, api.coordinator_api_mgmt_port, kind_auto, "mgmt API"))

    api.coordinator_obs_port = _maybe_auto(
        pac,
        host,
        api.coordinator_obs_port,
        "coordinator_obs_port",
        scan_range,
        probe_timeout,
    )
    rows.append(_row("Coordinator", host, api.coordinator_obs_port, kind_auto, "observability API"))

    iwc = config.inference_workers_config
    if iwc.worker_metaserver_base_port > 0:
        base_port = iwc.worker_metaserver_base_port
        for idx in range(iwc.num_workers):
            rows.append(_row("Coordinator", host, base_port + idx, "config", f"worker metaserver #{idx}"))

    kv_cfg = config.scheduler_config.kv_conductor_config
    if kv_cfg.conductor_service:
        cond_host, cond_port = _parse_host_port(kv_cfg.conductor_service, kv_cfg.http_server_port)
        if cond_host:
            reachable = True
            if pac.enable:
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

        kv_cfg.http_server_port = _maybe_auto(
            pac,
            host,
            kv_cfg.http_server_port,
            "http_server_port",
            scan_range,
            probe_timeout,
        )
        rows.append(_row("Coordinator", host, kv_cfg.http_server_port, kind_auto, "Conductor callback HTTP"))

    render = config.render_config
    if render.enabled:
        rows.append(
            _row(
                "Render",
                render.endpoint.host,
                render.endpoint.port,
                "config",
                "vLLM Render sidecar",
            )
        )

    pmc = config.prometheus_metrics_config
    if pmc.enable_kv_store_metrics:
        metrics_port = pmc.kv_store_metrics_port
        if not metrics_port:
            metrics_port = 50088 if (pmc.kv_store_backend or "").lower() == "mooncake" else 50090
        metrics_host = pmc.kv_store_service or "remote"
        endpoint = pmc.kv_store_metrics_endpoint
        if endpoint:
            parsed_host, parsed_port = _parse_host_port(_strip_endpoint_host(endpoint), metrics_port)
            if parsed_host:
                metrics_host = parsed_host
            if parsed_port:
                metrics_port = parsed_port
        rows.append(_row("KVMetrics", metrics_host, metrics_port, "remote", "KV store Prometheus scrape"))

    _append_etcd(rows, config.etcd_config)
    PortAllocator.print_matrix(rows)


def apply_controller_ports(config: ControllerConfig) -> None:
    pac = config.port_allocator_config
    host, scan_range, probe_timeout, _ = _allocator(pac)
    rows: list[PortRow] = []
    api = config.api_config

    api.controller_api_port = _maybe_strict(
        pac,
        host,
        api.controller_api_port,
        "controller_api_port",
        probe_timeout,
    )
    rows.append(
        _row("Controller", host, api.controller_api_port, _alloc_strategy(pac, "strict"), "mgmt API (external)")
    )

    api.observability_api_port = _maybe_auto(
        pac,
        host,
        api.observability_api_port,
        "observability_api_port",
        scan_range,
        probe_timeout,
    )
    rows.append(_row("Controller", host, api.observability_api_port, _alloc_strategy(pac, "auto"), "observability API"))

    _append_etcd(rows, config.etcd_config)
    PortAllocator.print_matrix(rows)


def apply_node_manager_ports(config: NodeManagerConfig) -> None:
    pac = config.port_allocator_config
    host, scan_range, probe_timeout, _ = _allocator(pac)
    rows: list[PortRow] = []
    api = config.api_config
    ep = config.endpoint_config
    kind_auto = _alloc_strategy(pac, "auto")
    role = _pod_role(config.basic_config.role) or (Env.role or "").lower()
    engine = _role_name(role)
    seen: set[tuple[int, str]] = set()

    reserved = {api.node_manager_port} | {int(p) for p in ep.service_ports}
    sc = config.single_container_config
    if sc.single_container_flag:
        reserved |= {p for p in (sc.kv_port, sc.lookup_rpc_port, sc.dp_rpc_port) if p}
    allocated: set[int] = set()

    def _auto(pref: int, name: str) -> int:
        p = _maybe_auto(
            pac,
            host,
            pref,
            name,
            scan_range,
            probe_timeout,
            skip_ports=(reserved - {pref}) | allocated,
        )
        allocated.add(p)
        return p

    def _emit(component: str, port: int, strategy: str, purpose: str) -> None:
        parsed = _as_port(port)
        if parsed is None or (parsed, purpose) in seen:
            return
        seen.add((parsed, purpose))
        rows.append(_row(component, host, parsed, strategy, purpose))

    api.node_manager_port = _auto(api.node_manager_port, "node_manager_port")
    _emit("NodeManager", api.node_manager_port, kind_auto, "NM API")

    if ep.service_ports:
        new_service_ports: list[str] = []
        for idx, svc_pref in enumerate(ep.service_ports):
            svc_port = _auto(int(svc_pref), f"service_ports[{idx}]")
            new_service_ports.append(str(svc_port))
            _emit(engine, svc_port, kind_auto, f"DP{idx} business")
        ep.service_ports = new_service_ports
    else:
        _emit(engine, ep.base_port, "config", "business base")

    if ep.bootstrap_port:
        _emit(engine, ep.bootstrap_port, "config", "PD bootstrap")

    if sc.single_container_flag:
        if sc.kv_port is not None:
            sc.kv_port = _auto(sc.kv_port, "kv_port")
            _emit(engine, sc.kv_port, kind_auto, "KV transfer")
        if sc.lookup_rpc_port is not None:
            sc.lookup_rpc_port = _auto(sc.lookup_rpc_port, "lookup_rpc_port")
            _emit(engine, sc.lookup_rpc_port, kind_auto, "KV lookup RPC")
        if sc.dp_rpc_port is not None:
            sc.dp_rpc_port = _auto(sc.dp_rpc_port, "dp_rpc_port")
            _emit(engine, sc.dp_rpc_port, kind_auto, "DP RPC")

    kcfg = config.kv_cache_store_config
    if kcfg.enable:
        _emit("KVStore", kcfg.port, "config", "KV MetaService RPC")
        if kcfg.store_http_port > 0:
            _emit("KVStore", kcfg.store_http_port, "config", "Mooncake store HTTP")

    if not sc.single_container_flag:
        _append_pd_engine_ports(rows, config, host, seen)
    PortAllocator.print_matrix(rows)


def run_port_setup_or_exit(apply_fn, config) -> None:
    try:
        apply_fn(config)
    except PortConflictError as exc:
        logger.error("%s Aborting.", exc)
        sys.exit(1)
