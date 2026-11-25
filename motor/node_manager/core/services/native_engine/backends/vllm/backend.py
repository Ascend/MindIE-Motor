# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.common.logger import get_logger
from motor.common.resources.dispatch import DispatchProfile, classify_vllm_dispatch_profile
from motor.common.resources.instance import PDRole
from motor.config.endpoint import EndpointConfig
from motor.node_manager.core.services.native_engine.backends.base import BaseNativeEngineBackend
from motor.node_manager.core.services.native_engine.backends.vllm.log_cli_config import (
    apply_log_cli_paths,
    read_engine_versions,
    resolve_log_cli_capabilities,
)
from motor.node_manager.core.services.native_engine.models import LaunchContext

logger = get_logger(__name__)


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

    def _configure_log_cli_paths(self, endpoint_config: EndpointConfig, context: LaunchContext) -> None:
        engine_root = self._get_engine_log_path(context.environment)
        if engine_root is None:
            return
        versions = read_engine_versions()
        capabilities = resolve_log_cli_capabilities(versions)
        logger.info("Native log version rules: %s", versions)
        apply_log_cli_paths(endpoint_config.deploy_config.engine_config.configs, engine_root, capabilities)
