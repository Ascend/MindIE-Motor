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
import logging
import os
import sys
import ipaddress
from typing import Any

ENCODING_UTF8 = "utf-8"

KV_POOL_CONFIG_KEY = "kv_cache_pool_config"
KV_CONDUCTOR_CONFIG_KEY = "kv_conductor_config"
MASTER_SERVER_ADDRESS = "master_server_address"
ENDPOINT_ADDRESS = "endpoint"
MODEL_NAME = "modelname"
MASTER_SERVER_PORT_KEY = "port"
DEFAULT_MASTER_SERVER_PORT = "50088"
KVEVENT_INSTANCE = "kvevent_instance"
MOONCAKE_MASTER = "mooncake_master"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def format_address(host: str, port) -> str:
    try:
        if isinstance(ipaddress.ip_address(host.strip("[]")), ipaddress.IPv6Address):
            return f"[{host.strip('[]')}]:{port}"
    except ValueError:
        pass
    return f"{host}:{port}"


def resolve_model_name(engine_section, default="Unknown"):
    """
    Resolve model_name from engine_config (native) or model_config (legacy).

    Checks engine_config first using engine-type-specific keys, then falls back
    to the deprecated model_config.
    """
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


def ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def read_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding=ENCODING_UTF8) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object at root, got: {type(data)}")
    return data


def write_json(path: str, data: dict[str, Any]) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding=ENCODING_UTF8) as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def generate_kv_cache_pool_config(output_path: str, user_config_path: str) -> bool:
    if not os.path.exists(user_config_path):
        logging.error("user_config.json does not exist: %s", user_config_path)
        return False

    user_cfg = read_json(user_config_path)
    kv_cfg = user_cfg.get(KV_POOL_CONFIG_KEY)
    if not kv_cfg:
        logging.error("KV cache pool config not provided, skipping kv_cache_pool_config generation")
        return False

    out_cfg: dict[str, Any] = dict(kv_cfg)

    kvp_master_service = os.getenv("KVP_MASTER_SERVICE", "")
    if not kvp_master_service:
        logging.error("Env KVP_MASTER_SERVICE is required but not set, cannot generate kv_cache_pool_config")
        return False
    master_server_port = kv_cfg.get(MASTER_SERVER_PORT_KEY, DEFAULT_MASTER_SERVER_PORT)
    out_cfg[MASTER_SERVER_ADDRESS] = format_address(kvp_master_service, master_server_port)

    write_json(output_path, out_cfg)
    logging.info("kv_cache_pool_config generated: %s", output_path)
    return True


def generate_kv_conductor_config(output_path: str, user_config_path: str) -> bool:
    if not os.path.exists(user_config_path):
        logging.error("user_config.json does not exist: %s", user_config_path)
        return False

    user_cfg = read_json(user_config_path)
    kv_cfg = user_cfg.get(KV_CONDUCTOR_CONFIG_KEY)
    if not kv_cfg:
        logging.error("KV cache conductor config not provided, skipping kv_conductor_config generation")
        return False

    logging.info("kv_conductor_config generated: %s", output_path)

    out_cfg: dict[str, Any] = dict(kv_cfg)
    kvevent_instance = out_cfg.get(KVEVENT_INSTANCE, None)
    if kvevent_instance is None:
        logging.info("KV cache conductor config kvevent_instance is None")
        write_json(output_path, out_cfg)
        return True

    kv_pool_cfg = user_cfg.get(KV_POOL_CONFIG_KEY)
    if not kv_pool_cfg:
        logging.error("KV cache pool config not provided, skipping kv_cache_pool_config generation")
        return False

    kvp_master_service = os.getenv("KVP_MASTER_SERVICE", "")
    if not kvp_master_service:
        logging.error("Env KVP_MASTER_SERVICE is required but not set, cannot generate kv_cache_pool_config")
        return False
    master_server_port = kv_pool_cfg.get(MASTER_SERVER_PORT_KEY, DEFAULT_MASTER_SERVER_PORT)
    mooncake_master = out_cfg[KVEVENT_INSTANCE][MOONCAKE_MASTER]
    mooncake_master[ENDPOINT_ADDRESS] = f"tcp://{format_address(kvp_master_service, master_server_port)}"
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    mooncake_master[MODEL_NAME] = resolve_model_name(user_cfg["motor_engine_prefill_config"])
    write_json(output_path, out_cfg)
    return True


def main() -> None:
    if len(sys.argv) < 4:
        logging.info("Usage: python3 mooncake_config.py <pool|conductor> <output_config_path> <user_config_path>")
        sys.exit(1)

    config_type = sys.argv[1]
    output_path = sys.argv[2]
    user_config_path = sys.argv[3]

    if config_type == "pool":
        success = generate_kv_cache_pool_config(output_path, user_config_path)
    elif config_type == "conductor":
        success = generate_kv_conductor_config(output_path, user_config_path)
    else:
        logging.error("Unknown config type: %s. Expected 'pool' or 'conductor'", config_type)
        sys.exit(1)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
