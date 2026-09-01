# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Docker deploy config parsing, role identity, start_motor rendering, and docker CLI."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field

import lib.constant as C

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

_PD_SEPARATION_DEPLOY_KEYS = {
    C.P_INSTANCES_NUM,
    C.D_INSTANCES_NUM,
    C.SINGER_P_INSTANCES_NUM,
    C.SINGER_D_INSTANCES_NUM,
    C.P_POD_NPU_NUM,
    C.D_POD_NPU_NUM,
}
_PD_HYBRID_REQUIRED_DEPLOY_KEYS = {
    C.HYBRID_INSTANCES_NUM,
    C.SINGLE_HYBRID_INSTANCE_POD_NUM,
    C.HYBRID_POD_NPU_NUM,
}
_DEFAULT_KVS_MASTER_SERVICE = "mindie-motor-kvs-master"


def read_json(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_json_by_path(data, path, default=None):
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
            if current is None:
                return default
        else:
            return default
    return current


def get_deploy_node_port(deploy_config, config_key, default=None):
    node_port = deploy_config.get(config_key, default)
    if node_port is None:
        return default
    if isinstance(node_port, str):
        node_port = node_port.strip()
        if node_port == "-":
            return None
        if not node_port:
            return default
    try:
        return int(node_port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{C.MOTOR_DEPLOY_CONFIG}.{config_key} must be an integer or '-'") from exc


def resolve_model_name(engine_section, default="Unknown"):
    engine_config = engine_section.get("engine_config", {})
    engine_type = engine_section.get("engine_type", "vllm")
    if engine_type == "sglang":
        name = engine_config.get("served-model-name")
    else:
        name = engine_config.get("served_model_name")
    if name:
        return name
    model_config = engine_section.get("model_config", {})
    return model_config.get("model_name", default)


def shell_escape(value):
    if not isinstance(value, str):
        return str(value)
    value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    value = value.replace("`", "\\`")
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "\\r")
    value = value.replace("\t", "\\t")
    return value


def update_shell_safely(script_path, env_config, component_key="", function_name="set_common_env"):
    all_env_vars = {}
    all_env_vars.update(env_config[C.MOTOR_COMMON_ENV])
    if component_key and component_key in env_config:
        all_env_vars.update(env_config[component_key])

    with open(script_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    start_idx, end_idx = -1, -1
    for i, line in enumerate(lines):
        if line.strip().startswith(f"function {function_name}()"):
            start_idx = i
        elif start_idx != -1 and line.strip() == "}":
            end_idx = i
            break

    new_function_lines = [
        f"function {function_name}() {{\n",
        *[
            f'    export {key}="{shell_escape(value)}"\n' if isinstance(value, str) else f"    export {key}={value}\n"
            for key, value in all_env_vars.items()
        ],
        "}\n",
    ]

    if start_idx != -1 and end_idx != -1:
        new_lines = lines[:start_idx] + new_function_lines + lines[end_idx + 1 :]
    else:
        new_lines = new_function_lines + lines

    with open(script_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def resolve_config_paths(config_dir, user_config_path, env_config_path):
    if not config_dir and not user_config_path and not env_config_path:
        logger.error("No configuration provided. Please use one of the following options:")
        logger.error("  --config_dir <dir>     : Directory containing user_config.json and env.json")
        logger.error("  --config <file>        : Path to user_config.json (requires --env)")
        logger.error("  --env <file>           : Path to env.json (requires --config)")
        raise ValueError("Missing required configuration. Use --config_dir or both --config and --env.")

    if config_dir and (user_config_path or env_config_path):
        raise ValueError(
            "Use either --config_dir, or both --config and --env; "
            "do not pass --config_dir together with --config/--env."
        )

    if config_dir:
        dir_user_config = os.path.join(config_dir, "user_config.json")
        dir_env_config = os.path.join(config_dir, "env.json")

        if not user_config_path:
            if os.path.exists(dir_user_config):
                user_config_path = dir_user_config
                logger.info("Using user_config.json from config_dir: %s", user_config_path)
            else:
                logger.error("user_config.json not found in %s", config_dir)
                raise FileNotFoundError(f"user_config.json not found in {config_dir}")

        if not env_config_path:
            if os.path.exists(dir_env_config):
                env_config_path = dir_env_config
                logger.info("Using env.json from config_dir: %s", env_config_path)
            else:
                logger.error("env.json not found in %s", config_dir)
                raise FileNotFoundError(f"env.json not found in {config_dir}")

    if user_config_path and not env_config_path:
        logger.error("--config is specified but --env is missing")
        raise ValueError("Both --config and --env must be specified together, or use --config_dir")

    if env_config_path and not user_config_path:
        logger.error("--env is specified but --config is missing")
        raise ValueError("Both --config and --env must be specified together, or use --config_dir")

    logger.info("%sUser config path: %s%s", C.GREEN, user_config_path, C.RESET)
    logger.info("%sEnv config path: %s%s", C.GREEN, env_config_path, C.RESET)
    return user_config_path, env_config_path


def validate_pd_hybrid_config(user_config):
    deploy_config = user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
    if not isinstance(deploy_config, dict):
        raise ValueError("motor_deploy_config is required for PD hybrid.")

    missing_keys = _PD_HYBRID_REQUIRED_DEPLOY_KEYS - deploy_config.keys()
    if missing_keys:
        raise ValueError(f"PD hybrid config missing required keys: {sorted(missing_keys)}")

    mixed_deploy_keys = _PD_SEPARATION_DEPLOY_KEYS & deploy_config.keys()
    if mixed_deploy_keys:
        raise ValueError(f"PD hybrid config cannot include separation keys: {sorted(mixed_deploy_keys)}")

    if "engine_topology" in deploy_config:
        raise ValueError("PD hybrid config must not include motor_deploy_config.engine_topology.")

    if C.MOTOR_ENGINE_UNION_CONFIG not in user_config:
        raise ValueError("PD hybrid config requires motor_engine_union_config.")
    if C.MOTOR_ENGINE_PREFILL_CONFIG in user_config or C.MOTOR_ENGINE_DECODE_CONFIG in user_config:
        raise ValueError("PD hybrid config cannot include prefill/decode engine config sections.")


def _obtain_engine_instance_total(deploy_config):
    if C.HYBRID_INSTANCES_NUM in deploy_config:
        try:
            hybrid_instances = int(deploy_config[C.HYBRID_INSTANCES_NUM])
        except (TypeError, ValueError) as e:
            raise ValueError(f"{C.HYBRID_INSTANCES_NUM} must be an integer") from e
        return hybrid_instances, 0

    if C.P_INSTANCES_NUM not in deploy_config:
        raise KeyError(f"{C.P_INSTANCES_NUM} is required in motor_deploy_config")
    if C.D_INSTANCES_NUM not in deploy_config:
        raise KeyError(f"{C.D_INSTANCES_NUM} is required in motor_deploy_config")
    try:
        p_instances = int(deploy_config[C.P_INSTANCES_NUM])
        d_instances = int(deploy_config[C.D_INSTANCES_NUM])
    except (TypeError, ValueError) as e:
        raise ValueError(f"{C.P_INSTANCES_NUM} and {C.D_INSTANCES_NUM} must be integers") from e
    return p_instances, d_instances


def validate_instance_nums(user_config):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    if C.HYBRID_INSTANCES_NUM in deploy_config:
        hybrid_total, _ = _obtain_engine_instance_total(deploy_config)
        if hybrid_total <= C.INSTANCE_NUM_ZERO:
            raise ValueError(f"{C.HYBRID_INSTANCES_NUM} must be greater than {C.INSTANCE_NUM_ZERO}")
        if hybrid_total > C.INSTANCE_NUM_MAX:
            raise ValueError(f"{C.HYBRID_INSTANCES_NUM} must not exceed {C.INSTANCE_NUM_MAX}")
        return

    p_total, d_total = _obtain_engine_instance_total(deploy_config)
    if p_total <= C.INSTANCE_NUM_ZERO:
        raise ValueError(f"{C.P_INSTANCES_NUM} must be greater than {C.INSTANCE_NUM_ZERO}")
    if p_total > C.INSTANCE_NUM_MAX:
        raise ValueError(f"{C.P_INSTANCES_NUM} must not exceed {C.INSTANCE_NUM_MAX}")
    if d_total <= C.INSTANCE_NUM_ZERO:
        raise ValueError(f"{C.D_INSTANCES_NUM} must be greater than {C.INSTANCE_NUM_ZERO}")
    if d_total > C.INSTANCE_NUM_MAX:
        raise ValueError(f"{C.D_INSTANCES_NUM} must not exceed {C.INSTANCE_NUM_MAX}")


def kv_store_enabled(user_config: dict) -> bool:
    engine_section = user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG) or user_config.get(C.MOTOR_ENGINE_UNION_CONFIG, {})
    kv_connector = engine_section.get(C.ENGINE_CONFIG, {}).get(C.KV_TRANSFER_CONFIG, {}).get(C.KV_CONNECTOR, "")
    kv_store_cfg = user_config.get(C.KV_CACHE_STORE_CONFIG)
    return kv_connector == C.MULTI_CONNECTOR or (isinstance(kv_store_cfg, dict) and bool(kv_store_cfg))


def normalize_kv_cache_store_config(user_config):
    kv_config = user_config.get(C.KV_CACHE_STORE_CONFIG)
    if not isinstance(kv_config, dict):
        raise ValueError(f"Missing or invalid '{C.KV_CACHE_STORE_CONFIG}' in user config")
    if C.KV_CACHE_STORE_PORT not in kv_config:
        kv_config[C.KV_CACHE_STORE_PORT] = C.DEFAULT_KV_CACHE_STORE_PORT
    if C.KV_STORE_BACKEND not in kv_config:
        kv_config[C.KV_STORE_BACKEND] = C.DEFAULT_KV_STORE_BACKEND
    return kv_config


def gen_kv_store_env(kv_store_config):
    service_port = kv_store_config.get(C.KV_CACHE_STORE_PORT)
    backend = kv_store_config.get(C.KV_STORE_BACKEND, C.DEFAULT_KV_STORE_BACKEND)
    items = [
        {C.NAME: C.ENV_KVS_MASTER_SERVICE, C.VALUE: _DEFAULT_KVS_MASTER_SERVICE},
        {C.NAME: C.ENV_KV_STORE_BACKEND, C.VALUE: backend},
        {C.NAME: C.ENV_KV_CACHE_STORE_PORT, C.VALUE: str(service_port)},
    ]
    if backend == "mooncake":
        missing_keys = []
        if C.KV_STORE_EVICTION_HIGH_WATERMARK_RATIO not in kv_store_config:
            missing_keys.append(C.KV_STORE_EVICTION_HIGH_WATERMARK_RATIO)
        if C.KV_STORE_EVICTION_RATIO not in kv_store_config:
            missing_keys.append(C.KV_STORE_EVICTION_RATIO)
        if missing_keys:
            raise ValueError(
                f"Missing required kv cache pool config: {missing_keys}. "
                f"Please configure them in '{C.KV_CACHE_STORE_CONFIG}'."
            )
        lease_ttl = kv_store_config.get(C.DEFAULT_KV_LEASE_TTL, 11000)
        items.append(
            {
                C.NAME: C.ENV_KV_STORE_EVICTION_HIGH_WATERMARK_RATIO,
                C.VALUE: str(kv_store_config[C.KV_STORE_EVICTION_HIGH_WATERMARK_RATIO]),
            }
        )
        items.append({C.NAME: C.ENV_KV_STORE_EVICTION_RATIO, C.VALUE: str(kv_store_config[C.KV_STORE_EVICTION_RATIO])})
        items.append({C.NAME: C.ENV_DEFAULT_KV_LEASE_TTL, C.VALUE: str(lease_ttl)})
    elif backend == C.MMC_STORE_BACKEND:
        mmc_config_store_port = kv_store_config.get(C.MMC_CONFIG_STORE_PORT_KEY, C.DEFAULT_MMC_CONFIG_STORE_PORT)
        mmc_metrics_port = kv_store_config.get(C.MMC_METRICS_PORT_KEY, C.DEFAULT_MMC_METRICS_PORT)
        items.append({C.NAME: C.ENV_MMC_CONFIG_STORE_URL, C.VALUE: f"tcp://0.0.0.0:{mmc_config_store_port}"})
        items.append({C.NAME: C.ENV_MMC_METRICS_URL, C.VALUE: f"http://0.0.0.0:{mmc_metrics_port}"})
    return items


CONTAINER_CONFIG_PATH = "/usr/local/Ascend/pyMotor/conf"

_API_CONFIG = "api_config"
_COORD_INFER_PORT = "coordinator_api_infer_port"
_COORD_OBS_PORT = "coordinator_obs_port"
DEFAULT_COORD_INFER_PORT = 1025
DEFAULT_COORD_OBS_PORT = 1027
DEFAULT_INFER_NODE_PORT = 31015
DEFAULT_OBS_NODE_PORT = 31017


@dataclass
class DockerDeployParams:
    image: str
    weight_mount_path: str
    hardware_type: str
    is_a5: bool
    required_npu_num: int
    infer_container_port: int
    obs_container_port: int
    infer_host_port: int
    obs_host_port: int
    deploy_mode: str
    kv_store_enabled: bool
    kv_store_env: dict = field(default_factory=dict)
    dshm_size: str | None = None


def _known_hardware_types() -> list[str]:
    return [*sorted(C.HARDWARE_TYPE_A2), *sorted(C.HARDWARE_TYPE_A3), *C.HARDWARE_TYPE_950I_A5]


def _is_a5(hardware_type: str) -> bool:
    if hardware_type in C.HARDWARE_TYPE_950I_A5:
        return True
    if hardware_type in C.HARDWARE_TYPE_A2 or hardware_type in C.HARDWARE_TYPE_A3:
        return False
    raise ValueError(f"Unknown hardware_type '{hardware_type}'. Supported values: {_known_hardware_types()}")


def npu_docker_card_count(hardware_type: str) -> int:
    """Host davinci nodes for a pure-docker create: A3=16, A2/A5=8."""
    _is_a5(hardware_type)
    if hardware_type in C.HARDWARE_TYPE_A3:
        return 16
    return 8


def _engine_config_normalized(engine_section: dict) -> dict:
    raw = engine_section.get(C.ENGINE_CONFIG) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key).replace("-", "_"): value for key, value in raw.items()}


def _engine_world_size(engine_section: dict) -> int:
    """Match NodeManager: world_size = dp * pcp * tp * pp (defaults 1)."""
    cfg = _engine_config_normalized(engine_section or {})
    engine_type = str((engine_section or {}).get(C.ENGINE_TYPE, "")).strip().lower()
    if engine_type == C.ENGINE_TYPE_SGLANG:
        dp = int(cfg.get("dp_size") or cfg.get("data_parallel_size") or 1)
        tp = int(cfg.get("tp_size") or cfg.get("tensor_parallel_size") or 1)
        pp = int(cfg.get("pp_size") or cfg.get("pipeline_parallel_size") or 1)
        pcp = 1
        if cfg.get("enable_prefill_context_parallel") and cfg.get("context_parallel_size"):
            pcp = int(cfg["context_parallel_size"])
        if cfg.get("enable_dp_attention"):
            return max(1, tp * pp * pcp)
        return max(1, dp * tp * pp * pcp)
    dp = int(cfg.get("data_parallel_size") or cfg.get("dp_size") or 1)
    tp = int(cfg.get("tensor_parallel_size") or cfg.get("tp_size") or 1)
    pp = int(cfg.get("pipeline_parallel_size") or cfg.get("pp_size") or 1)
    pcp = int(cfg.get("prefill_context_parallel_size") or 1)
    return max(1, dp * pcp * tp * pp)


def required_visible_device_count(user_config: dict) -> int:
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    if C.HYBRID_INSTANCES_NUM in deploy_config:
        instances = int(deploy_config[C.HYBRID_INSTANCES_NUM])
        union_section = user_config.get(C.MOTOR_ENGINE_UNION_CONFIG) or {}
        return instances * _engine_world_size(union_section)
    p_instances = int(deploy_config[C.P_INSTANCES_NUM])
    d_instances = int(deploy_config[C.D_INSTANCES_NUM])
    prefill_section = user_config[C.MOTOR_ENGINE_PREFILL_CONFIG]
    decode_section = user_config[C.MOTOR_ENGINE_DECODE_CONFIG]
    return p_instances * _engine_world_size(prefill_section) + d_instances * _engine_world_size(decode_section)


def parse_visible_device_ids(raw: str) -> list[int]:
    parts = [part.strip() for part in raw.split(",")]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("ASCEND_VISIBLE_DEVICES is empty")
    ids: list[int] = []
    seen: set[int] = set()
    for part in parts:
        try:
            device_id = int(part, 10)
        except ValueError as exc:
            raise ValueError(f"invalid device id '{part}' (expected non-negative integers)") from exc
        if device_id < 0:
            raise ValueError(f"invalid device id '{part}' (expected non-negative integers)")
        if device_id in seen:
            raise ValueError(f"duplicate device id {device_id}")
        seen.add(device_id)
        ids.append(device_id)
    return ids


def _host_port(deploy_config: dict, config_key: str, default: int) -> int:
    port = get_deploy_node_port(deploy_config, config_key, default=default)
    if port is None:
        logger.info("%s is '-', using default host port %s", config_key, default)
        return default
    return int(port)


def _derive_kv_store_env(user_config: dict) -> tuple[bool, dict]:
    if not kv_store_enabled(user_config):
        return False, {
            C.ENV_KV_STORE_BACKEND: "",
            C.ENV_KVS_MASTER_SERVICE: "",
        }

    kv_store_config = normalize_kv_cache_store_config(user_config)
    env_items = gen_kv_store_env(kv_store_config)
    env_dict = {item[C.NAME]: item[C.VALUE] for item in env_items}
    return True, env_dict


_K8S_QUANTITY_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)(?P<unit>[KMGTPE]i?)$", re.IGNORECASE)
_IEC_MULTIPLIER = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
}
_SI_MULTIPLIER = {
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
}


def k8s_quantity_to_docker_shm(size: str) -> str:
    raw = (size or "").strip()
    match = _K8S_QUANTITY_RE.fullmatch(raw)
    if not match:
        raise ValueError(
            f"'{C.DSHM_SIZE}' = {size!r} is not a Kubernetes quantity with a unit; use a value like '4Gi' or '512Mi'."
        )
    num_s, unit = match.group("num"), match.group("unit")
    if unit[-1].lower() == "i":
        multiplier = _IEC_MULTIPLIER[unit[0].upper() + "i"]
    else:
        multiplier = _SI_MULTIPLIER[unit.upper()]
    nbytes = int(float(num_s) * multiplier)
    if nbytes <= 0:
        raise ValueError(f"'{C.DSHM_SIZE}' = {size!r} must be a positive size.")
    for docker_unit, factor in (("g", 1024**3), ("m", 1024**2), ("k", 1024)):
        if nbytes % factor == 0:
            return f"{nbytes // factor}{docker_unit}"
    return f"{nbytes}b"


def _derive_dshm_size(deploy_config: dict) -> str | None:
    dshm_size = deploy_config.get(C.DSHM_SIZE)
    if dshm_size is None or dshm_size == "" or dshm_size is False:
        return None
    if dshm_size is True:
        raise ValueError(
            f"'{C.DSHM_SIZE}' = true is not a size; use a quantity like '4Gi', or "
            "false/omit to keep the docker-run template default."
        )
    if isinstance(dshm_size, (int, float)) or (
        isinstance(dshm_size, str) and re.fullmatch(r"\d+(\.\d+)?", dshm_size.strip())
    ):
        raise ValueError(
            f"'{C.DSHM_SIZE}' = '{dshm_size}' has no unit; did you mean '{dshm_size}Gi'? Use a quantity like '4Gi'."
        )
    if not isinstance(dshm_size, str):
        raise ValueError(f"'{C.DSHM_SIZE}' = {dshm_size!r} must be a string quantity like '4Gi'.")
    k8s_quantity_to_docker_shm(dshm_size)
    return dshm_size.strip()


def derive_params(user_config: dict) -> DockerDeployParams:
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]

    image = deploy_config[C.IMAGE_NAME]
    weight_mount_path = str(deploy_config.get(C.WEIGHT_MOUNT_PATH) or "").strip()
    hardware_type = deploy_config[C.HARDWARE_TYPE]
    is_a5 = _is_a5(hardware_type)
    # 计算需要使用的NPU数量
    required_npu_num = required_visible_device_count(user_config)

    coord_api = get_json_by_path(user_config, f"{C.MOTOR_COORDINATOR_CONFIG}.{_API_CONFIG}", {}) or {}
    infer_container_port = int(coord_api.get(_COORD_INFER_PORT, DEFAULT_COORD_INFER_PORT))
    obs_container_port = int(coord_api.get(_COORD_OBS_PORT, DEFAULT_COORD_OBS_PORT))
    infer_host_port = _host_port(deploy_config, C.COORDINATOR_INFER_NODE_PORT, DEFAULT_INFER_NODE_PORT)
    obs_host_port = _host_port(deploy_config, C.COORDINATOR_OBS_NODE_PORT, DEFAULT_OBS_NODE_PORT)

    deploy_mode = deploy_config.get(C.DEPLOY_MODE_CONFIG_KEY, "")
    kv_store_enabled, kv_store_env = _derive_kv_store_env(user_config)
    dshm_size = _derive_dshm_size(deploy_config)

    return DockerDeployParams(
        image=image,
        weight_mount_path=weight_mount_path,
        hardware_type=hardware_type,
        is_a5=is_a5,
        required_npu_num=required_npu_num,
        infer_container_port=infer_container_port,
        obs_container_port=obs_container_port,
        infer_host_port=infer_host_port,
        obs_host_port=obs_host_port,
        deploy_mode=deploy_mode,
        kv_store_enabled=kv_store_enabled,
        kv_store_env=kv_store_env,
        dshm_size=dshm_size,
    )


def create_env_from_config(user_config_path: str, deployer_dir: str) -> dict[str, str]:
    user_config_path = os.path.abspath(os.path.expanduser((user_config_path or "").strip()))
    if not os.path.isfile(user_config_path):
        raise ValueError(f"user_config.json not found: {user_config_path}")
    with open(user_config_path, encoding="utf-8") as handle:
        user_config = json.load(handle)
    if not isinstance(user_config, dict):
        raise ValueError(f"{user_config_path} must be a JSON object.")
    deploy = user_config.get(C.MOTOR_DEPLOY_CONFIG)
    if not isinstance(deploy, dict):
        raise ValueError(f"{user_config_path} is missing motor_deploy_config.")
    image = str(deploy.get(C.IMAGE_NAME) or "").strip()
    if not image:
        raise ValueError("Set motor_deploy_config.image_name in user_config.json.")
    env = {
        "EXAMPLES": os.path.dirname(os.path.abspath(deployer_dir)),
        "IMAGE": image,
    }
    weight = str(deploy.get(C.WEIGHT_MOUNT_PATH) or "").strip()
    if weight:
        env["WEIGHT"] = weight
    return env


DOCKER_MULTI_ROLES = (
    "coordinator",
    "controller",
    "coordinator_controller",
    "prefill",
    "decode",
    "union",
    "kv_store",
)
ENGINE_ROLES = ("prefill", "decode", "union")
CONTROL_ROLES = ("coordinator", "controller", "coordinator_controller")
ROLE_COORDINATOR_CONTROLLER = "coordinator_controller"
_ENGINE_SECTION = {
    "prefill": C.MOTOR_ENGINE_PREFILL_CONFIG,
    "decode": C.MOTOR_ENGINE_DECODE_CONFIG,
    "union": C.MOTOR_ENGINE_UNION_CONFIG,
}
_ROLE_ALIASES = {
    "coordinator,controller": ROLE_COORDINATOR_CONTROLLER,
    "controller,coordinator": ROLE_COORDINATOR_CONTROLLER,
}


_CONTROL_ENGINE_SPLIT = (
    "Control-plane and engine cannot share a container. "
    "Use --role coordinator,controller (or coordinator / controller) in one container, "
    "and --role prefill/decode/union in another."
)


def split_docker_role(role: str | None) -> tuple[str | None, str | None]:
    if not role:
        return None, None
    if role in CONTROL_ROLES:
        return role, None
    return None, role


def engine_role_of(role: str | None) -> str | None:
    _, engine = split_docker_role(role)
    return engine if engine in ENGINE_ROLES else None


def control_role_of(role: str | None) -> str | None:
    control, _ = split_docker_role(role)
    return control if control in CONTROL_ROLES else None


def is_supported_docker_role(role: str | None) -> bool:
    return bool(role) and role in DOCKER_MULTI_ROLES


def _compose_role_from_tokens(tokens: list[str]) -> str:
    control: list[str] = []
    others: list[str] = []
    for token in tokens:
        mapped = _ROLE_ALIASES.get(token, token)
        if mapped == ROLE_COORDINATOR_CONTROLLER:
            control.extend(["coordinator", "controller"])
        elif mapped in ("coordinator", "controller"):
            control.append(mapped)
        elif mapped in ENGINE_ROLES or mapped == "kv_store":
            others.append(mapped)
        else:
            raise ValueError(
                f"Unknown --role '{token}'. Valid: {', '.join(DOCKER_MULTI_ROLES)}. "
                "coordinator,controller is an alias for coordinator_controller."
            )
    control = list(dict.fromkeys(control))
    others = list(dict.fromkeys(others))
    if "kv_store" in others and (control or len(others) > 1):
        raise ValueError("kv_store cannot be combined with other --role values.")
    if len(others) > 1:
        raise ValueError("Use one of prefill, decode, union, or kv_store per container.")
    control_role = None
    if len(control) == 2:
        control_role = ROLE_COORDINATOR_CONTROLLER
    elif len(control) == 1:
        control_role = control[0]
    other_role = others[0] if others else None
    if not control_role and not other_role:
        raise ValueError("Empty --role.")
    if control_role and other_role:
        raise ValueError(_CONTROL_ENGINE_SPLIT)
    return control_role or other_role


def normalize_docker_role(role: str | None) -> str | None:
    if role is None:
        return None
    compact = "".join(str(role).split())
    if not compact:
        return None
    if compact in _ROLE_ALIASES:
        return _ROLE_ALIASES[compact]
    if compact in DOCKER_MULTI_ROLES:
        return compact
    if "+" in compact:
        raise ValueError(_CONTROL_ENGINE_SPLIT)
    return _compose_role_from_tokens([token for token in compact.split(",") if token])


@dataclass
class DockerRuntimeIdentity:
    role: str
    job_name: str
    pod_ip: str
    coordinator_ip: str
    controller_ip: str
    kv_store_ip: str
    host_network: bool
    attach_npu: bool
    nic_name: str = ""


@dataclass
class EnginePortOverrides:
    node_manager_port: int | None = None
    data_parallel_rpc_port: int | None = None
    kv_port: int | None = None
    base_port: int | None = None

    def specified(self) -> list[str]:
        flags = []
        if self.node_manager_port is not None:
            flags.append("--node-manager-port")
        if self.data_parallel_rpc_port is not None:
            flags.append("--data-parallel-rpc-port")
        if self.kv_port is not None:
            flags.append("--kv-port")
        if self.base_port is not None:
            flags.append("--base-port")
        return flags


def ip_from_nic(nic: str) -> str:
    name = (nic or "").strip()
    if not name or any(ch in name for ch in "/ \\"):
        raise ValueError(f"Invalid --nic-name {nic!r}.")
    try:
        import fcntl
        import struct

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            packed = struct.pack("256s", name.encode("utf-8")[:15])
            info = fcntl.ioctl(sock.fileno(), 0x8915, packed)
        finally:
            sock.close()
        ip = socket.inet_ntoa(info[20:24])
    except (OSError, ImportError, ValueError) as exc:
        raise ValueError(f"Failed to get IPv4 for --nic-name {name!r}.") from exc
    if not ip or ip.startswith("127."):
        raise ValueError(f"--nic-name {name!r} resolved to loopback {ip!r}.")
    return ip


def _canon_ipv4(text: str, flag: str) -> str:
    try:
        return socket.inet_ntoa(socket.inet_aton(text))
    except OSError as exc:
        raise ValueError(f"{flag} {text!r} is not a valid IPv4 address.") from exc


def _require_ip(value: str | None, flag: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{flag} is required.")
    return text


def require_pod_ip_and_nic(pod_ip: str | None, nic_name: str | None) -> tuple[str, str]:
    missing = []
    ip = (pod_ip or "").strip()
    nic = (nic_name or "").strip()
    if not ip:
        missing.append("--pod-ip")
    if not nic:
        missing.append("--nic-name")
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        raise ValueError(f"{' and '.join(missing)} {verb} required.")
    if any(ch in nic for ch in "/ \\"):
        raise ValueError(f"Invalid --nic-name {nic!r}.")
    ip = _canon_ipv4(ip, "--pod-ip")
    if ip.startswith("127."):
        raise ValueError(f"--pod-ip {ip!r} is loopback.")
    nic_ip = ip_from_nic(nic)
    if nic_ip != ip:
        raise ValueError(f"--pod-ip {ip!r} is not the IPv4 of --nic-name {nic!r} ({nic_ip}).")
    return ip, nic


def resolve_runtime_identity(
    user_config: dict,
    *,
    role: str | None,
    job_name: str | None,
    pod_ip: str | None,
    coordinator_ip: str | None,
    controller_ip: str | None,
    kv_store_ip: str | None,
    kv_store_enabled: bool,
    nic_name: str | None = None,
) -> DockerRuntimeIdentity | None:
    resolved_pod_ip, resolved_nic = require_pod_ip_and_nic(pod_ip, nic_name)
    if not role:
        extra = [
            ("--instance-name", job_name),
            ("--coordinator-ip", coordinator_ip),
            ("--controller-ip", controller_ip),
            ("--kv-store-ip", kv_store_ip),
        ]
        unexpected = [flag for flag, value in extra if value]
        if unexpected:
            raise ValueError(f"{', '.join(unexpected)} is only valid together with --role.")
        return None

    role = normalize_docker_role(role)
    if not is_supported_docker_role(role):
        raise ValueError(f"Unknown --role '{role}'. Valid: {', '.join(DOCKER_MULTI_ROLES)}.")
    resolved_job = (job_name or "").strip()
    resolved_coord = (coordinator_ip or "").strip()
    resolved_ctrl = (controller_ip or "").strip()
    resolved_kvs = (kv_store_ip or "").strip()
    control, engine = split_docker_role(role)
    has_engine = engine in ENGINE_ROLES

    if has_engine:
        if not resolved_job:
            raise ValueError(f"--instance-name is required for --role {role}.")
    elif resolved_job:
        raise ValueError(f"--instance-name is not used for --role {role}.")

    if control == "coordinator":
        resolved_coord = resolved_coord or resolved_pod_ip
        resolved_ctrl = _require_ip(resolved_ctrl, "--controller-ip")
    elif control == "controller":
        resolved_ctrl = resolved_ctrl or resolved_pod_ip
        resolved_coord = _require_ip(resolved_coord, "--coordinator-ip")
    elif control == ROLE_COORDINATOR_CONTROLLER:
        resolved_coord = resolved_coord or resolved_pod_ip
        resolved_ctrl = resolved_ctrl or resolved_pod_ip
    else:
        resolved_coord = _require_ip(resolved_coord, "--coordinator-ip")
        resolved_ctrl = _require_ip(resolved_ctrl, "--controller-ip")

    if kv_store_enabled:
        if engine == "kv_store":
            resolved_kvs = resolved_kvs or resolved_pod_ip
        else:
            resolved_kvs = _require_ip(resolved_kvs, "--kv-store-ip")
    elif resolved_kvs:
        raise ValueError("--kv-store-ip was set but KV Cache Store is disabled in user_config.")

    return DockerRuntimeIdentity(
        role=role,
        job_name=resolved_job,
        pod_ip=resolved_pod_ip,
        coordinator_ip=resolved_coord,
        controller_ip=resolved_ctrl,
        kv_store_ip=resolved_kvs,
        host_network=True,
        attach_npu=has_engine,
        nic_name=resolved_nic,
    )


def engine_port_overrides_from_args(args) -> EnginePortOverrides:
    return EnginePortOverrides(
        node_manager_port=getattr(args, "node_manager_port", None),
        data_parallel_rpc_port=getattr(args, "data_parallel_rpc_port", None),
        kv_port=getattr(args, "kv_port", None),
        base_port=getattr(args, "base_port", None),
    )


# 如果传入了端口避让，那必须是运行推理引擎，否则报错
def validate_engine_port_overrides(identity: DockerRuntimeIdentity | None, overrides: EnginePortOverrides) -> None:
    flags = overrides.specified()
    if not flags:
        return
    if identity is None or engine_role_of(identity.role) not in ENGINE_ROLES:
        raise ValueError(f"{', '.join(flags)} is only valid with --role prefill, decode, or union.")


def _tcp_port(value: int, flag: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if value < minimum or value > 65535:
        lo = "0" if allow_zero else "1"
        raise ValueError(f"{flag} must be in range {lo}-65535 (got {value}).")
    return value


def _set_preserving_type(mapping: dict, key: str, port: int) -> None:
    existing = mapping.get(key)
    mapping[key] = str(port) if isinstance(existing, str) else port


def _ensure_dict(parent: dict, key: str) -> dict:
    current = parent.get(key)
    if not isinstance(current, dict):
        current = {}
        parent[key] = current
    return current


def _apply_kv_port(engine_config: dict, port: int) -> None:
    kv = engine_config.get(C.KV_TRANSFER_CONFIG)
    if not isinstance(kv, dict):
        raise ValueError("--kv-port requires engine_config.kv_transfer_config in user_config.json.")
    extra = kv.get("kv_connector_extra_config")
    connectors = extra.get("connectors") if isinstance(extra, dict) else None
    connector0 = (
        connectors[0] if isinstance(connectors, list) and connectors and isinstance(connectors[0], dict) else None
    )
    existing = kv.get("kv_port")
    if existing is None and connector0 is not None:
        existing = connector0.get("kv_port")
    kv["kv_port"] = str(port) if isinstance(existing, str) else port
    if connector0 is not None:
        _set_preserving_type(connector0, "kv_port", port)


def apply_engine_port_overrides(user_config: dict, role: str, overrides: EnginePortOverrides) -> None:
    if not overrides.specified():
        return
    section_key = _ENGINE_SECTION.get(engine_role_of(role) or role)
    if not section_key:
        raise ValueError(f"--role {role} has no engine section for port overrides.")
    section = user_config.get(section_key)
    if not isinstance(section, dict):
        raise ValueError(f"{section_key} is missing; cannot apply engine port overrides.")

    if overrides.node_manager_port is not None:
        port = _tcp_port(overrides.node_manager_port, "--node-manager-port")
        nm = _ensure_dict(section, C.MOTOR_NODEMANAGER_CONFIG)
        api = _ensure_dict(nm, "api_config")
        api["node_manager_port"] = port

    if overrides.base_port is not None:
        port = _tcp_port(overrides.base_port, "--base-port", allow_zero=True)
        nm = _ensure_dict(section, C.MOTOR_NODEMANAGER_CONFIG)
        endpoint = _ensure_dict(nm, "endpoint_config")
        endpoint["base_port"] = port

    if overrides.data_parallel_rpc_port is not None:
        port = _tcp_port(overrides.data_parallel_rpc_port, "--data-parallel-rpc-port")
        engine_config = _ensure_dict(section, C.ENGINE_CONFIG)
        engine_config["data_parallel_rpc_port"] = port
        if "data-parallel-rpc-port" in engine_config:
            engine_config["data-parallel-rpc-port"] = port

    if overrides.kv_port is not None:
        port = _tcp_port(overrides.kv_port, "--kv-port")
        engine_config = _ensure_dict(section, C.ENGINE_CONFIG)
        _apply_kv_port(engine_config, port)


_DEFAULT_NODE_MANAGER_PORT = 1026
_DEFAULT_BASE_PORT = 10000
_DEFAULT_DP_RPC_PORT = 9000


def _host_listen_port(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if port < 1 or port > 65535:
        return None
    return port


def _read_kv_port(engine_config: dict) -> int | None:
    kv = engine_config.get(C.KV_TRANSFER_CONFIG)
    if not isinstance(kv, dict):
        return None
    extra = kv.get("kv_connector_extra_config")
    connectors = extra.get("connectors") if isinstance(extra, dict) else None
    connector0 = (
        connectors[0] if isinstance(connectors, list) and connectors and isinstance(connectors[0], dict) else None
    )
    value = kv.get("kv_port")
    if value is None and connector0 is not None:
        value = connector0.get("kv_port")
    return _host_listen_port(value)


def _enable_multi_endpoints(section: dict) -> bool:
    if "enable_multi_endpoints" in section:
        return bool(section["enable_multi_endpoints"])
    cfg = section.get(C.ENGINE_CONFIG)
    if isinstance(cfg, dict) and "enable_multi_endpoints" in cfg:
        return bool(cfg["enable_multi_endpoints"])
    return True


def _pod_npu_num(user_config: dict, engine_role: str) -> int:
    deploy = user_config.get(C.MOTOR_DEPLOY_CONFIG) or {}
    key = {
        "prefill": C.P_POD_NPU_NUM,
        "decode": C.D_POD_NPU_NUM,
        "union": C.HYBRID_POD_NPU_NUM,
    }.get(engine_role)
    if not key:
        return 0
    try:
        n = int(deploy.get(key) or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _engine_section(user_config: dict, role: str | None) -> dict | None:
    engine_role = engine_role_of(role) or role
    section_key = _ENGINE_SECTION.get(engine_role) if engine_role else None
    section = user_config.get(section_key) if section_key else None
    return section if isinstance(section, dict) else None


def instance_world_size(user_config: dict, role: str | None) -> int:
    if role and engine_role_of(role):
        section = _engine_section(user_config, role)
        if section is None:
            raise ValueError(f"--role {role} has no engine section to derive world_size.")
        return max(1, _engine_world_size(section))
    return required_visible_device_count(user_config)


def _engine_nnodes(section: dict) -> int:
    cfg = _engine_config_normalized(section)
    try:
        n = int(cfg.get("nnodes") or 1)
    except (TypeError, ValueError):
        n = 1
    return n if n > 0 else 1


def instance_local_npu_count(user_config: dict, role: str | None) -> int:
    """NPUs this container must attach (one node), not the full instance world_size.

    Matches NodeManager: nnodes=1 → world_size; nnodes>1 → world_size/nnodes
    (TP stays intra-node; PCP/PP may span). Same number K8s puts in *_pod_npu_num.
    """
    if not (role and engine_role_of(role)):
        return required_visible_device_count(user_config)
    section = _engine_section(user_config, role)
    if section is None:
        raise ValueError(f"--role {role} has no engine section to derive NPU count.")
    world = max(1, _engine_world_size(section))
    nnodes = _engine_nnodes(section)
    cfg = _engine_config_normalized(section)
    try:
        dp = int(cfg.get("data_parallel_size") or cfg.get("dp_size") or 1)
    except (TypeError, ValueError):
        dp = 1
    if nnodes > 1 and dp > 1:
        raise ValueError(f"--role {role}: nnodes>1 with data_parallel_size>1 is unsupported.")
    if nnodes > 1:
        if world % nnodes != 0:
            raise ValueError(f"--role {role}: world_size={world} is not divisible by nnodes={nnodes}.")
        expected = world // nnodes
    else:
        expected = world
    pod_npu = _pod_npu_num(user_config, engine_role_of(role))
    if pod_npu > 0 and pod_npu != expected:
        raise ValueError(
            f"--role {role}: pod NPU count is {pod_npu}, but this node needs {expected} "
            f"(world_size={world}, nnodes={nnodes})."
        )
    return expected


def _instance_endpoint_num(user_config: dict, section: dict, engine_role: str) -> int:
    """Match NodeManager._generate_endpoint_ports for this instance."""
    nm = section.get(C.MOTOR_NODEMANAGER_CONFIG)
    endpoint = nm.get("endpoint_config") if isinstance(nm, dict) else None
    if isinstance(endpoint, dict) and endpoint.get("endpoint_num") not in (None, ""):
        try:
            n = int(endpoint["endpoint_num"])
            if n > 0:
                return n
        except (TypeError, ValueError):
            pass
    if not _enable_multi_endpoints(section):
        return 1
    try:
        cfg = _engine_config_normalized(section)
        dp = max(1, int(cfg.get("data_parallel_size") or cfg.get("dp_size") or 1))
        tp = max(1, int(cfg.get("tensor_parallel_size") or cfg.get("tp_size") or 1))
        pp = max(1, int(cfg.get("pipeline_parallel_size") or cfg.get("pp_size") or 1))
        pcp = max(1, int(cfg.get("prefill_context_parallel_size") or 1))
        devices_per_dp = tp * pp * pcp
        device_num = _pod_npu_num(user_config, engine_role)
        if device_num <= 0:
            device_num = _engine_world_size(section)
    except (TypeError, ValueError):
        return 1
    if devices_per_dp < 1 or device_num < devices_per_dp:
        return 1
    return max(1, min(dp, device_num // devices_per_dp))


def collect_engine_listen_ports(user_config: dict, role: str) -> list[tuple[str, int]]:
    """Ports this engine instance binds on host network after CLI overrides.

    Aligns with NodeManager defaults/service_ports and vLLM-Ascend Mooncake
    handshake ports: kv_port .. kv_port+world_size-1.
    """
    engine_role = engine_role_of(role) or role
    section = _engine_section(user_config, role)
    if not isinstance(section, dict):
        return []

    ports: list[tuple[str, int]] = []
    seen: set[int] = set()

    def add(label: str, port: int | None) -> None:
        if port is None or port in seen:
            return
        seen.add(port)
        ports.append((label, port))

    nm = section.get(C.MOTOR_NODEMANAGER_CONFIG)
    api = nm.get("api_config") if isinstance(nm, dict) else None
    endpoint = nm.get("endpoint_config") if isinstance(nm, dict) else None
    engine_config = section.get(C.ENGINE_CONFIG)
    if not isinstance(engine_config, dict):
        engine_config = {}

    nm_port = _host_listen_port((api or {}).get("node_manager_port"))
    add("node-manager", nm_port if nm_port is not None else _DEFAULT_NODE_MANAGER_PORT)

    dp_rpc = engine_config.get("data_parallel_rpc_port")
    if dp_rpc is None:
        dp_rpc = engine_config.get("data-parallel-rpc-port")
    parsed_rpc = _host_listen_port(dp_rpc)
    add("data-parallel-rpc", parsed_rpc if parsed_rpc is not None else _DEFAULT_DP_RPC_PORT)

    base = _host_listen_port((endpoint or {}).get("base_port"))
    if base is None and (not isinstance(endpoint, dict) or "base_port" not in endpoint):
        base = _DEFAULT_BASE_PORT
    if base is not None:
        for i in range(_instance_endpoint_num(user_config, section, engine_role)):
            add(f"engine-service[{i}]", _host_listen_port(base + i * 2))
            add(f"engine-mgmt[{i}]", _host_listen_port(base + i * 2 + 1))

    kv_port = _read_kv_port(engine_config)
    if kv_port is not None:
        try:
            n_cards = max(1, _engine_world_size(section))
        except (TypeError, ValueError):
            n_cards = 1
        for i in range(n_cards):
            add(f"kv[{i}]", _host_listen_port(kv_port + i))

    return ports


def attached_npu_count(devices_arg: str | None, hardware_type: str, *, template_fallback: bool) -> int | None:
    raw = (devices_arg or "").strip() or (os.environ.get("ASCEND_VISIBLE_DEVICES") or "").strip()
    if raw:
        return len(parse_visible_device_ids(raw))
    if template_fallback:
        return npu_docker_card_count(hardware_type)
    return None


def validate_devices_vs_world_size(
    user_config: dict,
    role: str | None,
    *,
    devices_arg: str | None,
    hardware_type: str,
    template_fallback: bool,
) -> None:
    """Require attached NPUs to match this node's card count, not cluster P+D total."""
    if role and engine_role_of(role) is None:
        return
    expected = instance_local_npu_count(user_config, role)
    got = attached_npu_count(devices_arg, hardware_type, template_fallback=template_fallback)
    if got is None or got == expected:
        return
    if (devices_arg or "").strip():
        source = "--devices"
    elif (os.environ.get("ASCEND_VISIBLE_DEVICES") or "").strip():
        source = "ASCEND_VISIBLE_DEVICES"
    else:
        source = "docker-run NPU template"
    raise ValueError(
        f"{source} attaches {got} NPU(s), but this container needs {expected} "
        f"(this node: world_size/nnodes, or *_pod_npu_num; not cluster P+D total). "
        f"Pass --devices with exactly {expected} card(s)."
    )


_ROLE_ROOT_FILES = ["boot.sh", "common.sh", "hccl_tools.py"]
_KV_BACKEND_FILES = [
    ("roles/kv_store_backends/mooncake/mooncake.sh", "kv_store_backends.mooncake.mooncake.sh"),
    ("roles/kv_store_backends/mooncake/mooncake_config.py", "kv_store_backends.mooncake.mooncake_config.py"),
    ("roles/kv_store_backends/memcache/memcache.sh", "kv_store_backends.memcache.memcache.sh"),
    (
        "roles/kv_store_backends/memcache/memcache_meta_service.py",
        "kv_store_backends.memcache.memcache_meta_service.py",
    ),
    (
        "roles/kv_store_backends/memcache/mmc-local-inprocess.conf",
        "kv_store_backends.memcache.mmc-local-inprocess.conf",
    ),
]


def prepare_configmap(
    deployer_dir: str,
    configmap_path: str,
    user_config_path: str,
    env_config_path: str,
    *,
    role: str | None = None,
    engine_ports: EnginePortOverrides | None = None,
) -> None:
    startup_dir = os.path.join(deployer_dir, "startup")
    os.makedirs(configmap_path, exist_ok=True)

    for name in _ROLE_ROOT_FILES:
        shutil.copyfile(os.path.join(startup_dir, name), os.path.join(configmap_path, name))

    roles_dir = os.path.join(startup_dir, "roles")
    for name in os.listdir(roles_dir):
        src = os.path.join(roles_dir, name)
        if os.path.isfile(src) and name.endswith(".sh"):
            shutil.copyfile(src, os.path.join(configmap_path, name))

    for rel_src, dst_name in _KV_BACKEND_FILES:
        src = os.path.join(startup_dir, rel_src)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(configmap_path, dst_name))

    dest_user_config = os.path.join(configmap_path, "user_config.json")
    shutil.copyfile(user_config_path, dest_user_config)
    shutil.copyfile(env_config_path, os.path.join(configmap_path, "env.json"))

    if engine_ports and engine_ports.specified():
        with open(dest_user_config, encoding="utf-8") as handle:
            copied = json.load(handle)
        apply_engine_port_overrides(copied, role, engine_ports)
        with open(dest_user_config, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(copied, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    _run_set_env_docker(deployer_dir, configmap_path)
    logger.info("ConfigMap prepared at %s", configmap_path)


def _run_set_env_docker(deployer_dir: str, configmap_path: str) -> None:
    module_path = os.path.join(deployer_dir, "startup", "set_env_docker.py")
    spec = importlib.util.spec_from_file_location("set_env_docker", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.set_env_docker(configmap_path)


def render_start_motor_sh(
    params: DockerDeployParams,
    configmap_path: str,
    identity: DockerRuntimeIdentity | None = None,
    nic_name: str | None = None,
    pod_ip: str | None = None,
) -> str:
    role = identity.role if identity else C.ROLE_SINGLE_CONTAINER
    kv_env = dict(params.kv_store_env)
    if identity is not None and params.kv_store_enabled:
        kv_env[C.ENV_KVS_MASTER_SERVICE] = identity.kv_store_ip
    ip, nic = require_pod_ip_and_nic(
        identity.pod_ip if identity is not None else pod_ip,
        nic_name or (identity.nic_name if identity is not None else None),
    )
    lines = [
        "#!/bin/bash",
        "set -e",
        "",
        f'export CONFIGMAP_PATH="{configmap_path}"',
        f'export CONFIG_PATH="{CONTAINER_CONFIG_PATH}"',
        f"export ROLE={role}",
    ]
    if identity is not None:
        lines.extend(
            [
                f'export JOB_NAME="{identity.job_name}"',
                f'export POD_IP="{ip}"',
                f'export COORDINATOR_SERVICE="{identity.coordinator_ip}"',
                f'export CONTROLLER_SERVICE="{identity.controller_ip}"',
            ]
        )
    else:
        lines.append(f'export POD_IP="{ip}"')
    lines.extend(
        [
            f'export HCCL_IF_IP="{ip}"',
            f'export GLOO_SOCKET_IFNAME="{nic}"',
            f'export TP_SOCKET_IFNAME="{nic}"',
            f'export HCCL_SOCKET_IFNAME="{nic}"',
        ]
    )
    lines.extend(
        [
            "",
        ]
    )
    for key, value in kv_env.items():
        lines.append(f'export {key}="{value}"')
    lines.extend(
        [
            "",
            'mkdir -p "$CONFIG_PATH"',
            'source "$CONFIGMAP_PATH/boot.sh"',
            "",
        ]
    )
    return "\n".join(lines)


def infer_endpoint(params: DockerDeployParams, identity: DockerRuntimeIdentity | None = None) -> str:
    if identity is not None:
        return f"http://{identity.coordinator_ip}:{params.infer_container_port}/v1/chat/completions"
    return f"http://<host-ip>:{params.infer_host_port}/v1/chat/completions"


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


def describe_port_holder(port: int) -> str:
    for cmd in (["ss", "-ltnp"], ["netstat", "-ltnp"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
        except FileNotFoundError:
            continue
        for line in out.splitlines():
            if f":{port} " in line or f":{port}\t" in line:
                return line.strip()
    return ""


def _docker(*args: str, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B607
        ["docker", *args], capture_output=capture, text=True, check=check
    )


def docker_available() -> bool:
    try:
        return _docker("version", "--format", "{{.Server.Version}}").returncode == 0
    except FileNotFoundError:
        return False


def image_exists(image: str) -> bool:
    return _docker("image", "inspect", image).returncode == 0


def container_exists(name: str) -> bool:
    return _docker("inspect", name).returncode == 0
