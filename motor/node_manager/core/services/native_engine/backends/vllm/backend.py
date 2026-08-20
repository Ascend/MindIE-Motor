# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.resources.dispatch import DispatchProfile, classify_vllm_dispatch_profile
from motor.common.resources.instance import PDRole
from motor.config.endpoint import EndpointConfig
from motor.node_manager.core.services.native_engine.backends.base import BaseNativeEngineBackend


class VllmBackend(BaseNativeEngineBackend):
    """Build and validate native ``vllm serve`` launch specifications."""

    engine_type = "vllm"
    command_prefix = ("vllm", "serve")

    def _validate_endpoint_config(self, endpoint_config: EndpointConfig) -> None:
        if endpoint_config.role not in (PDRole.ROLE_P.value, PDRole.ROLE_D.value):
            return
        deploy_config = endpoint_config.deploy_config
        profile = classify_vllm_dispatch_profile(
            deploy_config.engine_config,
            explicit_profile=deploy_config.dispatch_profile,
        )
        if profile not in (DispatchProfile.HANDOFF, DispatchProfile.TRIGGER):
            raise ValueError(
                "Native vLLM P/D launch only supports handoff or trigger connectors; "
                f"resolved dispatch profile is {profile.value}"
            )
