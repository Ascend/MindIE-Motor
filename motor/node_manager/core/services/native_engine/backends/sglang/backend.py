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
from motor.common.resources.instance import PDRole
from motor.node_manager.core.services.native_engine.backends.base import BaseNativeEngineBackend
from motor.node_manager.core.services.native_engine.models import CommandSpec, LaunchContext, LaunchSpec

logger = get_logger(__name__)

SGLANG_HEALTH_ENDPOINT_GENERATION_ENV = "SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION"

# SGLang ZBAL default rendezvous (pipeline-parallel bootstrap).
# Upstream env: SGLANG_ZBAL_BOOTSTRAP_URL; default port 24691.
SGLANG_ZBAL_BOOTSTRAP_URL_ENV = "SGLANG_ZBAL_BOOTSTRAP_URL"
SGLANG_ZBAL_BOOTSTRAP_PORT = 24691


class SGLangBackend(BaseNativeEngineBackend):
    """Build and validate native SGLang launch specifications."""

    engine_type = "sglang"
    command_prefix = ("python3", "-m", "sglang.launch_server")

    def _validate_context(self, context: LaunchContext) -> None:
        if context.role == PDRole.ROLE_E:
            raise ValueError("SGLang encode role is not supported")

    def prepare(self, context: LaunchContext) -> LaunchSpec:
        launch_spec = super().prepare(context)

        # Pin generative /health on; container env or enable_virtual_inference=false must not disable it.
        env = dict(launch_spec.command.env)
        env[SGLANG_HEALTH_ENDPOINT_GENERATION_ENV] = "true"

        # Cross-node PP/ZBAL: all ranks must rendezvous on Controller-provided master_dp_ip,
        # not each pod's own POD_IP from a static env.json value.
        if context.master_dp_ip:
            env[SGLANG_ZBAL_BOOTSTRAP_URL_ENV] = f"tcp://{context.master_dp_ip}:{SGLANG_ZBAL_BOOTSTRAP_PORT}"
            logger.info(
                "ZBAL bootstrap URL overridden to %s (master_dp_ip)",
                env[SGLANG_ZBAL_BOOTSTRAP_URL_ENV],
            )

        return LaunchSpec(
            command=CommandSpec(
                argv=launch_spec.command.argv,
                env=env,
                cwd=launch_spec.command.cwd,
            ),
            probe=launch_spec.probe,
            deploy_config=launch_spec.deploy_config,
        )
