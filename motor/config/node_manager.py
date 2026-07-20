# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import json
from typing import Any
from enum import Enum
from dataclasses import dataclass, field, asdict
from pathlib import Path

from motor.common.resources.instance import ParallelConfig, PDRole
from motor.common.resources.dispatch import (
    DISPATCH_PROFILE_KEY,
    DispatchPlan,
    DispatchProfile,
    classify_vllm_dispatch_profile,
    dispatch_capabilities_for_profile,
)
from motor.config.resolver import ConfigResolver
from motor.config.tls_config import TLSConfig
from motor.common.utils.env import Env
from motor.common.utils.patch_check import safe_open
from motor.common.logger import get_logger
from motor.config.config_utils import (
    ConfigKey,
    format_config_summary_header,
    raise_if_config_errors,
    reload_dataclass_config_from_json,
    save_instance_config_to_json,
    _update_tls_config,
    MGMT_TLS_CONFIG,
)
from motor.config.log_config import LoggingConfig
from motor.config.port_allocator_config import PortAllocatorConfig


FILE_ENCODING = "utf-8"

PP = "pp_size"
TP = "tp_size"
DP = "dp_size"
BASIC_CONFIG_KEY = "basic_config"
SNAPSHOT_CONFIG_KEY = "snapshot_config"
PARALLEL_CONFIG_KEY = "parallel_config"
MOTOR_NODE_MANAGER_CONFIG_KEY = "motor_nodemanger_config"
MOTOR_ENGINE_ENCODE_CONFIG_KEY = "motor_engine_encode_config"
MOTOR_ENGINE_PREFILL_CONFIG_KEY = "motor_engine_prefill_config"
MOTOR_ENGINE_DECODE_CONFIG_KEY = "motor_engine_decode_config"
MOTOR_ENGINE_UNION_CONFIG_KEY = "motor_engine_union_config"
MOTOR_CONTAINER_SNAPSHOT_CONFIG_KEY = "motor_container_snapshot_config"
ENGINE_CONFIG_KEY = "engine_config"
KV_TRANSFER_CONFIG_KEY = "kv_transfer_config"
KV_CONNECTOR_KEY = "kv_connector"
MULTICONNECTOR = "MultiConnector"
KV_CONNECTOR_EXTRA_CONFIG_KEY = "kv_connector_extra_config"
CONNECTORS_KEY = "connectors"
KV_PORT_KEY = "kv_port"
LOOPUP_RPC_PORT_KEY = "lookup_rpc_port"
UCM_CONNECTOR = "UCMConnector"
SERVER_LIST = "server_list"
DEVICE = "device"
HARDWARE_TYPE_KEY = "hardware_type"
MODEL_NAME_KEY = "model_name"
ENGINE_TYPE_KEY = "engine_type"
DISPATCH_CAPABILITIES_KEY = "dispatch_capabilities"
ENABLE_MULTI_ENDPOINTS_KEY = "enable_multi_endpoints"

ENGINE_TYPE_VLLM = "vllm"
ENGINE_TYPE_SGLANG = "sglang"

ENABLE_SNAPSHOT_KEY = "enable_snapshot"
SNAPSHOT_METADATA_PATH_KEY = "snapshot_metadata_path"

logger = get_logger(__name__)


class HardwareType(str, Enum):
    TYPE_800I_A2 = "800I-A2"
    TYPE_800I_A3 = "800I-A3"
    # A5 系列
    TYPE_350_ATLAS_8 = "350-Atlas-8"
    TYPE_350_ATLAS_16 = "350-Atlas-16"
    TYPE_350_ATLAS_4P_8 = "350-Atlas-4p-8"
    TYPE_350_ATLAS_4P_16 = "350-Atlas-4p-16"
    TYPE_850_ATLAS_8P_8 = "850-Atlas-8p-8"
    TYPE_850_SUPERPOD_ATLAS_8 = "850-SuperPod-Atlas-8"
    TYPE_950_SUPERPOD_ATLAS_8 = "950-SuperPod-Atlas-8"

    def __repr__(self) -> str:
        return str.__repr__(self.value)

    @classmethod
    def is_a5(cls, hardware_type: str) -> bool:
        return hardware_type in {member.value for member in cls if member not in (cls.TYPE_800I_A2, cls.TYPE_800I_A3)}


@dataclass
class BasicConfig:
    """Basic configuration class"""

    # Job configuration
    job_name: str = Env.job_name
    role: PDRole = PDRole.ROLE_U
    model_name: str = ""
    engine_type: str | None = None
    dispatch_capabilities: list[str] = field(default_factory=list)
    hardware_type: HardwareType = HardwareType.TYPE_800I_A3

    # Heartbeat sending configuration
    heartbeat_interval_seconds: int = 3

    # Device information
    device_num: int = 0
    # Parallel configuration
    parallel_config: ParallelConfig = field(default_factory=ParallelConfig)
    # Multi-endpoints configuration
    enable_multi_endpoints: bool = True
    # Cross-node PCP configuration
    nnodes: int = 1


@dataclass
class SnapshotConfig:
    """Snapshot configuration class"""

    enable_snapshot: bool = False
    snapshot_metadata_path: str = ""


@dataclass
class APIConfig:
    """API configuration class"""

    # http config
    pod_ip: str | None = field(default_factory=lambda: Env.pod_ip or "127.0.0.1")
    node_manager_port: int = 1026


@dataclass
class EndpointConfig:
    # EngineServer's number
    endpoint_num: int = 0

    # EngineServer's Port configuration
    base_port: int = 10000
    mgmt_ports: list[str] = field(default_factory=list)
    service_ports: list[str] = field(default_factory=list)


@dataclass
class SingleContainerNodemanagerConfig:
    single_container_flag: bool = False
    node_manager_port_offset: int | None = None
    base_port_offset: int | None = None
    device_offset: int | None = None
    device_num: int | None = None
    kv_port: int | None = None
    lookup_rpc_port: int | None = None
    dp_rpc_port: int | None = None

    @classmethod
    def from_json(cls, user_config_data: dict[str, Any]) -> "SingleContainerNodemanagerConfig":
        config = cls()
        deploy_mode = user_config_data.get("motor_deploy_config", {}).get("deploy_mode", "")
        if deploy_mode != "single_container" or not Env.role or Env.index is None:
            return config

        config.single_container_flag = True
        index = int(Env.index)

        union_section = user_config_data.get(MOTOR_ENGINE_UNION_CONFIG_KEY)
        prefill_section = user_config_data.get(MOTOR_ENGINE_PREFILL_CONFIG_KEY)
        if union_section and not prefill_section:
            if Env.role != "union":
                return config

            union_resolver = ConfigResolver(union_section)
            union_parallel_config = union_resolver.get_parallel_config()
            u_dp_size = union_parallel_config.get(DP, 1)
            u_world_size = union_parallel_config["world_size"]

            config.node_manager_port_offset = index
            config.base_port_offset = index * u_dp_size * 2
            config.device_offset = index * u_world_size
            config.device_num = u_world_size
            config.dp_rpc_port = int(union_parallel_config["dp_rpc_port"]) + index
            return config

        p_instances_num = user_config_data['motor_deploy_config']['p_instances_num']
        d_instances_num = user_config_data['motor_deploy_config']['d_instances_num']
        encode_section = user_config_data.get(MOTOR_ENGINE_ENCODE_CONFIG_KEY, {})
        prefill_section = user_config_data[MOTOR_ENGINE_PREFILL_CONFIG_KEY]
        decode_section = user_config_data[MOTOR_ENGINE_DECODE_CONFIG_KEY]
        encode_resolver = ConfigResolver(encode_section) if encode_section else None
        prefill_resolver = ConfigResolver(prefill_section)
        decode_resolver = ConfigResolver(decode_section)
        encode_parallel_config = encode_resolver.get_parallel_config() if encode_resolver else {}
        prefill_parallel_config = prefill_resolver.get_parallel_config()
        decode_parallel_config = decode_resolver.get_parallel_config()
        e_dp_size = encode_parallel_config.get(DP, 1)
        p_dp_size = prefill_parallel_config.get(DP, 1)
        d_dp_size = decode_parallel_config.get(DP, 1)
        e_world_size = encode_parallel_config.get("world_size", 0)
        p_world_size = prefill_parallel_config["world_size"]
        d_world_size = decode_parallel_config["world_size"]

        d_node_manager_port_offset = p_instances_num * p_dp_size + index
        d_base_port_offset = (p_instances_num * p_dp_size + index * d_dp_size) * 2
        d_device_offset = p_instances_num * p_world_size + index * d_world_size

        e_node_manager_port_offset = d_instances_num * d_dp_size + d_node_manager_port_offset
        e_base_port_offset = d_base_port_offset + e_dp_size
        e_device_offset = d_device_offset + e_world_size

        kv_port_offset = 0
        lookup_rpc_port_offset = 0
        dp_rpc_port_offset = 0

        if Env.role == 'prefill':
            config.node_manager_port_offset = index
            config.base_port_offset = index * d_dp_size * 2
            config.device_offset = index * p_world_size
            config.device_num = p_world_size
            kv_port_offset = config.device_offset
            lookup_rpc_port_offset = index
            dp_rpc_port_offset = index
        elif Env.role == 'decode':
            config.node_manager_port_offset = d_node_manager_port_offset
            config.base_port_offset = d_base_port_offset
            config.device_offset = d_device_offset
            config.device_num = d_world_size
            kv_port_offset = config.device_offset
            lookup_rpc_port_offset = p_instances_num + index
            dp_rpc_port_offset = p_instances_num + index
        elif Env.role == 'encode':
            config.node_manager_port_offset = e_node_manager_port_offset
            config.base_port_offset = e_base_port_offset
            config.device_offset = e_device_offset
            config.device_num = e_world_size
            kv_port_offset = config.device_offset
            lookup_rpc_port_offset = index
            dp_rpc_port_offset = index

        kv_config = user_config_data[MOTOR_ENGINE_PREFILL_CONFIG_KEY][ENGINE_CONFIG_KEY].get(KV_TRANSFER_CONFIG_KEY, {})
        if kv_config:
            if kv_config[KV_CONNECTOR_KEY] == MULTICONNECTOR:
                extra_config = kv_config.get(KV_CONNECTOR_EXTRA_CONFIG_KEY)
                connectors = extra_config.get(CONNECTORS_KEY) if isinstance(extra_config, dict) else None
                if not isinstance(connectors, list) or len(connectors) < 2:
                    raise ValueError(
                        f"{KV_TRANSFER_CONFIG_KEY}.{KV_CONNECTOR_EXTRA_CONFIG_KEY}.{CONNECTORS_KEY} "
                        f"must be a list of at least 2 connectors (transport first, store second) "
                        f"when {KV_CONNECTOR_KEY} is {MULTICONNECTOR}"
                    )
                if not all(isinstance(connector, dict) for connector in connectors[:2]):
                    raise ValueError(
                        f"{KV_TRANSFER_CONFIG_KEY}.{KV_CONNECTOR_EXTRA_CONFIG_KEY}.{CONNECTORS_KEY} "
                        "entries must be objects (connector configs)"
                    )
                config.kv_port = int(connectors[0][KV_PORT_KEY]) + kv_port_offset
                store = connectors[1]
                # UCM store carries no lookup_rpc_port. Skip ONLY UCM (use .get() to avoid a
                # KeyError on the kv_connector lookup); every other store still direct-indexes
                # lookup_rpc_port, so a genuine AscendStore missing its port fails fast exactly
                # as before instead of being silently skipped.
                if store.get(KV_CONNECTOR_KEY) != UCM_CONNECTOR:
                    config.lookup_rpc_port = int(store[LOOPUP_RPC_PORT_KEY]) + lookup_rpc_port_offset
            else:
                config.kv_port = int(kv_config[KV_PORT_KEY]) + kv_port_offset

        config.dp_rpc_port = int(prefill_parallel_config["dp_rpc_port"]) + dp_rpc_port_offset

        return config


@dataclass
class NodeManagerFaultToleranceConfig:
    """Fault tolerance configuration for NodeManager"""

    enable_fault_tolerance: bool = False
    zmq_pub_port: int = 0


@dataclass
class KVCacheStoreConfig:
    """KV cache store configuration — parsed from ``kv_cache_store_config``."""

    enable: bool = False  # True when kv_cache_store_config is present
    backend: str = "memcache"
    service: str = ""  # default from $KVS_MASTER_SERVICE
    local_service_mode: str = ""  # "standalone" / "inprocess"
    dram_size: str = ""  # e.g. "100GB"
    port: int = 50088  # RPC port
    config_store_port: int = 50089  # ConfigStore TCP port
    local_config_path: str = "/usr/local/Ascend/pyMotor/conf/mmc-local.conf"


@dataclass
class NodeManagerConfig:
    """Global configuration singleton for node manager"""

    # Configuration sections
    api_config: APIConfig = field(default_factory=APIConfig)
    mgmt_tls_config: TLSConfig = field(default_factory=TLSConfig)
    endpoint_config: EndpointConfig = field(default_factory=EndpointConfig)
    basic_config: BasicConfig = field(default_factory=BasicConfig)
    snapshot_config: SnapshotConfig = field(default_factory=SnapshotConfig)
    logging_config: LoggingConfig = field(default_factory=LoggingConfig)
    single_container_config: SingleContainerNodemanagerConfig = field(default_factory=SingleContainerNodemanagerConfig)
    fault_tolerance_config: NodeManagerFaultToleranceConfig = field(default_factory=NodeManagerFaultToleranceConfig)
    port_allocator_config: PortAllocatorConfig = field(default_factory=PortAllocatorConfig)
    kv_cache_store_config: KVCacheStoreConfig = field(default_factory=KVCacheStoreConfig)

    # Internal fields
    config_path: str | None = field(default=None, init=False)
    last_modified: float | None = field(default=None, init=False)

    def __post_init__(self):
        """Validate configuration after initialization"""
        # Set internal paths with defaults only if not already set (e.g., by from_json)
        if not hasattr(self, "config_path") or self.config_path is None:
            self.config_path = Env.user_config_path or Env.config_path

        # Set last modified time if config file exists
        try:
            if self.config_path and os.path.exists(self.config_path):
                self.last_modified = os.path.getmtime(self.config_path)
        except (OSError, IOError):
            # Ignore errors when checking file modification time
            pass

        self.validate_config()

    @classmethod
    def from_json(cls, config_path: str | None = None) -> "NodeManagerConfig":
        """Load configuration from user_config.json"""
        if config_path is None:
            config_path = Env.user_config_path or Env.config_path

        config_path_obj = Path(config_path) if config_path else None
        logger.info("Loading configuration files: config=%s", config_path_obj)

        config = cls()
        config.config_path = config_path

        config_data = {}
        raw = None
        if config_path_obj is not None and os.path.exists(str(config_path_obj)):
            with safe_open(str(config_path_obj), "r") as f:
                raw = json.load(f)
            logger.info("Successfully loaded config file: %s", config_path_obj)

            config.single_container_config = SingleContainerNodemanagerConfig.from_json(raw)

            if isinstance(raw, dict):
                cls._discard_user_dispatch_capabilities(raw)
                config_data = cls._load_node_manager_config_data(raw)
            else:
                config_data = raw

            cls._update_from_config_data(config, config_data)
        else:
            logger.warning("Config file does not exist, using default configuration: %s", config_path_obj)

        cls._set_device_count_from_config(config, raw)
        cls._parse_kv_cache_store_config(config, raw)

        config.validate_config()

        if config_path_obj is not None and config_path_obj.exists():
            config.last_modified = config_path_obj.stat().st_mtime

        logger.info("Configuration loading completed")
        return config

    @classmethod
    def _discard_user_dispatch_capabilities(cls, user_cfg: dict[str, Any]) -> None:
        """Remove user overrides; capabilities are derived from engine semantics."""
        removed = False

        root_basic = user_cfg.get(BASIC_CONFIG_KEY)
        if isinstance(root_basic, dict):
            removed = root_basic.pop(DISPATCH_CAPABILITIES_KEY, None) is not None or removed

        engine_keys = (
            MOTOR_ENGINE_ENCODE_CONFIG_KEY,
            MOTOR_ENGINE_PREFILL_CONFIG_KEY,
            MOTOR_ENGINE_DECODE_CONFIG_KEY,
            MOTOR_ENGINE_UNION_CONFIG_KEY,
        )
        for engine_key in engine_keys:
            engine_config = user_cfg.get(engine_key)
            if not isinstance(engine_config, dict):
                continue
            removed = engine_config.pop(DISPATCH_CAPABILITIES_KEY, None) is not None or removed
            node_manager_config = engine_config.get(MOTOR_NODE_MANAGER_CONFIG_KEY)
            if not isinstance(node_manager_config, dict):
                continue
            basic_config = node_manager_config.get(BASIC_CONFIG_KEY)
            if isinstance(basic_config, dict):
                removed = basic_config.pop(DISPATCH_CAPABILITIES_KEY, None) is not None or removed

        if removed:
            logger.warning(
                "User-configured dispatch_capabilities is no longer supported and was ignored. "
                "Configure kv_transfer_config or dispatch_profile instead."
            )

    @classmethod
    def _load_node_manager_config_data(cls, user_cfg: dict[str, Any]) -> dict[str, Any]:
        """Load node_manager_config from engine config based on role"""
        engine_config_key = None
        if Env.role == "encode":
            engine_config_key = MOTOR_ENGINE_ENCODE_CONFIG_KEY
        elif Env.role == "prefill":
            engine_config_key = MOTOR_ENGINE_PREFILL_CONFIG_KEY
        elif Env.role == "decode":
            engine_config_key = MOTOR_ENGINE_DECODE_CONFIG_KEY
        elif Env.role in ("union", "both"):
            engine_config_key = MOTOR_ENGINE_UNION_CONFIG_KEY

        if not engine_config_key or engine_config_key not in user_cfg:
            return user_cfg

        engine_config = user_cfg[engine_config_key]
        if "motor_nodemanger_config" in engine_config:
            config_data = engine_config.get("motor_nodemanger_config", {})
        else:
            config_data = {}

        if BASIC_CONFIG_KEY not in config_data:
            config_data[BASIC_CONFIG_KEY] = {}

        resolver = ConfigResolver(engine_config)
        config_data[BASIC_CONFIG_KEY][MODEL_NAME_KEY] = resolver.get_model_name("")
        config_data[BASIC_CONFIG_KEY][ENGINE_TYPE_KEY] = engine_config.get(ENGINE_TYPE_KEY)
        config_data[BASIC_CONFIG_KEY][DISPATCH_CAPABILITIES_KEY] = cls._infer_dispatch_capabilities(engine_config)
        config_data[BASIC_CONFIG_KEY][HARDWARE_TYPE_KEY] = user_cfg["motor_deploy_config"][HARDWARE_TYPE_KEY]

        # Read nnodes from engine_config for cross-node PCP support
        engine_cfg = engine_config.get(ENGINE_CONFIG_KEY, {})
        try:
            config_data[BASIC_CONFIG_KEY]["nnodes"] = int(engine_cfg.get("nnodes", 1))
        except (TypeError, ValueError):
            config_data[BASIC_CONFIG_KEY]["nnodes"] = 1

        if Env.role in ("encode", "prefill", "decode", "union", "both"):
            config_data[BASIC_CONFIG_KEY]["parallel_config"] = resolver.get_parallel_config()
            config_data[BASIC_CONFIG_KEY][ENABLE_MULTI_ENDPOINTS_KEY] = resolver.get_enable_multi_endpoints()

        # Adjust local_world_size for cross-node PCP: each node contributes pcp/nnodes PCP ranks
        nnodes = config_data[BASIC_CONFIG_KEY].get("nnodes", 1)
        pc = config_data[BASIC_CONFIG_KEY].get("parallel_config", {})
        pcp_size = pc.get("pcp_size", 1)
        if isinstance(nnodes, int) and nnodes > 1 and pcp_size > 1 and pcp_size % nnodes == 0:
            per_node_pcp = pcp_size // nnodes
            per_node_tp = pc.get("tp_size", 1)
            per_node_pp = pc.get("pp_size", 1)
            pc["local_world_size"] = per_node_pcp * per_node_tp * per_node_pp
            logger.info(
                "Cross-node PCP detected (nnodes=%d, pcp=%d): per-node local_world_size adjusted from %d to %d",
                nnodes,
                pcp_size,
                pcp_size * per_node_tp * per_node_pp,
                pc["local_world_size"],
            )

        _update_tls_config([MGMT_TLS_CONFIG], config_data, user_cfg)

        snapshot_cfg = user_cfg.get(MOTOR_CONTAINER_SNAPSHOT_CONFIG_KEY, {})
        config_data[SNAPSHOT_CONFIG_KEY] = {
            ENABLE_SNAPSHOT_KEY: snapshot_cfg.get(ENABLE_SNAPSHOT_KEY, False),
            SNAPSHOT_METADATA_PATH_KEY: snapshot_cfg.get(SNAPSHOT_METADATA_PATH_KEY, ""),
        }

        return config_data

    @classmethod
    def _infer_dispatch_capabilities(cls, engine_config: dict[str, Any]) -> list[str]:
        """Infer Motor dispatch capabilities from engine-native config."""
        engine_type = str(engine_config.get(ENGINE_TYPE_KEY, "")).strip().lower()
        native_engine_config = engine_config.get(ENGINE_CONFIG_KEY, {})
        if not isinstance(native_engine_config, dict):
            native_engine_config = {}

        if engine_type == ENGINE_TYPE_SGLANG:
            return [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
        if engine_type == ENGINE_TYPE_VLLM:
            profile = classify_vllm_dispatch_profile(
                native_engine_config,
                explicit_profile=engine_config.get(DISPATCH_PROFILE_KEY),
            )
            capabilities = dispatch_capabilities_for_profile(profile)
            if not capabilities and profile == DispatchProfile.UNKNOWN:
                logger.warning(
                    "Unable to infer vLLM dispatch capability from kv_transfer_config. "
                    "Configure a supported connector or set dispatch_profile explicitly."
                )
            return capabilities
        return []

    @classmethod
    def _update_from_config_data(cls, config: "NodeManagerConfig", cfg: dict[str, Any]):
        """Update configuration from config JSON data"""

        # Helper function to update config object from dict
        def update_config_from_dict(config_obj, config_dict):
            """Update configuration object fields from dictionary, only for existing keys"""
            for key, value in config_dict.items():
                if hasattr(config_obj, key):
                    setattr(config_obj, key, value)

        # Update configuration sections if they exist in JSON
        if "logging_config" in cfg:
            update_config_from_dict(config.logging_config, cfg["logging_config"])

        if "api_config" in cfg:
            update_config_from_dict(config.api_config, cfg["api_config"])

        if "mgmt_tls_config" in cfg:
            update_config_from_dict(config.mgmt_tls_config, cfg["mgmt_tls_config"])

        if "fault_tolerance_config" in cfg:
            update_config_from_dict(config.fault_tolerance_config, cfg["fault_tolerance_config"])

        if "port_allocator_config" in cfg:
            update_config_from_dict(config.port_allocator_config, cfg["port_allocator_config"])

        if "endpoint_config" in cfg:
            update_config_from_dict(config.endpoint_config, cfg["endpoint_config"])

        if SNAPSHOT_CONFIG_KEY in cfg:
            update_config_from_dict(config.snapshot_config, cfg[SNAPSHOT_CONFIG_KEY])

        if BASIC_CONFIG_KEY in cfg:
            basic_cfg = cfg[BASIC_CONFIG_KEY]
            update_config_from_dict(config.basic_config, basic_cfg)
            # Handle parallel_config specially
            if "parallel_config" in basic_cfg:
                pc = basic_cfg["parallel_config"]
                if isinstance(pc, dict):
                    config.basic_config.parallel_config = ParallelConfig(**pc)

        if config.single_container_config.single_container_flag:
            config.api_config.node_manager_port += config.single_container_config.node_manager_port_offset
            config.endpoint_config.base_port += config.single_container_config.base_port_offset

        # Set role from environment
        try:
            role = Env.role
            config.basic_config.role = PDRole(role)
            logger.info("Role from environment: %s", role)
            logger.info("Role from config: %s", config.basic_config.role)
        except ValueError as e:
            raise ValueError("Invalid role value from environment") from e

    @classmethod
    def _set_device_count_from_config(cls, config: "NodeManagerConfig", raw: dict | None):
        """
        Set device count from config based on role.
        For prefill role, use p_pod_npu_num.
        For decode role, use d_pod_npu_num.
        For single_container mode, use parallel_config.world_size instead.
        """
        try:
            if not isinstance(raw, dict) or "motor_deploy_config" not in raw:
                logger.warning("No motor_deploy_config found, using default device configuration")
                config.basic_config.device_num = 0
                config.endpoint_config.endpoint_num = 0
                config.endpoint_config.service_ports = []
                config.endpoint_config.mgmt_ports = []
                return

            if config.single_container_config.single_container_flag:
                cls._set_device_count_for_single_container(config)
            else:
                cls._set_device_count_for_normal_mode(config, raw)

            cls._generate_endpoint_ports(config)

        except Exception as e:
            logger.error("Failed to get device count from config: %s", e)
            config.basic_config.device_num = 0
            config.endpoint_config.endpoint_num = 0
            config.endpoint_config.service_ports = []
            config.endpoint_config.mgmt_ports = []

    @classmethod
    def _parse_kv_cache_store_config(cls, config: "NodeManagerConfig", raw: dict | None):
        """Populate ``kv_cache_store_config`` from ``user_config.json``.

        Env vars serve as fallback for values only the deployer provides.
        When the config section is missing, env vars alone can enable KV store.
        """
        if not isinstance(raw, dict):
            raw = {}
        kv = raw.get("kv_cache_store_config", {})
        if not isinstance(kv, dict):
            kv = {}
        kcfg = config.kv_cache_store_config

        # --- enable: config section or deployer-injected env var ---
        if kv or os.getenv("KV_STORE_BACKEND", ""):
            kcfg.enable = True

        # --- populate from config first, env var as fallback ---
        if "backend" in kv:
            kcfg.backend = kv["backend"]
        if not kcfg.service:
            kcfg.service = kv.get("service", "") or os.getenv("KVS_MASTER_SERVICE", "")
        if not kcfg.local_service_mode:
            kcfg.local_service_mode = kv.get("local_service_mode", "") or os.getenv("MMC_LOCAL_SERVICE_MODE", "")
        if not kcfg.dram_size:
            kcfg.dram_size = kv.get("dram_size", "") or os.getenv("MMC_DRAM_SIZE", "")
        port = kv.get("port", 0)
        if port:
            kcfg.port = int(port)
        elif not kcfg.port or kcfg.port == 50088:
            env_port = os.getenv("KV_CACHE_STORE_PORT", "")
            if env_port:
                kcfg.port = int(env_port)
        cs_port = kv.get("config_store_port", 0)
        if cs_port:
            kcfg.config_store_port = int(cs_port)
        config_path = kv.get("local_config_path", "") or os.getenv("MMC_LOCAL_CONFIG_PATH", "")
        if config_path:
            kcfg.local_config_path = config_path

    @classmethod
    def _set_device_count_for_single_container(cls, config: "NodeManagerConfig"):
        """Set device count for single container mode using parallel_config.world_size"""
        device_count = config.basic_config.parallel_config.world_size
        if device_count > 0:
            logger.info("Single container mode: using world_size %d from parallel_config", device_count)
            config.basic_config.device_num = device_count
        else:
            logger.warning("Single container mode: world_size is 0, falling back to single_container_config.device_num")
            config.basic_config.device_num = config.single_container_config.device_num or 0

    @classmethod
    def _set_device_count_for_normal_mode(cls, config: "NodeManagerConfig", raw: dict):
        """Set device count for normal mode using motor_deploy_config"""
        deploy_config = raw["motor_deploy_config"]
        if Env.role == "encode":
            device_count = deploy_config.get("e_pod_npu_num", 0)
        elif Env.role == "prefill":
            device_count = deploy_config.get("p_pod_npu_num", 0)
        elif Env.role == "decode":
            device_count = deploy_config.get("d_pod_npu_num", 0)
        elif Env.role in ("union", "both"):
            device_count = deploy_config.get("hybrid_pod_npu_num", 0)
        else:
            device_count = 0

        if device_count > 0:
            logger.info("Using %d devices from config for role %s", device_count, Env.role)
            config.basic_config.device_num = device_count
        else:
            logger.warning("No device count found in config for role %s", Env.role)
            config.basic_config.device_num = 0

    @classmethod
    def _generate_endpoint_ports(cls, config: "NodeManagerConfig"):
        """
        Calculate endpoint number based on tensor parallel & pipeline parallel config.
        Example: tp=2, pp=4 => 8 devices per pod
        """
        dp = config.basic_config.parallel_config.dp_size
        devices_per_dp = config.basic_config.parallel_config.local_world_size

        # only enable multi endpoints should check device count
        if (config.basic_config.enable_multi_endpoints and config.basic_config.device_num < devices_per_dp) or dp < 1:
            raise ValueError(
                f"Device count ({config.basic_config.device_num}) must bigger than "
                f"or equal to devices per dp ({devices_per_dp}) "
                f"and dp must be bigger than 0"
            )

        if not config.basic_config.enable_multi_endpoints:
            config.endpoint_config.endpoint_num = 1
        else:
            config.endpoint_config.endpoint_num = min(dp, config.basic_config.device_num // devices_per_dp)
        config.endpoint_config.service_ports = [
            str(config.endpoint_config.base_port + i * 2) for i in range(config.endpoint_config.endpoint_num)
        ]
        config.endpoint_config.mgmt_ports = [
            str(config.endpoint_config.base_port + i * 2 + 1) for i in range(config.endpoint_config.endpoint_num)
        ]

        logger.info(
            "Generate endpoint ports successfully: endpoint_num: %d, mgmt_ports: %s, service_ports: %s.",
            config.endpoint_config.endpoint_num,
            config.endpoint_config.mgmt_ports,
            config.endpoint_config.service_ports,
        )

    def validate_config(self) -> None:
        """Validate the validity of configuration values"""
        errors = []

        # Validate API configuration
        if self.api_config.node_manager_port <= 0 or self.api_config.node_manager_port > 65535:
            errors.append("node_manager_port must be in range 1-65535")

        # Validate network configuration
        if self.endpoint_config.base_port < 0 or self.endpoint_config.base_port > 65535:
            errors.append("base_port must be in range 0-65535")

        if self.endpoint_config.endpoint_num < 0:
            errors.append("endpoint_num cannot be negative")

        # Validate device configuration
        if self.basic_config.heartbeat_interval_seconds <= 0:
            errors.append("heartbeat_interval_seconds must be greater than 0")

        # Validate logging configuration
        valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
        if self.logging_config.log_level.upper() not in valid_log_levels:
            errors.append(f"log_level must be one of: {', '.join(valid_log_levels)}")

        if self.logging_config.log_max_line_length <= 0:
            errors.append("log_max_line_length must be greater than 0")

        raise_if_config_errors(errors)

    def reload(self) -> bool:
        """Reload configuration from files"""
        return reload_dataclass_config_from_json(
            self,
            NodeManagerConfig.from_json,
            skip=frozenset({"config_path", "last_modified"}),
            skip_private=False,
            success_message="NodeManager configuration reload successful",
            error_message="Failed to reload NodeManager configuration: %s",
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration to dictionary with grouped structure"""

        # Use dataclasses.asdict to automatically serialize all config objects
        config_dict = asdict(self)

        # Handle BaseModel objects that can't be serialized by asdict
        if hasattr(self.basic_config.parallel_config, "model_dump"):
            config_dict["basic_config"]["parallel_config"] = self.basic_config.parallel_config.model_dump()

        # Remove internal fields that shouldn't be in the output
        config_dict.pop("config_path", None)
        config_dict.pop("last_modified", None)
        config_dict["basic_config"].pop(DISPATCH_CAPABILITIES_KEY, None)

        return config_dict

    def save_to_json(self, config_path: str | None = None) -> bool:
        """Save configuration to JSON files"""
        return save_instance_config_to_json(
            self,
            config_path,
            config_key=ConfigKey.MOTOR_NODEMANAGER,
            file_encoding=FILE_ENCODING,
            component_name="node manager",
            missing_path_message="Save paths not specified",
        )

    def get_config_summary(self) -> str:
        """Get configuration summary information"""
        title = " " * 22 + "NodeManager Configuration Summary"
        return (
            format_config_summary_header(title) + "  Logging Configuration:\n"
            f"    ├─ Log Level:           {self.logging_config.log_level}\n"
            f"    ├─ Log File:            {self.logging_config.host_log_dir}\n"
            f"    └─ Log Max Line Length: {self.logging_config.log_max_line_length}\n"
            "\n"
            "  Network Configuration:\n"
            f"    ├─ Node Manager Port:   {self.api_config.node_manager_port}\n"
            f"    ├─ Pod IP:              {self.api_config.pod_ip}\n"
            f"    └─ TLS:                 {'Enabled' if self.mgmt_tls_config.enable_tls else 'Disabled'}\n"
            "\n"
            "  Basic Configuration:\n"
            f"    ├─ Job Name:            {self.basic_config.job_name}\n"
            f"    ├─ Role:                {self.basic_config.role}\n"
            f"    ├─ Model:               {self.basic_config.model_name}\n"
            f"    ├─ Device Count:        {self.basic_config.device_num}\n"
            f"    ├─ Multi Endpoints:     {self.basic_config.enable_multi_endpoints}\n"
            f"    ├─ Endpoint Count:      {self.endpoint_config.endpoint_num}\n"
            f"    └─ Hardware Type:       {self.basic_config.hardware_type}\n"
            "\n"
            "  Snapshot Configuration:\n"
            f"    ├─ Snapshot Enable:     "
            f"{'Enabled' if self.snapshot_config.enable_snapshot else 'Disabled'}\n"
            f"    └─ Metadata Path:     {self.snapshot_config.snapshot_metadata_path or '(default)'}\n"
            "\n"
            "  Parallel Configuration:\n"
            f"    ├─ TP Size:          TP={self.basic_config.parallel_config.tp_size}\n"
            f"    ├─ PP Size:          PP={self.basic_config.parallel_config.pp_size}\n"
            f"    ├─ DP Size:          DP={self.basic_config.parallel_config.dp_size}\n"
            f"    ├─ EP Size:          EP={self.basic_config.parallel_config.ep_size}\n"
            f"    ├─ PCP Size:         PCP={self.basic_config.parallel_config.pcp_size}\n"
            f"    └─ World Size:       World Size={self.basic_config.parallel_config.world_size}\n"
            "\n"
            "  KV Cache Store Configuration:\n"
            f"    ├─ Enabled:              {self.kv_cache_store_config.enable}\n"
            f"    ├─ Backend:              {self.kv_cache_store_config.backend}\n"
            f"    ├─ Service:              {self.kv_cache_store_config.service or '(env: KVS_MASTER_SERVICE)'}\n"
            f"    ├─ Mode:                 {self.kv_cache_store_config.local_service_mode or '(default)'}\n"
            f"    ├─ DRAM Size:            {self.kv_cache_store_config.dram_size or '(default)'}\n"
            f"    ├─ Port:                 {self.kv_cache_store_config.port}\n"
            f"    ├─ ConfigStore Port:     {self.kv_cache_store_config.config_store_port}\n"
            f"    └─ Local Config Path:    {self.kv_cache_store_config.local_config_path}\n"
            f"{'=' * 80}"
        )
