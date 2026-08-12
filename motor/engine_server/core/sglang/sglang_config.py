# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from motor.common.logger import get_logger
from motor.common.utils.net import format_address
from motor.config.endpoint import EndpointConfig
from motor.engine_server.constants import constants
from motor.engine_server.core.config import IConfig

logger = get_logger(__name__)


def _add_argument_to_list(arg_list: list, key: str, value: Any):
    """Append key-value to arg_list as CLI args (e.g. --key value)."""
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


@dataclass
class SGLangConfig(IConfig):
    """SGLang engine configuration for hybrid and PD-disaggregated roles."""

    args: argparse.Namespace | None = None
    endpoint_config: EndpointConfig | None = None

    def initialize(self):
        pass

    def validate(self):
        pass

    def convert(self):
        arg_list = self._get_param_list()
        logger.info("engine server sglang arg_list: %s", arg_list)

        sys.argv = ["serve"] + arg_list
        from sglang.srt.server_args import ServerArgs

        parser = argparse.ArgumentParser()
        ServerArgs.add_cli_args(parser)
        raw_args = parser.parse_args()
        self.args = ServerArgs.from_cli_args(raw_args)

    def get_args(self) -> argparse.Namespace:
        return self.args

    def get_endpoint_config(self) -> EndpointConfig:
        return self.endpoint_config

    def get_cli_args(self) -> list[str]:
        """Return CLI args for native 'sglang.launch_server' command."""
        return self._get_param_list()

    def _flatten_config(self) -> dict[str, Any]:
        """Flatten deploy_config to sglang CLI key-value dict.

        CLI args come from engine_config plus runtime endpoint fields. Legacy
        model_config / parallel_config field remapping is not applied here.
        """
        flattened = {}
        deploy_config = self.endpoint_config.deploy_config
        role = self.endpoint_config.role

        flattened.update(deploy_config.engine_config.configs)

        flattened["host"] = self.endpoint_config.host
        flattened["port"] = self.endpoint_config.port

        if flattened.get("nnodes", 1) > 1:
            parallel_config = deploy_config.get_parallel_config(role)
            flattened["dist-init-addr"] = format_address(self.endpoint_config.master_dp_ip, parallel_config.dp_rpc_port)
            flattened["node-rank"] = self.endpoint_config.node_rank

        if role == constants.PREFILL_ROLE:
            flattened[constants.DISAGGREGATION_MODE] = "prefill"
        elif role == constants.DECODE_ROLE:
            flattened[constants.DISAGGREGATION_MODE] = "decode"
        else:
            flattened[constants.DISAGGREGATION_MODE] = "null"

        return flattened

    def _get_param_list(self) -> list[str]:
        processed_args = []
        flattened_config = self._flatten_config()
        for key, value in flattened_config.items():
            if key in ("engine_type", "kv_transfer_config"):
                continue
            formatted_key = key.replace("_", "-")
            _add_argument_to_list(processed_args, formatted_key, value)
        return processed_args
