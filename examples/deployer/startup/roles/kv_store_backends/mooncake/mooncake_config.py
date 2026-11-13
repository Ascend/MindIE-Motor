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

KV_STORE_CONFIG_KEY = "kv_cache_store_config"
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
        if "served_model_name" in engine_config:
            return name or engine_config.get("model", default)
    if name:
        return name
    model_config = engine_section.get("model_config", {})
    if engine_type == "vllm":
        return model_config.get("model_name") or engine_config.get("model") or default
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


def resolve_master_server_address(kv_cfg: dict[str, Any]) -> str:
    """Return the explicit ``master_server_address`` or ``""`` when not configured.

    The value is passed through verbatim so that HA connection strings such as
    ``etcd://10.0.0.1:2379;10.0.0.2:2379`` or ``redis://10.0.0.1:6379`` survive; they must
    never be run through ``format_address`` (that would append a port to the whole string).
    """
    explicit = kv_cfg.get(MASTER_SERVER_ADDRESS, "")
    if not isinstance(explicit, str):
        return ""
    return explicit.strip()


def is_self_describing(address: str) -> bool:
    """Return True when the address already carries its own endpoints.

    Both URI forms (``etcd://...`` / ``redis://...``) and semicolon-separated multi-endpoint
    lists (``h1:2379;h2:2379``) describe themselves; appending a port or a ``tcp://`` prefix
    would corrupt them. Only a plain ``host:port`` may be passed to ``format_address``.
    """
    return "://" in address or ";" in address


def generate_kv_cache_store_config(output_path: str, user_config_path: str) -> bool:
    if not os.path.exists(user_config_path):
        logging.error("user_config.json does not exist: %s", user_config_path)
        return False

    user_cfg = read_json(user_config_path)
    kv_cfg = user_cfg.get(KV_STORE_CONFIG_KEY)
    if not kv_cfg:
        logging.error("KV cache pool config not provided, skipping kv_cache_store_config generation")
        return False

    out_cfg: dict[str, Any] = dict(kv_cfg)

    out_cfg.setdefault("metadata_server", "P2PHANDSHAKE")

    # Standalone: the store process contributes memory; engines contribute nothing but keep a staging buffer.
    if kv_cfg.get("store_mode") == "standalone":
        out_cfg["global_segment_size"] = 0
        out_cfg["local_buffer_size"] = kv_cfg.get("local_buffer_size") or "1GB"

    explicit_master = resolve_master_server_address(kv_cfg)
    if explicit_master:
        out_cfg[MASTER_SERVER_ADDRESS] = explicit_master
    else:
        kvp_master_service = os.getenv("KVS_MASTER_SERVICE", "")
        if not kvp_master_service:
            logging.error("Env KVS_MASTER_SERVICE is required but not set, cannot generate kv_cache_store_config")
            return False
        master_server_port = kv_cfg.get(MASTER_SERVER_PORT_KEY, DEFAULT_MASTER_SERVER_PORT)
        out_cfg[MASTER_SERVER_ADDRESS] = format_address(kvp_master_service, master_server_port)

    write_json(output_path, out_cfg)
    logging.info("kv_cache_store_config generated: %s", output_path)
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

    kv_store_cfg = user_cfg.get(KV_STORE_CONFIG_KEY)
    if not kv_store_cfg:
        logging.error("KV cache pool config not provided, skipping kv_cache_store_config generation")
        return False

    kvp_master_service = os.getenv("KVS_MASTER_SERVICE", "")
    explicit_master = resolve_master_server_address(kv_store_cfg)
    mooncake_master = out_cfg[KVEVENT_INSTANCE][MOONCAKE_MASTER]
    if not mooncake_master.get(ENDPOINT_ADDRESS):
        if explicit_master and not is_self_describing(explicit_master):
            mooncake_master[ENDPOINT_ADDRESS] = f"tcp://{explicit_master}"
        elif explicit_master:
            # Self-describing addresses (URI or semicolon-separated endpoints) resolve to the
            # current leader at runtime; there is no static host to derive the kv-events
            # endpoint from.
            logging.warning(
                "master_server_address %r is a self-describing address; cannot derive the kv-events "
                "endpoint. Set kvevent_instance.mooncake_master.endpoint explicitly if KV events "
                "or pool registration are required.",
                explicit_master,
            )
        else:
            if not kvp_master_service:
                logging.error("Env KVS_MASTER_SERVICE is required but not set, cannot generate kv_cache_store_config")
                return False
            master_server_port = kv_store_cfg.get(MASTER_SERVER_PORT_KEY, DEFAULT_MASTER_SERVER_PORT)
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
        success = generate_kv_cache_store_config(output_path, user_config_path)
    elif config_type == "conductor":
        success = generate_kv_conductor_config(output_path, user_config_path)
    else:
        logging.error("Unknown config type: %s. Expected 'pool' or 'conductor'", config_type)
        sys.exit(1)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
