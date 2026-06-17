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
import sys
import json
from typing import Any
from dataclasses import dataclass, field

from vllm.entrypoints.openai.cli_args import make_arg_parser, validate_parsed_serve_args

from motor.config.endpoint import EndpointConfig
from motor.engine_server.core.config import IConfig
from motor.common.logger import get_logger
from motor.engine_server.constants import constants

logger = get_logger(__name__)


def _add_argument_to_list(arg_list: list, key: str, value: Any):
    if isinstance(value, bool):
        if value:
            arg_list.append(f"--{key}")
    elif isinstance(value, list):
        if value:
            arg_list.append(f"--{key}")
            for item in value:
                arg_list.append(str(item))
    elif isinstance(value, dict):
        arg_list.append(f"--{key}")
        arg_list.append(json.dumps(value))
    else:
        arg_list.append(f"--{key}")
        arg_list.append(str(value))


def _get_default_mapping() -> dict[str, str]:
    return {
        'model_path': 'model',
        'model_name': 'served_model_name',
        'npu_mem_utils': 'gpu_memory_utilization',
        'dp_size': 'data_parallel_size',
        'tp_size': 'tensor_parallel_size',
        'pp_size': 'pipeline_parallel_size',
        'enable_ep': 'enable_expert_parallel',
        'dp_rpc_port': 'data_parallel_rpc_port',
        'cp_kv_cache_interleave_size': 'cp_kv_cache_interleave_size',
    }


@dataclass
class VLLMConfig(IConfig):
    args: argparse.Namespace | None = None
    data_parallel_address: str | None = None
    data_parallel_rpc_port: int | None = None
    kv_transfer_config: str | None = None
    _d2d_source: list | None = None
    mapping: dict[str, str] | None = field(default_factory=_get_default_mapping)
    endpoint_config: EndpointConfig | None = None

    def initialize(self):
        role = self.endpoint_config.role
        if self.endpoint_config.deploy_config.get_parallel_config(role).dp_size > 1:
            self.data_parallel_address = self.endpoint_config.master_dp_ip
            self.data_parallel_rpc_port = self.endpoint_config.deploy_config.get_parallel_config(role).dp_rpc_port
        if role == constants.PREFILL_ROLE or role == constants.DECODE_ROLE:
            self._process_kv_transfer_config()
        self._process_d2d_config()

    def validate(self):
        if self.args is not None:
            validate_parsed_serve_args(self.args)

    def convert(self):
        arg_list = self._get_param_list()
        logger.info(f'engine server parsed arg_list: {arg_list}')

        sys.argv = ["serve"] + arg_list

        try:
            from vllm.utils import FlexibleArgumentParser
        except ImportError:
            from vllm.utils.argparse_utils import FlexibleArgumentParser
        parser = FlexibleArgumentParser(description="vLLM parser")
        parser = make_arg_parser(parser)
        self.args = parser.parse_args()

    def get_args(self) -> argparse.Namespace:
        return self.args

    def get_endpoint_config(self) -> EndpointConfig:
        return self.endpoint_config

    def _process_kv_transfer_config(self):
        role = self.endpoint_config.role
        if role == constants.UNION_ROLE:
            return

        kv_config = self.endpoint_config.deploy_config.engine_config.get(constants.KV_TRANSFER_CONFIG)
        if kv_config is None:
            raise ValueError(f"{constants.KV_TRANSFER_CONFIG} is None in engine_config")
        try:
            if kv_config[constants.KV_CONNECTOR] == constants.MULTI_CONNECTOR:
                self._process_multi_connector(kv_config)
            else:
                self._process_mooncake_connector(kv_config, add_engine_id=True)

            self.kv_transfer_config = json.dumps(kv_config)
        except Exception as e:
            logger.error(f"Failed to process kv_transfer_config: {e}")
            raise ValueError(f"Failed to process kv_transfer_config: {e}") from e

    def _process_multi_connector(self, kv_config):
        role = self.endpoint_config.role
        if role == constants.PREFILL_ROLE:
            kv_config[constants.KV_ROLE] = constants.KV_PRODUCER
        elif role == constants.DECODE_ROLE:
            kv_config[constants.KV_ROLE] = constants.KV_CONSUMER
        kv_config[constants.ENGINE_ID] = str(self.endpoint_config.instance_id)
        if constants.KV_CONNECTOR_EXTRA_CONFIG not in kv_config:
            raise ValueError("KV connector extra config missing from multi connector")
        connectors = kv_config[constants.KV_CONNECTOR_EXTRA_CONFIG][constants.CONNECTORS]
        if len(connectors) < 2:
            raise ValueError("KV connector extra config at least have 2 connectors")
        self._process_mooncake_connector(connectors[0], add_engine_id=False)
        self._process_store_connector(connectors[1])

    def _process_mooncake_connector(self, kv_config, add_engine_id: bool = True):
        role = self.endpoint_config.role
        if role == constants.PREFILL_ROLE:
            kv_config[constants.KV_ROLE] = constants.KV_PRODUCER
        elif role == constants.DECODE_ROLE:
            kv_config[constants.KV_ROLE] = constants.KV_CONSUMER
        if add_engine_id:
            kv_config[constants.ENGINE_ID] = str(self.endpoint_config.instance_id)

        prefill_parallel = self.endpoint_config.deploy_config.get_parallel_config(constants.KV_PREFILL)
        decode_parallel = self.endpoint_config.deploy_config.get_parallel_config(constants.KV_DECODE)

        if constants.KV_CONNECTOR_EXTRA_CONFIG not in kv_config:
            kv_config[constants.KV_CONNECTOR_EXTRA_CONFIG] = {}

        kv_config[constants.KV_CONNECTOR_EXTRA_CONFIG][constants.KV_PREFILL] = {
            constants.DP_SIZE: prefill_parallel.dp_size,
            constants.TP_SIZE: prefill_parallel.tp_size,
            constants.PP_SIZE: prefill_parallel.pp_size,
        }
        kv_config[constants.KV_CONNECTOR_EXTRA_CONFIG][constants.KV_DECODE] = {
            constants.DP_SIZE: decode_parallel.dp_size,
            constants.TP_SIZE: decode_parallel.tp_size,
            constants.PP_SIZE: decode_parallel.pp_size,
        }

    def _process_store_connector(self, kv_config):
        role = self.endpoint_config.role
        if role == constants.PREFILL_ROLE:
            kv_config[constants.KV_ROLE] = constants.KV_PRODUCER
        elif role == constants.DECODE_ROLE:
            kv_config[constants.KV_ROLE] = constants.KV_CONSUMER

        if kv_config[constants.KV_CONNECTOR] == constants.MOON_CAKE_STORE_V1:
            kv_config[constants.KV_CONNECTOR_EXTRA_CONFIG][constants.MOON_CAKE_RPC_PORT] = str(
                self.endpoint_config.instance_id
            )
        elif kv_config[constants.KV_CONNECTOR] == constants.ASCEND_STORE_CONNECTOR:
            kv_config[constants.KV_CONNECTOR_EXTRA_CONFIG][constants.LOOKUP_RPC_PORT] = str(
                self.endpoint_config.instance_id
            )
        else:
            raise ValueError(f"{constants.KV_CONNECTOR} is not supported")

    def _process_d2d_config(self):
        d2d_peer_ips = self.endpoint_config.d2d_peer_ips
        if d2d_peer_ips is None:
            return

        engine_cfg = self.endpoint_config.deploy_config.engine_config
        ml_extra = engine_cfg.configs.get("model_loader_extra_config")
        if not isinstance(ml_extra, dict):
            return
        source = ml_extra.get("source") or ml_extra.get("SOURCE")
        if source != "auto":
            return
        listen_port = ml_extra.get("listen_port") if "listen_port" in ml_extra else ml_extra.get("LISTEN_PORT")
        if listen_port is None:
            return

        peer_ips = [ip.strip() for ip in d2d_peer_ips.split(",") if ip.strip()]
        if not peer_ips:
            logger.warning("D2D peer IPs is empty, entering seed mode (load from disk, serve peers)")
            return
        role = self.endpoint_config.role
        parallel_config = self.endpoint_config.deploy_config.get_parallel_config(role)
        local_world_size = parallel_config.local_world_size
        dp_rank = self.endpoint_config.dp_rank
        offset = dp_rank * local_world_size
        self._d2d_source = [
            {"device_id": offset + rank, "sources": [f"{ip}:{int(listen_port) + offset + rank}" for ip in peer_ips]}
            for rank in range(local_world_size)
        ]
        logger.info("D2D peer SOURCE: %s", self._d2d_source)

    def _flatten_config(self) -> dict[str, Any]:
        """
        Flatten deploy_config into a simple key-value dictionary with the following rules:
        1. Include all key-value pairs from engine_config
        2. For other fields, only include those defined in self.mapping
        3. Use the value from mapping as the final key name
        4. If there's a conflict between engine_config and model_config, engine_config takes precedence
        """
        flattened = {}

        deploy_config = self.endpoint_config.deploy_config

        flattened.update(deploy_config.engine_config.configs)

        model_config = deploy_config.model_config
        for server_key, vllm_key in self.mapping.items():
            if hasattr(model_config, server_key):
                value = getattr(model_config, server_key)
                if value is not None:
                    flattened.setdefault(vllm_key, value)

        role = self.endpoint_config.role
        parallel_config = deploy_config.get_parallel_config(role)
        for server_key, vllm_key in self.mapping.items():
            if hasattr(parallel_config, server_key):
                value = getattr(parallel_config, server_key)
                if value is not None:
                    flattened.setdefault(vllm_key, value)

        if parallel_config.pcp_size > 1:
            flattened.setdefault("prefill_context_parallel_size", parallel_config.pcp_size)

        flattened.update({"host": self.endpoint_config.host, "port": self.endpoint_config.port})
        if self.data_parallel_address is not None:
            flattened["data_parallel_address"] = self.data_parallel_address
            flattened["data_parallel_rpc_port"] = self.data_parallel_rpc_port
            flattened["data_parallel_rank"] = self.endpoint_config.dp_rank

        # Cross-node PCP: detect nnodes > 1 and master_port (or master-port) in engine_config
        engine_nnodes = deploy_config.engine_config.get("nnodes", 1)
        # User may write "master_port" or "master-port" (vLLM native style) in engine_config
        engine_master_port = deploy_config.engine_config.get("master_port", None)
        if engine_master_port is None:
            engine_master_port = deploy_config.engine_config.get("master-port", None)
        logger.info(
            "Cross-node PCP detection: nnodes=%s (type=%s), master_port=%s, node_rank=%d, master_dp_ip=%s",
            engine_nnodes,
            type(engine_nnodes).__name__,
            engine_master_port,
            self.endpoint_config.node_rank,
            self.endpoint_config.master_dp_ip,
        )
        try:
            engine_nnodes_int = int(engine_nnodes)
        except (TypeError, ValueError):
            engine_nnodes_int = 1
        if engine_nnodes_int > 1 and engine_master_port is not None:
            node_rank = self.endpoint_config.node_rank
            master_dp_ip = self.endpoint_config.master_dp_ip
            logger.info("Cross-node PCP active: node_rank=%d, master_addr=%s", node_rank, master_dp_ip)
            flattened.setdefault("node_rank", node_rank)
            if master_dp_ip:
                flattened.setdefault("master_addr", master_dp_ip)
            if node_rank != 0:
                flattened.setdefault("headless", True)
        else:
            logger.info(
                "Cross-node PCP NOT active: nnodes_int=%d, master_port=%s",
                engine_nnodes_int,
                engine_master_port,
            )

        if self.kv_transfer_config is not None:
            flattened["kv_transfer_config"] = self.kv_transfer_config
        ml_extra = deploy_config.engine_config.configs.get("model_loader_extra_config")
        _KEY_MAP = {
            "source": "SOURCE",
            "listen_port": "LISTEN_PORT",
            "model": "MODEL",
            "int8_cache": "INT8_CACHE",
            "int8_cache_name": "INT8_CACHE_NAME",
            "output_prefix": "OUTPUT_PREFIX",
        }

        d2d_configured = (
            isinstance(ml_extra, dict)
            and (ml_extra.get("source") or ml_extra.get("SOURCE")) == "auto"
            and (ml_extra.get("listen_port") if "listen_port" in ml_extra else ml_extra.get("LISTEN_PORT")) is not None
        )

        if d2d_configured:
            ml_extra = {_KEY_MAP.get(k, k): v for k, v in ml_extra.items()}
            if self._d2d_source is not None:
                ml_extra["SOURCE"] = self._d2d_source
            else:
                ml_extra.pop("SOURCE", None)
            ml_extra.setdefault("MODEL", deploy_config.model_config.model_name)
            flattened["model_loader_extra_config"] = json.dumps(ml_extra)
            flattened["load_format"] = "netloader"
        elif isinstance(ml_extra, dict):
            ml_extra = {_KEY_MAP.get(k, k): v for k, v in ml_extra.items()}
            flattened["model_loader_extra_config"] = json.dumps(ml_extra)

        return flattened

    def _get_param_list(self) -> list[str]:
        processed_args = []

        flattened_config = self._flatten_config()

        for key, value in flattened_config.items():
            formatted_key = key.replace('_', '-')
            _add_argument_to_list(processed_args, formatted_key, value)

        return processed_args
