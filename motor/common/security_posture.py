# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Fail-closed binding checks for the Coordinator HTTP planes.

A plane that listens on a non-loopback address while API-key authentication is
disabled is reachable by every peer that can route to the pod. Turning that
misconfiguration into a startup error keeps the "expose by accident" case from
being the default, while the explicit ``allow_insecure_bind`` switch keeps lab
and single-host setups working.
"""

from __future__ import annotations

import ipaddress

_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def is_loopback_bind(host: str) -> bool:
    """Return ``True`` when ``host`` only accepts loopback connections."""
    candidate = (host or "").strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if not candidate:
        return False
    if candidate.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def ensure_secure_bind(plane: str, host: str, auth_enabled: bool, allow_insecure_bind: bool) -> None:
    """Refuse to expose ``plane`` on a non-loopback address without authentication.

    Args:
        plane: Human readable plane name used in the error message.
        host: Address the plane is about to bind.
        auth_enabled: Whether the plane's API-key authentication is enabled.
        allow_insecure_bind: Explicit operator opt-in for unauthenticated exposure.

    Raises:
        RuntimeError: When ``host`` is not loopback and no authentication (or
            explicit opt-in) is configured.
    """
    if auth_enabled or allow_insecure_bind or is_loopback_bind(host):
        return
    raise RuntimeError(
        f"{plane} would listen on {host!r} with API-key authentication disabled, "
        "which exposes it to every peer that can reach this node. Enable the API key "
        "or set 'allow_insecure_bind': true in the corresponding api_key_config "
        "section if this exposure is intentional."
    )
