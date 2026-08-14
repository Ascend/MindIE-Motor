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

from motor.config.endpoint import EndpointConfig
from motor.node_manager.core.services.native_engine.backends.base import IConfig


class ConfigFactory:
    """Lazily construct engine-specific native CLI configuration adapters."""

    _ENGINE_CONFIG_MAP: dict[str, str] = {
        "vllm": "motor.node_manager.core.services.native_engine.backends.vllm.config.VLLMConfig",
        "sglang": "motor.node_manager.core.services.native_engine.backends.sglang.config.SGLangConfig",
    }

    def __init__(self, endpoint_config: EndpointConfig):
        self.endpoint_config = endpoint_config

    def build_cli_config(self) -> IConfig:
        """Build native CLI configuration without importing engine parsers."""
        engine_type = self.endpoint_config.engine_type
        config_class_path = self._ENGINE_CONFIG_MAP.get(engine_type)

        if not config_class_path:
            supported_types = list(self._ENGINE_CONFIG_MAP.keys())
            raise ValueError(
                f"Unsupported engine type: {engine_type}. "
                f"Supported types are: {supported_types}."
            )

        try:
            module_path, class_name = config_class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            config_class = getattr(module, class_name)

            config_instance = config_class(endpoint_config=self.endpoint_config)
            config_instance.initialize()
            return config_instance
        except (ImportError, AttributeError) as e:
            raise ValueError(f"Failed to load config class for {engine_type}") from e

    def parse(self) -> IConfig:
        """Build and validate the transitional in-process EngineServer configuration."""
        config_instance = self.build_cli_config()
        config_instance.convert()
        config_instance.validate()
        return config_instance
