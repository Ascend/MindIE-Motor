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
from collections.abc import Callable
from dataclasses import fields
from enum import Enum
from pathlib import Path
from typing import Any

from motor.common.logger import get_logger, reconfigure_logging
from motor.common.utils.env import Env
from motor.config.resolver import ConfigResolver
from motor.config.standby import LOCK_SLASH

logger = get_logger(__name__)

MINDIE_MOTOR_CONFIG_FILENAME = "config_sample.json"
MOTOR_DEPLOY_CONFIG = "motor_deploy_config"
MOTOR_ENGINE_PREFILL_CONFIG = "motor_engine_prefill_config"
MOTOR_ENGINE_UNION_CONFIG = "motor_engine_union_config"
DEFAULT_KV_CONDUCTOR_HTTP_PORT = 13333
TLS_CONFIG = "tls_config"
MGMT_TLS_CONFIG = "mgmt_tls_config"
INFER_TLS_CONFIG = "infer_tls_config"
ETCD_TLS_CONFIG = "etcd_tls_config"
GRPC_TLS_CONFIG = "grpc_tls_config"
OBSERVABILITY_TLS_CONFIG = "observability_tls_config"
ENABLE_TLS = "enable_tls"
CA_FILE = "ca_file"
CERT_FILE = "cert_file"
KEY_FILE = "key_file"
CRL_FILE = "crl_file"
ENGINE_CONFIG = "engine_config"
ENGINE_TYPE = "engine_type"
BLOCK_SIZE = "block_size"
KV_EVENTS_CONFIG = "kv-events-config"
PREFILL_KV_EVENT_CONFIG = "prefill_kv_event_config"
ENDPOINT = "endpoint"
REPLAY_ENDPOINT = "replay_endpoint"
KV_CONDUCTOR_CONFIG = "kv_conductor_config"
HTTP_SERVER_PORT = "http_server_port"
RE_REGISTER_INTERVAL_SEC = "re_register_interval_sec"
DEFAULT_RE_REGISTER_INTERVAL_SEC = 30
MODEL_PATH = "model_path"
SSL_ENABLE = "ssl_enable"
SSL_CA_CERTS = "ssl_ca_certs"
SSL_CERTFILE = "ssl_certfile"
SSL_KEYFILE = "ssl_keyfile"
ADDITIONAL_CONFIG = "additional_config"
KV_TRANSFER_CONFIG = "kv_transfer_config"
KV_CONNECTOR_EXTRA_CONFIG = "kv_connector_extra_config"
DEPLOY_CONFIG = "deploy_config"
P_INSTANCES_NUM = "p_instances_num"
D_INSTANCES_NUM = "d_instances_num"
HYBRID_INSTANCES_NUM = "hybrid_instances_num"
SINGLE_HYBRID_INSTANCE_POD_NUM = "single_hybrid_instance_pod_num"
HYBRID_POD_NPU_NUM = "hybrid_pod_npu_num"


class ConfigKey(Enum):
    MOTOR_CONTROLLER = "motor_controller_config"
    MOTOR_COORDINATOR = "motor_coordinator_config"
    MOTOR_ENGINE_PREFILL = "motor_engine_prefill_config"
    MOTOR_ENGINE_DECODE = "motor_engine_decode_config"
    MOTOR_NODEMANAGER = "motor_nodemanger_config"
    MOTOR_KV_STORE = "kv_cache_store_config"

    @staticmethod
    def is_valid(config_key: str) -> bool:
        return config_key in [key.value for key in ConfigKey]

    @staticmethod
    def get_supported_keys() -> str:
        return ", ".join([key.value for key in ConfigKey])


def save_config_to_json(
    save_path: str,
    config_key: ConfigKey,
    config_dict: dict[str, Any],
    *,
    file_encoding: str,
    component_name: str,
) -> None:
    """Save config dict to JSON, merging into unified config when needed."""
    save_path_obj = Path(save_path)
    if save_path_obj.name == MINDIE_MOTOR_CONFIG_FILENAME:
        unified_config: dict[str, Any] = {}
        if save_path_obj.exists():
            try:
                with open(save_path_obj, "r", encoding=file_encoding) as f:
                    existing = json.load(f)
                    if isinstance(existing, dict):
                        unified_config = existing
            except Exception as e:
                logger.warning(
                    "Failed to read existing unified config: %s, overwrite with %s config",
                    e,
                    component_name,
                )
        unified_config[config_key.value] = config_dict
        with open(save_path_obj, "w", encoding=file_encoding) as f:
            json.dump(unified_config, f, indent=2, ensure_ascii=False)
    else:
        with open(save_path, "w", encoding=file_encoding) as f:
            json.dump(config_dict, f, indent=2, ensure_ascii=False)


def _get_tls_config(user_config_data: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(user_config_data, dict):
        return None
    deploy_config = user_config_data.get(MOTOR_DEPLOY_CONFIG)
    if not isinstance(deploy_config, dict):
        return None
    tls_config = deploy_config.get(TLS_CONFIG)
    if not isinstance(tls_config, dict):
        return None
    return tls_config


def _update_tls_config(
    tls_configs: list[str],
    updated_config: dict[str, Any],
    user_config_data: dict[str, Any],
) -> None:
    tls_config = _get_tls_config(user_config_data)
    if not tls_config:
        return
    for tls_key in tls_configs:
        if tls_key in tls_config:
            updated_config[tls_key] = tls_config[tls_key]


def _update_engine_server_tls_config(
    updated_config: dict[str, Any],
    user_config_data: dict[str, Any],
) -> None:
    if TLS_CONFIG not in user_config_data[MOTOR_DEPLOY_CONFIG]:
        return

    mgmt_tls_config = user_config_data[MOTOR_DEPLOY_CONFIG][TLS_CONFIG].get(MGMT_TLS_CONFIG)
    updated_config[MGMT_TLS_CONFIG] = mgmt_tls_config

    infer_tls_config = user_config_data[MOTOR_DEPLOY_CONFIG][TLS_CONFIG].get(INFER_TLS_CONFIG)
    updated_config[INFER_TLS_CONFIG] = infer_tls_config

    engine_config = updated_config[ENGINE_CONFIG]

    if infer_tls_config and infer_tls_config[ENABLE_TLS]:
        engine_config[SSL_KEYFILE] = infer_tls_config[KEY_FILE]
        engine_config[SSL_CERTFILE] = infer_tls_config[CERT_FILE]
        engine_config[SSL_CA_CERTS] = infer_tls_config[CA_FILE]

    if mgmt_tls_config and mgmt_tls_config[ENABLE_TLS]:
        if KV_TRANSFER_CONFIG not in engine_config:
            engine_config[KV_TRANSFER_CONFIG] = {}
        kv_transfer_config = engine_config[KV_TRANSFER_CONFIG]
        if KV_CONNECTOR_EXTRA_CONFIG not in kv_transfer_config:
            kv_transfer_config[KV_CONNECTOR_EXTRA_CONFIG] = {}
        kv_connector_config = kv_transfer_config[KV_CONNECTOR_EXTRA_CONFIG]
        if TLS_CONFIG not in kv_connector_config:
            kv_connector_config[TLS_CONFIG] = {}
        tls_config = kv_connector_config[TLS_CONFIG]
        tls_config[SSL_ENABLE] = True
        tls_config[SSL_KEYFILE] = infer_tls_config[KEY_FILE]
        tls_config[SSL_CERTFILE] = infer_tls_config[CERT_FILE]
        tls_config[SSL_CA_CERTS] = infer_tls_config[CA_FILE]


def _update_instances_num(
    updated_config: dict[str, Any],
    user_config_data: dict[str, Any],
) -> None:
    if not isinstance(user_config_data, dict):
        return
    deploy_config = user_config_data.get(MOTOR_DEPLOY_CONFIG)
    if not isinstance(deploy_config, dict):
        return

    hybrid_instances = deploy_config.get(HYBRID_INSTANCES_NUM)
    if hybrid_instances is not None:
        updated_config[DEPLOY_CONFIG] = {
            P_INSTANCES_NUM: hybrid_instances,
            D_INSTANCES_NUM: hybrid_instances,
            HYBRID_INSTANCES_NUM: hybrid_instances,
            SINGLE_HYBRID_INSTANCE_POD_NUM: deploy_config.get(SINGLE_HYBRID_INSTANCE_POD_NUM),
            HYBRID_POD_NPU_NUM: deploy_config.get(HYBRID_POD_NPU_NUM),
        }
        return

    updated_config[DEPLOY_CONFIG] = {
        P_INSTANCES_NUM: deploy_config.get(P_INSTANCES_NUM, 1),
        D_INSTANCES_NUM: deploy_config.get(D_INSTANCES_NUM, 1),
    }


def _resolve_re_register_interval_sec(user_config_data: dict[str, Any]) -> int:
    motor_coordinator_config = user_config_data.get(ConfigKey.MOTOR_COORDINATOR.value)
    if not isinstance(motor_coordinator_config, dict):
        return DEFAULT_RE_REGISTER_INTERVAL_SEC

    prefill_kv_config = motor_coordinator_config.get(PREFILL_KV_EVENT_CONFIG)
    if isinstance(prefill_kv_config, dict):
        interval = prefill_kv_config.get(RE_REGISTER_INTERVAL_SEC)
        if interval is not None:
            return int(interval)
    return DEFAULT_RE_REGISTER_INTERVAL_SEC


def _resolve_kv_conductor_http_port(user_config_data: dict[str, Any]) -> int:
    kv_conductor_config = user_config_data.get(KV_CONDUCTOR_CONFIG)
    if isinstance(kv_conductor_config, dict):
        port = kv_conductor_config.get(HTTP_SERVER_PORT)
        if port is not None:
            return int(port)
    return DEFAULT_KV_CONDUCTOR_HTTP_PORT


def _build_prefill_kv_event_from_engine_section(
    engine_section: dict[str, Any],
    user_config_data: dict[str, Any],
) -> dict[str, Any] | None:
    engine_config = engine_section.get(ENGINE_CONFIG)
    if not isinstance(engine_config, dict):
        logger.warning("engine_config is not dict")
        return None

    kv_events_config = engine_config.get(KV_EVENTS_CONFIG)
    if not isinstance(kv_events_config, dict):
        return None

    resolver = ConfigResolver(engine_section)
    return {
        ENDPOINT: kv_events_config.get(ENDPOINT, ""),
        REPLAY_ENDPOINT: kv_events_config.get(REPLAY_ENDPOINT, ""),
        BLOCK_SIZE: engine_config.get("block-size", 128),
        HTTP_SERVER_PORT: _resolve_kv_conductor_http_port(user_config_data),
        MODEL_PATH: resolver.get_model_path(""),
        RE_REGISTER_INTERVAL_SEC: _resolve_re_register_interval_sec(user_config_data),
    }


def _select_kv_event_engine_section(user_config_data: dict[str, Any]) -> dict[str, Any] | None:
    if MOTOR_ENGINE_PREFILL_CONFIG in user_config_data:
        prefill_section = user_config_data.get(MOTOR_ENGINE_PREFILL_CONFIG)
        if isinstance(prefill_section, dict):
            return prefill_section
    if MOTOR_ENGINE_UNION_CONFIG in user_config_data:
        union_section = user_config_data.get(MOTOR_ENGINE_UNION_CONFIG)
        if isinstance(union_section, dict):
            return union_section
    return None


def _update_prefill_kv_event_config(updated_config: dict[str, Any], user_config_data: dict[str, Any]) -> None:
    try:
        engine_section = _select_kv_event_engine_section(user_config_data)
        if engine_section is None:
            return

        prefill_kv_event = _build_prefill_kv_event_from_engine_section(engine_section, user_config_data)
        if prefill_kv_event is None:
            return

        updated_config[PREFILL_KV_EVENT_CONFIG] = prefill_kv_event
    except Exception as e:
        logger.warning("Failed to get kv event engine config: %s", e)


def log_json_config_format_error(json_path: str | None, exc: Exception) -> None:
    logger.warning(
        "Configuration file %s format error: %s, using default configuration",
        json_path,
        exc,
    )


def log_json_config_read_error(json_path: str | None, exc: Exception) -> None:
    logger.warning(
        "Unable to read configuration file %s: %s, using default configuration",
        json_path,
        exc,
    )


def log_json_config_load_error(json_path: str | None, exc: Exception) -> None:
    if isinstance(exc, json.JSONDecodeError):
        log_json_config_format_error(json_path, exc)
    else:
        log_json_config_read_error(json_path, exc)


def refresh_master_lock_key(standby_config: Any, component: str) -> None:
    if standby_config.master_lock_key == "/master_lock":
        standby_config.master_lock_key = LOCK_SLASH + component + standby_config.master_lock_key


def apply_standby_persistence_rule(instance: Any) -> None:
    """If master/standby is enabled, automatically enable ETCD persistence.

    When enable_master_standby is True, the system must persist Coordinator state
    in ETCD so the standby can resume on failover. Without persistence the standby
    cannot restore runtime state, which defeats the purpose of master/standby.
    """
    standby = instance.standby_config
    etcd = instance.etcd_config
    if standby.enable_master_standby and not etcd.enable_etcd_persistence:
        etcd.enable_etcd_persistence = True
        logger.info("Auto-enabled etcd persistence because master/standby mode is enabled")


def init_motor_config(instance: Any, component: str) -> None:
    refresh_master_lock_key(instance.standby_config, component)
    apply_standby_persistence_rule(instance)
    instance.validate_config()


def resolve_config_json_path(json_path: str | None) -> tuple[str | None, Path | None]:
    if json_path is None:
        json_path = Env.user_config_path
    return json_path, Path(json_path) if json_path else None


def apply_config_path_metadata(config: Any, config_path: Path | None) -> None:
    if config_path:
        config.config_path = str(config_path)
        if config_path.exists():
            config.last_modified = config_path.stat().st_mtime
    else:
        config.config_path = None


def log_config_file_loaded(config_path: Path | None) -> None:
    if config_path:
        logger.info("Loading configuration file: %s", config_path)
        if config_path.exists():
            logger.info("Successfully loaded configuration file: %s", config_path)
        else:
            logger.warning(
                "Configuration file does not exist, using default configuration: %s",
                config_path,
            )


def finalize_json_config_load(config_path: Path | None, *, no_path_message: str) -> None:
    log_config_file_loaded(config_path)
    if not config_path:
        logger.info(no_path_message)
    logger.info("Configuration loading completed")


def raise_if_config_errors(errors: list[str]) -> None:
    if errors:
        error_msg = "Configuration validation failed:\n" + "\n".join(f"  - {error}" for error in errors)
        logger.error(error_msg)
        raise ValueError(error_msg)


def sync_dataclass_fields_from(
    target: Any,
    source: Any,
    *,
    skip: frozenset[str] = frozenset(),
    skip_private: bool = True,
) -> None:
    """Copy dataclass field values from source onto target."""
    for dc_field in fields(target):
        name = dc_field.name
        if skip_private and name.startswith("_"):
            continue
        if name in skip:
            continue
        setattr(target, name, getattr(source, name))


def reload_dataclass_config_from_json(
    instance: Any,
    loader: Callable[[str], Any],
    *,
    skip: frozenset[str] = frozenset(),
    skip_private: bool = True,
    success_message: str = "Configuration reload successful",
    error_message: str = "Configuration reload failed: %s",
) -> bool:
    if not instance.config_path or not os.path.exists(instance.config_path):
        logger.warning("Configuration file path does not exist, cannot reload")
        return False

    try:
        current_mtime = os.path.getmtime(instance.config_path)
        if instance.last_modified and current_mtime <= instance.last_modified:
            logger.debug("Configuration file not modified, skipping reload")
            return True

        logger.info("Configuration file change detected, reloading...")
        new_config = loader(instance.config_path)
        sync_dataclass_fields_from(instance, new_config, skip=skip, skip_private=skip_private)
        instance.last_modified = current_mtime
        reconfigure_logging(instance.logging_config)
        logger.info(success_message)
        return True
    except Exception as exc:
        logger.error(error_message, exc)
        return False


def persist_config_to_json(
    save_path: str,
    config_key: ConfigKey,
    config_dict: dict[str, Any],
    *,
    file_encoding: str,
    component_name: str,
) -> bool:
    try:
        save_config_to_json(
            save_path,
            config_key,
            config_dict,
            file_encoding=file_encoding,
            component_name=component_name,
        )
        logger.info("Configuration saved to: %s", save_path)
        return True
    except Exception as exc:
        logger.error("Failed to save configuration: %s", exc)
        return False


def save_instance_config_to_json(
    instance: Any,
    json_path: str | None,
    *,
    config_key: ConfigKey,
    file_encoding: str,
    component_name: str,
    missing_path_message: str = "Save path not specified",
) -> bool:
    save_path = json_path or instance.config_path
    if not save_path:
        logger.error(missing_path_message)
        return False
    return persist_config_to_json(
        save_path,
        config_key,
        instance.to_dict(),
        file_encoding=file_encoding,
        component_name=component_name,
    )


def format_config_summary_header(title: str) -> str:
    separator = "=" * 80
    return f"{separator}\n{title}\n{separator}\n"
