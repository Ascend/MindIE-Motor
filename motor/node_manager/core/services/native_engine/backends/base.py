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
from typing import Protocol

from motor.common.logger import get_logger
from motor.config.endpoint import EndpointConfig
from motor.node_manager.core.services.native_engine.models import (
    CommandSpec,
    LaunchContext,
    LaunchSpec,
    ProbeSpec,
)

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


class NativeEngineBackend(Protocol):
    """Stateless conversion from launch context to a native engine launch specification."""

    engine_type: str

    def prepare(self, context: LaunchContext) -> LaunchSpec:
        """Validate one endpoint and build its immutable launch specification."""
        ...


class BaseNativeEngineBackend:
    """Shared native launch-spec construction for engine-specific backends."""

    engine_type: str
    command_prefix: tuple[str, ...]

    def prepare(self, context: LaunchContext) -> LaunchSpec:
        self._validate_context(context)
        endpoint_config = build_endpoint_config(context, self.engine_type)
        if endpoint_config.engine_type != self.engine_type:
            raise ValueError(
                f"Configured engine type {endpoint_config.engine_type} does not match "
                f"Node Manager engine type {self.engine_type}"
            )
        self._validate_endpoint_config(endpoint_config)

        # Import lazily so ConfigFactory can type against IConfig without a module cycle.
        from motor.node_manager.core.services.native_engine.config_factory import ConfigFactory

        config = ConfigFactory(endpoint_config=endpoint_config).build_cli_config()
        config.convert()
        config.validate()
        health_config = endpoint_config.deploy_config.health_check_config
        return LaunchSpec(
            command=CommandSpec(
                argv=self.command_prefix + tuple(config.get_cli_args()),
                env=context.environment,
            ),
            probe=ProbeSpec(
                path="/health",
                timeout_seconds=float(health_config.health_collector_timeout),
                startup_timeout_seconds=float(health_config.startup_timeout),
                max_attempts=health_config.health_collector_timeout_retry_attempts,
                tls_config=endpoint_config.deploy_config.infer_tls_config,
                process_only=context.headless,
            ),
        )

    def _validate_context(self, context: LaunchContext) -> None:
        pass

    def _validate_endpoint_config(self, endpoint_config: EndpointConfig) -> None:
        pass


def build_endpoint_config(context: LaunchContext, engine_type: str) -> EndpointConfig:
    """Rebuild and validate one role-specific endpoint configuration."""
    endpoint_config = EndpointConfig(
        engine_type=engine_type,
        host=context.host,
        role=context.role.value,
        kv_port=context.kv_port,
        lookup_rpc_port=context.lookup_rpc_port,
        master_dp_ip=context.master_dp_ip,
        dp_rpc_port=context.dp_rpc_port,
        port=context.business_port,
        mgmt_port=context.mgmt_port,
        instance_id=context.instance_id,
        dp_rank=context.dp_rank,
        node_rank=context.node_rank,
        config_path=context.config_path,
        d2d_peer_ips=",".join(context.d2d_peer_ips) if context.d2d_peer_ips else None,
    )
    endpoint_config.validate()
    endpoint_config.load_deploy_config()
    return endpoint_config
