# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.resources.instance import PDRole
from motor.node_manager.core.services.native_engine.backends.base import BaseNativeEngineBackend
from motor.node_manager.core.services.native_engine.models import LaunchContext


class SGLangBackend(BaseNativeEngineBackend):
    """Build and validate native SGLang launch specifications."""

    engine_type = "sglang"
    command_prefix = ("python3", "-m", "sglang.launch_server")

    def _validate_context(self, context: LaunchContext) -> None:
        if context.role == PDRole.ROLE_E:
            raise ValueError("SGLang encode role is not supported")
