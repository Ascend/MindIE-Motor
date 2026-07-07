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
from abc import ABC, abstractmethod

from motor.common.logger import get_logger
from motor.config.endpoint import EndpointConfig

logger = get_logger(__name__)

supported_engine = ["vllm", "sglang"]
supported_role = ["prefill", "decode", "union"]


class IConfig(ABC):
    @abstractmethod
    def initialize(self):
        pass

    @abstractmethod
    def validate(self):
        pass

    @abstractmethod
    def convert(self):
        pass

    @abstractmethod
    def get_args(self) -> argparse.Namespace | None:
        pass

    @abstractmethod
    def get_endpoint_config(self) -> EndpointConfig | None:
        pass

    @abstractmethod
    def get_cli_args(self) -> list[str]:
        """Return the CLI argument list suitable for native engine launch (vllm serve / sglang.launch_server)."""
        pass
