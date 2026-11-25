# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import ipaddress
import re
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass
class EtcdConfig:
    """ETCD configuration class"""

    # ETCD connection configuration
    etcd_hosts: list[str] = field(default_factory=lambda: ["etcd.default.svc.cluster.local"])
    etcd_port: int = 2379
    etcd_timeout: int = 5
    etcd_lb_policy: str = 'round_robin'
    # Expected certificate identity; multi-address TLS requires a shared DNS SAN.
    etcd_tls_server_name: str = ''

    # ETCD persistence configuration
    enable_etcd_persistence: bool = False  # Enable/disable ETCD persistence and restoration

    def __post_init__(self) -> None:
        # Validation belongs to component loading and direct client creation.
        if isinstance(self.etcd_hosts, list):
            self.etcd_hosts = list(self.etcd_hosts)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EtcdConfig":
        """Normalize legacy input; the owning component validates the configuration."""
        if not isinstance(data, dict):
            raise TypeError("etcd_config must be an object")
        # Keep the existing configuration loader's treatment of unrelated fields.
        supported = {item.name for item in fields(cls)} | {"etcd_host"}
        normalized = {name: value for name, value in data.items() if name in supported}
        if "etcd_host" in normalized:
            legacy_host = normalized.pop("etcd_host")
            if "etcd_hosts" in normalized and normalized["etcd_hosts"] != [legacy_host]:
                raise ValueError("etcd_host and etcd_hosts must specify the same single address")
            normalized["etcd_hosts"] = [legacy_host]
        return cls(**normalized)

    def validate_hosts(self, enable_tls: bool = False) -> None:
        """Allow one DNS name or IP, or a static list of IPs in one address family."""
        if not isinstance(self.etcd_hosts, list) or not self.etcd_hosts:
            raise ValueError("etcd_hosts must be a non-empty list of addresses")
        families = set()
        for host in self.etcd_hosts:
            if not isinstance(host, str) or not host or host.strip() != host:
                raise ValueError("etcd_hosts entries must be non-empty address strings")
            ip_literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
            try:
                address = ipaddress.ip_address(ip_literal)
                if ip_literal != host and address.version != 6:
                    raise ValueError("Only IPv6 addresses may use brackets")
                families.add(address.version)
            except ValueError as exc:
                if len(self.etcd_hosts) > 1:
                    raise ValueError("Multiple etcd_hosts must be static IP addresses") from exc
                if (
                    re.fullmatch(
                        r"[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*",
                        host.removesuffix("."),
                    )
                    is None
                ):
                    raise ValueError("etcd_hosts must contain valid IP addresses or a single hostname") from exc
        if len(families) > 1:
            raise ValueError("Multiple etcd_hosts must use the same IP address family")
        if enable_tls:
            name = self.etcd_tls_server_name
            if not isinstance(name, str) or (
                name
                and re.fullmatch(
                    r"[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*",
                    name.removesuffix("."),
                )
                is None
            ):
                raise ValueError("etcd_tls_server_name must be a hostname without a scheme or port, or empty")
            if len(self.etcd_hosts) > 1 and not name:
                raise ValueError("Multiple etcd_hosts with TLS require etcd_tls_server_name matching a shared DNS SAN")
