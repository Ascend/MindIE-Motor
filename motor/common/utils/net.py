# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""IPv4 / IPv6 address helpers.

Runtime decides the address family by inspecting the host literal so that
existing IPv4 deployments stay byte-for-byte compatible while IPv6 single
stack works without new configuration knobs.

Domain names (anything not a valid IP literal) fall back to ``AF_INET`` to
preserve current behaviour for clusters relying on Kubernetes service DNS.
"""

import ipaddress
import socket


def _strip_brackets(host: str) -> str:
    if host.startswith('[') and host.endswith(']'):
        return host[1:-1]
    return host


def _is_ipv6_literal(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(_strip_brackets(host)), ipaddress.IPv6Address)
    except ValueError:
        return False


def detect_family(host: str) -> int:
    """Pick AF_INET6 only when ``host`` is an IPv6 literal.

    Non-literal hosts (domain names, ``localhost``, empty string) default to
    AF_INET to remain backward compatible with existing IPv4 deployments.
    """
    return socket.AF_INET6 if _is_ipv6_literal(host) else socket.AF_INET


def format_host(host: str) -> str:
    """Wrap an IPv6 literal in brackets so it is safe to embed in URLs."""
    if not host:
        return host
    if host.startswith('[') and host.endswith(']'):
        return host
    if _is_ipv6_literal(host):
        return f"[{host}]"
    return host


def format_address(host: str, port: int | str) -> str:
    """Build an RFC 3986 ``host:port`` string for URLs or socket targets."""
    return f"{format_host(host)}:{port}"


def split_address(address: str) -> tuple[str, str]:
    """Reverse of :func:`format_address`.

    Returns ``(host, port)``. ``port`` is ``""`` when the input has no port.
    Accepts ``host:port``, ``[v6]:port`` and bare hosts.
    """
    if not address:
        return address, ""
    if address.startswith('['):
        host_part, sep, port = address[1:].partition(']')
        if not sep:
            return address, ""
        return host_part, port[1:] if port.startswith(':') else ""
    host, sep, port = address.rpartition(':')
    if not sep:
        return address, ""
    # Unbracketed ``[v6]:port`` — only split when the left side is a valid v6 literal
    # and the right side is numeric. Bare ``2001:db8::1`` / ``::1`` stay host-only because
    # rpartition would mis-parse the last hextet as a port.
    if port.isdigit() and _is_ipv6_literal(host):
        return host, port
    if _is_ipv6_literal(address):
        return address, ""
    return host, port
