# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from motor.common.logger import get_logger
from motor.common.resources.dispatch import DISPATCH_PROFILE_KEY
from motor.config.config_utils import _update_engine_server_tls_config
from motor.config.resolver import ConfigResolver, normalize_keys
from motor.config.tls_config import TLSConfig
from motor.engine_server.constants import constants
from motor.engine_server.utils.ip import ip_valid_check, port_valid_check
from motor.engine_server.utils.validators import FileValidator

logger = get_logger(__name__)

supported_engine = ["vllm", "sglang"]
supported_role = ["encode", "prefill", "decode", "union"]

MOTOR_ENGINE_ENCODE_CONFIG_KEY = "motor_engine_encode_config"
MOTOR_ENGINE_PREFILL_CONFIG_KEY = "motor_engine_prefill_config"
MOTOR_ENGINE_DECODE_CONFIG_KEY = "motor_engine_decode_config"
MOTOR_ENGINE_UNION_CONFIG_KEY = "motor_engine_union_config"
ENCODE_PARALLEL_CONFIG_KEY = "encode_parallel_config"
PREFILL_PARALLEL_CONFIG_KEY = "prefill_parallel_config"
DECODE_PARALLEL_CONFIG_KEY = "decode_parallel_config"


@dataclass
class ParallelConfig:
    """Configuration for parallel processing (both prefill and decode)"""

    dp_size: int = field(default=1)
    tp_size: int = field(default=1)
    pp_size: int = field(default=1)
    pcp_size: int = field(default=1)
    world_size: int | None = field(default=None)
    local_world_size: int | None = field(default=None)
    enable_ep: bool = field(default=False)
    dp_rpc_port: int = field(default=9000)
    cp_kv_cache_interleave_size: int = field(default=1)

    def __post_init__(self):
        if self.world_size is None:
            self.world_size = self.dp_size * self.pcp_size * self.tp_size * self.pp_size
        if self.local_world_size is None:
            self.local_world_size = self.pcp_size * self.tp_size * self.pp_size

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParallelConfig":
        return cls(**data)


@dataclass
class ModelConfig:
    """Configuration for the model itself"""

    model_name: str
    model_path: str
    npu_mem_utils: float
    encode_parallel_config: ParallelConfig
    prefill_parallel_config: ParallelConfig
    decode_parallel_config: ParallelConfig

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelConfig":
        return cls(
            model_name=data["model_name"],
            model_path=data["model_path"],
            npu_mem_utils=data["npu_mem_utils"],
            encode_parallel_config=ParallelConfig.from_dict(data.get(ENCODE_PARALLEL_CONFIG_KEY, {})),
            prefill_parallel_config=ParallelConfig.from_dict(data[PREFILL_PARALLEL_CONFIG_KEY]),
            decode_parallel_config=ParallelConfig.from_dict(data[DECODE_PARALLEL_CONFIG_KEY]),
        )


@dataclass
class EngineConfig:
    """Configuration for the engine with dynamic key-value pairs"""

    configs: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EngineConfig":
        """Parse EngineConfig from a dictionary (stores dynamic key-value pairs directly)"""
        return cls(configs=normalize_keys(data))

    def get(self, key: str, default: Any = None) -> Any:
        return self.configs.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.configs[key] = value


@dataclass
class HealthCheckConfig:
    """Configuration for health check"""

    health_collector_timeout: int = 5
    # Max attempts for /health probe when request times out (timeout-only retry).
    health_collector_timeout_retry_attempts: int = 3
    npu_usage_threshold: int = 3
    enable_virtual_inference: bool = False
    max_failure_count: int = 6

    @staticmethod
    def _as_positive_int(name: str, value: Any) -> int:
        # bool is a subclass of int; reject it to avoid true/false silently becoming 1/0.
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer, got {value!r}")
        if value < 1:
            raise ValueError(f"{name} must be >= 1, got {value}")
        return value

    def __post_init__(self):
        self.health_collector_timeout = self._as_positive_int("health_collector_timeout", self.health_collector_timeout)
        self.health_collector_timeout_retry_attempts = self._as_positive_int(
            "health_collector_timeout_retry_attempts", self.health_collector_timeout_retry_attempts
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HealthCheckConfig":
        return cls(**data)


@dataclass
class DeployConfig:
    """Root configuration class representing the entire JSON structure"""

    engine_type: str
    model_config: ModelConfig
    engine_config: EngineConfig
    mgmt_tls_config: TLSConfig | None
    infer_tls_config: TLSConfig | None
    dispatch_profile: str | None = None
    health_check_config: HealthCheckConfig = field(default_factory=HealthCheckConfig)
    enable_multi_endpoints: bool = True

    @classmethod
    def load(cls, file_path: str | Path, role: str | None = None) -> "DeployConfig":
        """
        Load configuration from a JSON file and parse into a DeployConfig instance

        :param file_path: Path to the JSON file
        :return: Parsed DeployConfig instance
        """
        with open(file_path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
        data = raw_data
        if isinstance(raw_data, dict) and (
            MOTOR_ENGINE_ENCODE_CONFIG_KEY in raw_data
            or MOTOR_ENGINE_PREFILL_CONFIG_KEY in raw_data
            or MOTOR_ENGINE_DECODE_CONFIG_KEY in raw_data
            or MOTOR_ENGINE_UNION_CONFIG_KEY in raw_data
        ):
            key_map = {
                "encode": MOTOR_ENGINE_ENCODE_CONFIG_KEY,
                "prefill": MOTOR_ENGINE_PREFILL_CONFIG_KEY,
                "decode": MOTOR_ENGINE_DECODE_CONFIG_KEY,
                "union": MOTOR_ENGINE_UNION_CONFIG_KEY,
            }
            data = raw_data.get(key_map.get(role, ""), {})
            _update_engine_server_tls_config(data, raw_data)

            resolver = ConfigResolver(data)

            if resolver.has_model_config():
                _msg = (
                    "model_config is deprecated and will be removed in a future version. "
                    "Please move your configuration directly into engine_config."
                )
                logger.warning(_msg)

            if MOTOR_ENGINE_UNION_CONFIG_KEY in raw_data:
                prefill_resolver = resolver
                decode_resolver = resolver
                encode_resolver = None
            else:
                prefill_section = raw_data.get(MOTOR_ENGINE_PREFILL_CONFIG_KEY, {})
                decode_section = raw_data.get(MOTOR_ENGINE_DECODE_CONFIG_KEY, {})
                prefill_resolver = ConfigResolver(prefill_section)
                decode_resolver = ConfigResolver(decode_section)

                # encode section is optional — only for EPD architecture
                encode_section = raw_data.get(MOTOR_ENGINE_ENCODE_CONFIG_KEY)
                encode_resolver = (
                    ConfigResolver(encode_section)
                    if (isinstance(encode_section, dict) and encode_section.get("engine_type"))
                    else None
                )

            model_config = ModelConfig(
                model_name=resolver.get_model_name(""),
                model_path=resolver.get_model_path(""),
                npu_mem_utils=resolver.get_npu_mem_utils(0.9),
                encode_parallel_config=ParallelConfig(**encode_resolver.get_parallel_config())
                if encode_resolver
                else ParallelConfig(),
                prefill_parallel_config=ParallelConfig(**prefill_resolver.get_parallel_config()),
                decode_parallel_config=ParallelConfig(**decode_resolver.get_parallel_config()),
            )
        else:
            model_config = ModelConfig.from_dict(data.get("model_config", {}))
            resolver = ConfigResolver(data, engine_type=data.get("engine_type"))

        mgmt_tls_config = data.get("mgmt_tls_config")
        infer_tls_config = data.get("infer_tls_config")

        enable_multi_endpoints = data.get("enable_multi_endpoints")
        if enable_multi_endpoints is None:
            enable_multi_endpoints = resolver.get_enable_multi_endpoints()

        return cls(
            engine_type=data["engine_type"],
            model_config=model_config,
            engine_config=EngineConfig.from_dict(data.get("engine_config", {})),
            mgmt_tls_config=TLSConfig.from_dict(mgmt_tls_config) if mgmt_tls_config else None,
            infer_tls_config=TLSConfig.from_dict(infer_tls_config) if infer_tls_config else None,
            dispatch_profile=data.get(DISPATCH_PROFILE_KEY),
            health_check_config=HealthCheckConfig.from_dict(data.get("health_check_config", {})),
            enable_multi_endpoints=bool(enable_multi_endpoints),
        )

    def get_parallel_config(self, role: str = "union") -> ParallelConfig:
        """
        Get the parallel configuration based on the role.

        :param role: Role for parallel config:
            - "union" or "prefill": Get prefill_parallel_config
            - "decode": Get decode_parallel_config
        :return: Corresponding ParallelConfig instance
        """
        if role in ("union", "prefill"):
            return self.model_config.prefill_parallel_config
        elif role == "decode":
            return self.model_config.decode_parallel_config
        elif role == "encode":
            return self.model_config.encode_parallel_config
        else:
            raise ValueError(f"Unsupported role: {role}. Allowed values: 'union', 'prefill', 'decode', 'encode'")


@dataclass
class EndpointConfig:
    engine_type: str = "vllm"
    host: str = "127.0.0.1"
    role: str = "union"
    kv_port: int | None = None
    lookup_rpc_port: int | None = None
    master_dp_ip: str | None = None
    dp_rpc_port: int | None = None
    port: int = 8000
    mgmt_port: int = 9001
    instance_id: int = 0
    dp_rank: int = 0
    node_rank: int = 0
    config_path: str | None = None
    d2d_peer_ips: str | None = None
    deploy_config: DeployConfig = None

    snapshot_metadata: str | None = None

    @classmethod
    def parse_cli_args(cls) -> argparse.Namespace:
        parser = argparse.ArgumentParser(description="EngineServer - Universal Inference Engine Service")
        parser.add_argument("--host", help="EngineServer endpoint host")
        parser.add_argument("--role", help="PD separate role, prefill/decode/union")
        parser.add_argument("--kv-port", type=int, help="kv port")
        parser.add_argument("--lookup-rpc-port", type=int, help="lookup rpc port")
        parser.add_argument("--master-dp-ip", type=str, help="Master DP ip for distributed setup")
        parser.add_argument("--dp-rpc-port", type=int, help="dp rpc port")
        parser.add_argument("--port", type=int, help="EngineServer business interface port")
        parser.add_argument("--mgmt-port", type=int, dest="mgmt_port", help="EngineServer management interface port")
        parser.add_argument("--instance-id", type=int, default=0, help="Engine instance id")
        parser.add_argument("--dp-rank", type=int, default=0, help="DP parallel rank")
        parser.add_argument("--node-rank", type=int, default=0, help="PCP node rank (assigned by Motor Controller)")
        parser.add_argument("--config-path", help="Path to engine-specific configuration file (JSON format)")
        parser.add_argument(
            "--d2d-peer-ips",
            type=str,
            default=None,
            help="Comma-separated IPs of peer instances for D2D weight transfer",
        )
        parser.add_argument(
            "--snapshot-metadata",
            default=None,
            help="Snapshot metadata file (JSON format), enable snapshot function",
        )
        return parser.parse_args()

    @classmethod
    def init_endpoint_config(cls) -> 'EndpointConfig':
        cli_args = cls.parse_cli_args()
        endpoint_config = cls(
            host=cli_args.host,
            role=cli_args.role,
            kv_port=cli_args.kv_port,
            lookup_rpc_port=cli_args.lookup_rpc_port,
            master_dp_ip=cli_args.master_dp_ip,
            dp_rpc_port=cli_args.dp_rpc_port,
            port=cli_args.port,
            mgmt_port=cli_args.mgmt_port,
            instance_id=cli_args.instance_id,
            config_path=cli_args.config_path,
            dp_rank=cli_args.dp_rank,
            d2d_peer_ips=cli_args.d2d_peer_ips,
            node_rank=cli_args.node_rank,
            snapshot_metadata=cli_args.snapshot_metadata,
        )
        endpoint_config.validate()
        endpoint_config.load_deploy_config()
        return endpoint_config

    def validate(self):
        if self.role not in supported_role:
            raise ValueError(f"role {self.role} is not supported.")
        if self.instance_id < 0:
            raise ValueError(f"instance_id {self.instance_id} illegal.")
        ip_valid_check(self.host)
        port_valid_check(int(self.port))
        port_valid_check(int(self.mgmt_port))
        if self.dp_rank < 0 or self.dp_rank > 65535:
            raise ValueError(f"{self.dp_rank} is not supported.")
        if not os.path.exists(self.config_path):
            raise ValueError(f"config file {self.config_path} does not exist")
        if not FileValidator(self.config_path).check_not_soft_link().check_file_size().check().is_valid():
            raise ValueError(f"{self.config_path} is not a valid file path.")
        if self.snapshot_metadata is not None:
            if not os.path.exists(self.snapshot_metadata):
                raise ValueError(f"snapshot metadata file {self.snapshot_metadata} does not exist")
            if not FileValidator(self.snapshot_metadata).check_not_soft_link().check_file_size().check().is_valid():
                raise ValueError(f"{self.snapshot_metadata} is not a valid file path")

    def load_deploy_config(self):
        self.deploy_config = DeployConfig.load(self.config_path, role=self.role)
        kv_config = self.deploy_config.engine_config.get(constants.KV_TRANSFER_CONFIG, {})
        if kv_config:
            if kv_config[constants.KV_CONNECTOR] == constants.MULTI_CONNECTOR:
                extra_config = kv_config.get(constants.KV_CONNECTOR_EXTRA_CONFIG)
                connectors = extra_config.get(constants.CONNECTORS) if isinstance(extra_config, dict) else None
                if not isinstance(connectors, list) or len(connectors) < 2:
                    raise ValueError(
                        f"{constants.KV_TRANSFER_CONFIG}.{constants.KV_CONNECTOR_EXTRA_CONFIG}"
                        f".{constants.CONNECTORS} must be a list of at least 2 connectors "
                        f"(transport first, store second) when {constants.KV_CONNECTOR} is "
                        f"{constants.MULTI_CONNECTOR}"
                    )
                if not all(isinstance(connector, dict) for connector in connectors[:2]):
                    raise ValueError(
                        f"{constants.KV_TRANSFER_CONFIG}.{constants.KV_CONNECTOR_EXTRA_CONFIG}"
                        f".{constants.CONNECTORS} entries must be objects (connector configs)"
                    )
                if self.kv_port is not None:
                    connectors[0][constants.KV_PORT] = str(self.kv_port)
                store = connectors[1]
                # UCM store has no lookup_rpc_port; writing one would pollute its inline config.
                # Skip only UCM; other stores keep the original direct write unchanged.
                if store.get(constants.KV_CONNECTOR) != constants.UCM_CONNECTOR and self.lookup_rpc_port is not None:
                    store[constants.KV_CONNECTOR_EXTRA_CONFIG][constants.LOOKUP_RPC_PORT] = str(self.lookup_rpc_port)
            else:
                if self.kv_port is not None:
                    kv_config[constants.KV_PORT] = str(self.kv_port)
        if self.role == "encode" and self.dp_rpc_port is not None:
            self.deploy_config.model_config.encode_parallel_config.dp_rpc_port = self.dp_rpc_port
        if self.role in ("prefill", "union") and self.dp_rpc_port is not None:
            self.deploy_config.model_config.prefill_parallel_config.dp_rpc_port = self.dp_rpc_port
        if self.role == "decode" and self.dp_rpc_port is not None:
            self.deploy_config.model_config.decode_parallel_config.dp_rpc_port = self.dp_rpc_port
        self.engine_type = str(self.deploy_config.engine_type)
        if self.engine_type not in supported_engine:
            raise ValueError(f"engine type {self.engine_type} is not supported.")

    def update_engine_config(self):
        split_str = "*:"
        kv_events_config = self.deploy_config.engine_config.get("kv-events-config", None)
        if kv_events_config is None:
            return
        endpoint = kv_events_config.get("endpoint", None)
        if endpoint is None:
            return
        endpoint_info = endpoint.split(split_str)
        if len(endpoint_info) != 2:
            return
        kv_events_config["endpoint"] = endpoint_info[0] + split_str + str(int(endpoint_info[1]) + self.dp_rank)

        replay_endpoint = kv_events_config.get("replay_endpoint", None)
        if replay_endpoint is None:
            return
        replay_endpoint_info = replay_endpoint.split(split_str)
        if len(replay_endpoint_info) != 2:
            return
        kv_events_config["replay_endpoint"] = (
            replay_endpoint_info[0] + split_str + str(int(replay_endpoint_info[1]) + self.dp_rank)
        )

        self.deploy_config.engine_config.set("kv-events-config", kv_events_config)
