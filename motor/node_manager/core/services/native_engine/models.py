# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from motor.common.resources.instance import PDRole
from motor.config.endpoint import DeployConfig
from motor.config.tls_config import TLSConfig


@dataclass(frozen=True)
class LaunchContext:
    """Complete, immutable input required to build one native engine command."""

    role: PDRole
    instance_id: int
    dp_rank: int
    node_rank: int
    host: str
    business_port: int
    mgmt_port: int
    config_path: str
    master_dp_ip: str | None
    kv_port: int | None
    lookup_rpc_port: int | None
    dp_rpc_port: int | None
    d2d_peer_ips: tuple[str, ...]
    environment: Mapping[str, str]
    headless: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "d2d_peer_ips", tuple(self.d2d_peer_ips))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True)
class CommandSpec:
    """Native process invocation produced by a runtime adapter."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True)
class ProbeSpec:
    """Native readiness probe associated with one engine process."""

    path: str
    timeout_seconds: float
    startup_timeout_seconds: float
    max_attempts: int = 1
    tls_config: TLSConfig | None = None
    process_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")


@dataclass(frozen=True)
class LaunchSpec:
    """Command and readiness probe built from one validated engine config.

    ``deploy_config`` is the already-loaded and validated role-specific deploy
    configuration the backend consumed while building the launch spec. It lets
    NodeManager-owned features (e.g. virtual inference) read the health check
    configuration without re-loading the config file; it may be None when a
    backend does not load a deploy config.
    """

    command: CommandSpec
    probe: ProbeSpec
    deploy_config: DeployConfig | None = None


class RuntimeState(str, Enum):
    """Node-local lifecycle state; it is not part of the control-plane API."""

    STARTING = "starting"
    RUNNING = "running"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass
class RuntimeProcess:
    """Mutable process record owned exclusively by ProcessSupervisor."""

    endpoint_id: int
    process: object
    command: CommandSpec
    probe: ProbeSpec
    started_at: float
    process_group_id: int | None = None
    state: RuntimeState = RuntimeState.STARTING
    ready_at: float | None = None
