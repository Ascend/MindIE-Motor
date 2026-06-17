# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.engine_server.core.config import IConfig
from motor.engine_server.core.dispatch_adapter.base import DispatchAdapter
from motor.engine_server.core.dispatch_adapter.sglang_adapter import SGLangDispatchAdapter
from motor.engine_server.core.dispatch_adapter.vllm_adapter import VLLMDispatchAdapter


def create_dispatch_adapter(config: IConfig) -> DispatchAdapter:
    engine_type = config.get_endpoint_config().engine_type
    if engine_type == "vllm":
        return VLLMDispatchAdapter(config)
    if engine_type == "sglang":
        return SGLangDispatchAdapter(config)
    return DispatchAdapter(config)
