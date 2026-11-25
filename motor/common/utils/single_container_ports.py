# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Assign single-container listen ports before any child process starts.

Coordinator, Controller, Render, KV store and every NodeManager share one IP.
Each process used to probe on its own, so one could move a port the others
still dial. This pass claims the whole set first and writes the result back.
"""

import json
import os
import shlex
from copy import deepcopy
from typing import Any

from motor.common.logger import get_logger
from motor.common.utils.port_allocator import PortAllocator, PortConflictError
from motor.config.config_utils import resolve_render_enabled
from motor.config.node_manager import NodeManagerConfig

logger = get_logger(__name__)

SCAN_RANGE = 100
BIND_HOST = "0.0.0.0"

# Same defaults the Docker single-container path already uses.
COORD_INFER = 1025
COORD_MGMT = 1026
COORD_OBS = 1027
CONTROLLER_API = 2026
CONTROLLER_OBS = 2027
KV_STORE_PORT = 50088
MMC_CONFIG_STORE = 50089
MMC_METRICS = 50090
MOONCAKE_METRICS = 50090
RENDER_PORT = 8100


def claim_port(preferred: int, name: str, taken: set[int]) -> int:
    """Keep ``preferred`` when it is free. Otherwise take the next free port."""
    chosen = PortAllocator.allocate_auto(
        BIND_HOST,
        int(preferred),
        name,
        scan_range=SCAN_RANGE,
        timeout=0.2,
        skip_ports=set(taken),
    )
    taken.add(chosen)
    return chosen


def _nested(mapping: dict, *keys: str) -> dict:
    current = mapping
    for key in keys:
        value = current.get(key)
        if not isinstance(value, dict):
            value = {}
            current[key] = value
        current = value
    return current


def _explicit_port(mapping: dict | None, key: str, default: int) -> int:
    if not isinstance(mapping, dict) or key not in mapping:
        return default
    value = mapping.get(key)
    if value in (None, ""):
        return default
    return int(value)


def _kv_enabled(user_config: dict) -> bool:
    if os.environ.get("KV_STORE_EXTERNAL", "0") == "1":
        return False
    engine_section = (
        user_config.get("motor_engine_prefill_config") or user_config.get("motor_engine_union_config") or {}
    )
    engine = engine_section.get("engine_config") if isinstance(engine_section, dict) else {}
    transfer = engine.get("kv_transfer_config") if isinstance(engine, dict) else {}
    connector = transfer.get("kv_connector") if isinstance(transfer, dict) else ""
    store = user_config.get("kv_cache_store_config")
    return connector == "MultiConnector" or (isinstance(store, dict) and bool(store))


def _instances(user_config: dict) -> list[tuple[str, int]]:
    """Instances the single-container script actually starts.

    The script greps for ``motor_engine_union_config`` and, when that key is
    present, starts only the union NodeManagers.
    """
    deploy = user_config.get("motor_deploy_config") or {}
    if isinstance(user_config.get("motor_engine_union_config"), dict):
        count = int(deploy.get("hybrid_instances_num") or 0)
        return [("union", index) for index in range(count)]
    roles: list[tuple[str, int]] = []
    for role, key in (("prefill", "p_instances_num"), ("decode", "d_instances_num")):
        count = int(deploy.get(key) or 0)
        roles.extend((role, index) for index in range(count))
    return roles


def collect_node_manager_ports(config_path: str, role: str, index: int) -> dict[str, Any] | None:
    """Ports this NodeManager would bind before its own allocator runs."""
    saved = {name: os.environ.get(name) for name in ("ROLE", "INDEX")}
    os.environ["ROLE"] = role
    os.environ["INDEX"] = str(index)
    try:
        config = NodeManagerConfig.from_json(config_path)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    single = config.single_container_config
    if not single.single_container_flag:
        return None
    reserved = [port for port in (config.kv_events_port, config.kv_replay_port) if port]
    return {
        "node_manager_port": int(config.api_config.node_manager_port),
        "service_ports": [int(port) for port in config.endpoint_config.service_ports],
        "kv_port": single.kv_port,
        "dp_rpc_port": single.dp_rpc_port,
        "reserved": reserved,
    }


def build_plan(user_config: dict, config_path: str) -> dict[str, Any]:
    """Claim every single-container listen port, moving only the ones that clash."""
    taken: set[int] = set()
    plan: dict[str, Any] = {"node_managers": {}}
    instances = []
    for role, index in _instances(user_config):
        ports = collect_node_manager_ports(config_path, role, index)
        if not ports:
            continue
        instances.append((role, index, ports))
        for reserved in ports["reserved"]:
            taken.add(int(reserved))

    coordinator = _child(user_config, "motor_coordinator_config", "api_config")
    plan["coordinator"] = {
        "coordinator_api_infer_port": claim_port(
            _explicit_port(coordinator, "coordinator_api_infer_port", COORD_INFER),
            "coordinator_api_infer_port",
            taken,
        ),
        "coordinator_api_mgmt_port": claim_port(
            _explicit_port(coordinator, "coordinator_api_mgmt_port", COORD_MGMT),
            "coordinator_api_mgmt_port",
            taken,
        ),
        "coordinator_obs_port": claim_port(
            _explicit_port(coordinator, "coordinator_obs_port", COORD_OBS),
            "coordinator_obs_port",
            taken,
        ),
    }

    controller = user_config.get("motor_controller_config")
    controller_api = controller.get("api_config") if isinstance(controller, dict) else None
    plan["controller"] = {
        "controller_api_port": claim_port(
            _explicit_port(controller_api, "controller_api_port", CONTROLLER_API),
            "controller_api_port",
            taken,
        ),
        "observability_api_port": claim_port(
            _explicit_port(controller_api, "observability_api_port", CONTROLLER_OBS),
            "observability_api_port",
            taken,
        ),
    }

    if _kv_enabled(user_config):
        store = user_config.get("kv_cache_store_config")
        has_store = isinstance(store, dict)
        if not has_store:
            store = {}
        backend = str(store.get("backend") or os.environ.get("KV_STORE_BACKEND") or "memcache")
        kv_plan = {
            "port": claim_port(_explicit_port(store, "port", KV_STORE_PORT), "kv_cache_store_port", taken),
            "backend": backend,
            "write_config": has_store,
        }
        if backend == "memcache":
            kv_plan["config_store_port"] = claim_port(
                _explicit_port(store, "config_store_port", MMC_CONFIG_STORE),
                "mmc_config_store_port",
                taken,
            )
            kv_plan["metrics_port"] = claim_port(
                _explicit_port(store, "metrics_port", MMC_METRICS),
                "mmc_metrics_port",
                taken,
            )
        elif backend == "mooncake":
            kv_plan["metrics_port"] = claim_port(MOONCAKE_METRICS, "mooncake_metrics_port", taken)
        plan["kv_store"] = kv_plan

    if resolve_render_enabled(user_config):
        render = _child(user_config, "motor_coordinator_config", "render_config") or {}
        endpoint = render.get("endpoint") if isinstance(render.get("endpoint"), dict) else None
        plan["render"] = claim_port(_explicit_port(endpoint, "port", RENDER_PORT), "render_port", taken)

    conductor = user_config.get("kv_conductor_config")
    if isinstance(conductor, dict) and conductor.get("http_server_port"):
        plan["conductor"] = claim_port(int(conductor["http_server_port"]), "kv_conductor_http_port", taken)

    for role, index, ports in instances:
        assigned = {
            "node_manager_port": claim_port(ports["node_manager_port"], f"{role}-{index}-node-manager", taken),
            "service_ports": [
                claim_port(port, f"{role}-{index}-business-{offset}", taken)
                for offset, port in enumerate(ports["service_ports"])
            ],
        }
        if ports["kv_port"] is not None:
            assigned["kv_port"] = claim_port(int(ports["kv_port"]), f"{role}-{index}-kv", taken)
        if ports["dp_rpc_port"] is not None:
            assigned["dp_rpc_port"] = claim_port(int(ports["dp_rpc_port"]), f"{role}-{index}-dp-rpc", taken)
        plan["node_managers"][f"{role}:{index}"] = assigned
    return plan


def apply_shared_ports(user_config: dict, plan: dict[str, Any]) -> dict:
    """Return a config copy whose shared listeners match ``plan``."""
    patched = deepcopy(user_config)
    coordinator = _nested(patched, "motor_coordinator_config", "api_config")
    coordinator.update(plan["coordinator"])
    controller = _nested(patched, "motor_controller_config", "api_config")
    controller.update(plan["controller"])

    kv_plan = plan.get("kv_store")
    if isinstance(kv_plan, dict) and kv_plan.get("write_config"):
        store = _nested(patched, "kv_cache_store_config")
        store["port"] = kv_plan["port"]
        if "config_store_port" in kv_plan:
            store["config_store_port"] = kv_plan["config_store_port"]
        if kv_plan.get("backend") == "memcache" and "metrics_port" in kv_plan:
            store["metrics_port"] = kv_plan["metrics_port"]

    if "render" in plan:
        endpoint = _nested(patched, "motor_coordinator_config", "render_config", "endpoint")
        endpoint["port"] = plan["render"]
    if "conductor" in plan:
        conductor = _nested(patched, "kv_conductor_config")
        conductor["http_server_port"] = plan["conductor"]
    return patched


def _child(mapping: dict, *keys: str) -> dict | None:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, dict) else None


def shell_exports(config_path: str, plan_path: str, plan: dict[str, Any]) -> str:
    """Shell assignments sourced by the single-container start script."""
    lines = [
        f"export USER_CONFIG_PATH={shlex.quote(config_path)}",
        f"export MOTOR_PORT_PLAN={shlex.quote(plan_path)}",
        "export MOTOR_PORTS_PLANNED=1",
    ]
    if "render" in plan:
        lines.append(f"export RENDER_PORT={int(plan['render'])}")
    if "conductor" in plan:
        lines.append(f"export KV_CONDUCTOR_PORT={int(plan['conductor'])}")
    kv_plan = plan.get("kv_store")
    if isinstance(kv_plan, dict):
        lines.append(f"export MOTOR_KV_CACHE_STORE_PORT={int(kv_plan['port'])}")
        if kv_plan.get("backend") == "memcache":
            config_store = "tcp://0.0.0.0:%s" % kv_plan["config_store_port"]
            metrics = "http://0.0.0.0:%s" % kv_plan["metrics_port"]
            lines.append(f"export MOTOR_MMC_CONFIG_STORE_URL={shlex.quote(config_store)}")
            lines.append(f"export MOTOR_MMC_METRICS_URL={shlex.quote(metrics)}")
        elif kv_plan.get("backend") == "mooncake":
            lines.append(f"export MOTOR_MOONCAKE_METRICS_PORT={int(kv_plan['metrics_port'])}")
    return "\n".join(lines)


def write_plan(config_path: str) -> str:
    with open(config_path, encoding="utf-8") as handle:
        user_config = json.load(handle)
    if not isinstance(user_config, dict):
        raise PortConflictError(f"{config_path} is not a JSON object")
    plan = build_plan(user_config, config_path)
    patched = apply_shared_ports(user_config, plan)
    directory = os.path.dirname(os.path.abspath(config_path))
    patched_path = os.path.join(directory, "user_config.single_container_ports.json")
    plan_path = os.path.join(directory, "single_container_port_plan.json")
    with open(patched_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(patched, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    with open(plan_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(plan, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    env_path = os.path.join(directory, "single_container_ports.env")
    with open(env_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(shell_exports(patched_path, plan_path, plan))
        handle.write("\n")
    logger.info("Single-container ports planned: %s", plan_path)
    return env_path


def main() -> int:
    config_path = os.environ.get("USER_CONFIG_PATH", "")
    if not config_path or not os.path.isfile(config_path):
        logger.error("USER_CONFIG_PATH is missing; cannot plan single-container ports")
        return 1
    try:
        write_plan(config_path)
    except (OSError, ValueError, PortConflictError) as exc:
        logger.error("Single-container port planning failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
