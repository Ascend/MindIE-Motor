# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Normalize caller probe IPs and shared ports without loading component configs."""

import ipaddress
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from motor.common.utils.env import Env
from motor.common.utils.net import format_address

EndpointIdentity = str
_REQUIRED_PORT = {"controller": "api", "coordinator": "management"}
_PORT_FIELDS = {"controller": "controller_api_port", "coordinator": "coordinator_api_mgmt_port"}
_TIMING_FIELDS = ("probe_interval_seconds", "probe_timeout_seconds", "max_staleness_seconds")


@dataclass(frozen=True)
class ServiceEndpoint:
    """One member identified by IP, using its component's shared fixed ports."""

    host: str
    ports: Mapping[str, int]

    def __post_init__(self) -> None:
        if not isinstance(self.host, str):
            raise TypeError("service endpoint host must be an IP address")
        address = ipaddress.ip_address(self.host)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("service endpoint host must be a unicast IP address")
        object.__setattr__(self, "host", str(address))
        if not isinstance(self.ports, Mapping) or not self.ports:
            raise ValueError("service endpoint ports must be a nonempty object")
        for kind, port in self.ports.items():
            if kind not in ("api", "management", "observability"):
                raise ValueError("Unknown service endpoint port: " + str(kind))
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("service endpoint port must be an integer between 1 and 65535")
        object.__setattr__(self, "ports", MappingProxyType(dict(self.ports)))

    @property
    def identity(self) -> EndpointIdentity:
        return self.host

    def address(self, kind: str) -> str:
        return format_address(self.host, self.ports[kind])


@dataclass(frozen=True)
class ServiceDiscoveryConfig:
    """Validated runtime endpoints; each configured target independently enables probing."""

    probe_interval_seconds: float = 2.0
    probe_timeout_seconds: float = 1.0
    max_staleness_seconds: float = 6.0
    endpoints: Mapping[str, tuple[ServiceEndpoint, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in _TIMING_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(name + " must be a finite positive number")
        if not isinstance(self.endpoints, Mapping):
            raise TypeError("service discovery endpoints must be an object")
        normalized = {}
        for service, members in self.endpoints.items():
            if service not in _REQUIRED_PORT:
                raise ValueError("Unknown discovery service: " + str(service))
            if not isinstance(members, (list, tuple)) or not members:
                raise ValueError(service + " endpoints must be a nonempty list")
            parsed = tuple(members)
            if any(_REQUIRED_PORT[service] not in member.ports for member in parsed):
                raise ValueError(service + " endpoint lacks its readiness port")
            if len({member.identity for member in parsed}) != len(parsed):
                raise ValueError(service + " contains duplicate endpoints")
            normalized[service] = parsed
        object.__setattr__(self, "endpoints", MappingProxyType(normalized))

    @classmethod
    def from_json(cls, json_path: str | None = None) -> "ServiceDiscoveryConfig":
        """Load static discovery settings using the existing USER_CONFIG_PATH convention."""
        config_path = json_path if json_path is not None else Env.user_config_path
        if not config_path or not Path(config_path).exists():
            return cls()
        content = Path(config_path).read_text(encoding="utf-8").strip()
        return cls.from_user_config(json.loads(content) if content else {})

    @classmethod
    def from_user_config(cls, data: dict[str, Any]) -> "ServiceDiscoveryConfig":
        """Expand public probe_ips with explicit shared ports from the caller's own JSON."""
        data = _require_object(data, "Motor user configuration")
        deploy = _require_object(data.get("motor_deploy_config", {}), "motor_deploy_config")
        discovery = _require_object(deploy.get("service_discovery_config", {}), "service_discovery_config")
        unknown = discovery.keys() - {*_TIMING_FIELDS, "probe_ips"}
        if unknown:
            raise ValueError("Unknown service discovery fields: " + ", ".join(sorted(unknown)))
        probe_ips = _require_object(discovery.get("probe_ips", {}), "probe_ips")
        endpoints = {}
        for service, hosts in probe_ips.items():
            if service not in _REQUIRED_PORT:
                raise ValueError("Unknown discovery service: " + str(service))
            if not isinstance(hosts, list) or not hosts:
                raise ValueError(service + " probe_ips must be a nonempty list")
            ports = _target_ports(data, service)
            endpoints[service] = tuple(ServiceEndpoint(host, ports) for host in hosts)
        timings = {key: discovery[key] for key in _TIMING_FIELDS if key in discovery}
        return cls(endpoints=endpoints, **timings)

    def uses_probe(self, service: str) -> bool:
        """A missing target keeps the existing direct-address path."""
        return service in self.endpoints


def _require_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(name + " must be an object")
    return value


def _target_ports(data: dict[str, Any], service: str) -> dict[str, int]:
    section = "motor_" + service + "_config"
    target = _require_object(data.get(section, {}), section)
    api = _require_object(target.get("api_config", {}), section + ".api_config")
    field_name = _PORT_FIELDS[service]
    if field_name not in api:
        raise ValueError(service + " probe_ips require explicit " + section + ".api_config." + field_name)
    ports = {_REQUIRED_PORT[service]: api[field_name]}
    if service == "coordinator":
        allocator = _require_object(target.get("port_allocator_config", {}), section + ".port_allocator_config")
        if allocator.get("enable") is not False:
            raise ValueError(
                "coordinator probe_ips require motor_coordinator_config.port_allocator_config.enable=false"
            )
        if "coordinator_obs_port" in api:
            ports["observability"] = api["coordinator_obs_port"]
    return ports
