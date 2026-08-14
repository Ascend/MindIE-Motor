# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

# MindIE is licensed under Mulan PSL v2.

# You can use this software according to the terms and conditions of the Mulan PSL v2.

# You may obtain a copy of Mulan PSL v2 at:

#         http://license.coscl.org.cn/MulanPSL2

# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,

# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,

# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.

# See the Mulan PSL v2 for more details.


import importlib
from typing import Any

from motor.common.logger import get_logger
from motor.node_manager.core.services.native_engine.backends.base import IConfig

logger = get_logger(__name__)


class EndpointFactory:
    _CREATOR_MAP: dict[str, str] = {
        "vllm": "motor.engine_server.core.vllm.vllm_endpoint.VLLMEndpoint",
        "sglang": "motor.engine_server.core.sglang.sglang_endpoint.SGLangEndpoint",
    }

    def get_infer_endpoint(self, config: IConfig) -> Any:
        engine_type = config.get_endpoint_config().engine_type
        target = self._CREATOR_MAP.get(engine_type)
        if not target:
            supported_types = list(self._CREATOR_MAP.keys())
            raise ValueError(f"Unsupported engine type: {engine_type}. Supported types are: {supported_types}.")

        try:
            module_path, class_name = target.rsplit(".", 1)
            module = importlib.import_module(module_path)
            creator = getattr(module, class_name)
            return creator(config)
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Failed to load Endpoint for {engine_type}") from e
