# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Generate /etc/hosts entries for host-network engines.

On an IPv6 hostNetwork cluster, engines cannot reach a ClusterIP. The entry
uses the Endpoint address: a hostNetwork pod IP is the node address the
process binds, and a pod-network IP stays the pod IP. IPv4 deploys do not
call this module.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from typing import Any

logger = logging.getLogger(__name__)

_MOTOR_SERVICE_PREFIXES = (
    "mindie-motor-",
    "service-vllm-",
    "kv-conductor-",
)

_WAIT_SERVICE_NAME_PARTS = (
    "mindie-motor-service-",
    "mindie-motor-observability-",
    # Pool master is pod-network. Host-network engines need its Endpoint IP in hosts.
    "kv-store",
)


def _kubectl_json(args: list[str]) -> dict[str, Any]:
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl not found in PATH")
    raw = subprocess.check_output([kubectl, *args], stderr=subprocess.STDOUT, timeout=60)
    return json.loads(raw)


def _is_motor_service(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in _MOTOR_SERVICE_PREFIXES)


def _collect_service_line(namespace: str, name: str, fqdn: str) -> str | None:
    try:
        ep = _kubectl_json(["get", "endpoints", name, "-n", namespace, "-o", "json"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("kubectl get endpoints %s failed: %s", name, exc)
        return None

    for subset in ep.get("subsets") or []:
        for addr in subset.get("addresses") or []:
            ep_ip = addr.get("ip")
            if ep_ip:
                return f"{ep_ip} {fqdn} {name}"
    logger.warning("No ready endpoint for %s; skipping hosts entry", fqdn)
    return None


def wait_motor_service_endpoints(namespace: str, timeout_s: int = 60, poll_s: float = 5.0) -> None:
    """Wait until Motor controller services have at least one endpoint address."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            svc_data = _kubectl_json(["get", "svc", "-n", namespace, "-o", "json"])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            logger.warning("wait endpoints: get svc failed: %s", exc)
            time.sleep(poll_s)
            continue

        pending: list[str] = []
        for item in svc_data.get("items") or []:
            name = (item.get("metadata") or {}).get("name") or ""
            if not any(part in name for part in _WAIT_SERVICE_NAME_PARTS):
                continue
            try:
                ep = _kubectl_json(["get", "endpoints", name, "-n", namespace, "-o", "json"])
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pending.append(name)
                continue
            has_addr = any((subset.get("addresses") or []) for subset in (ep.get("subsets") or []))
            if not has_addr:
                pending.append(name)

        if not pending:
            logger.info("Motor service endpoints ready in namespace %s", namespace)
            return
        logger.info("Waiting for endpoints: %s", ", ".join(pending))
        time.sleep(poll_s)

    logger.warning("Timed out after %ss waiting for Motor service endpoints in %s", timeout_s, namespace)


def write_service_hosts_file(namespace: str, output_path: str) -> int:
    lines: list[str] = []
    try:
        svc_data = _kubectl_json(["get", "svc", "-n", namespace, "-o", "json"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("kubectl get svc failed for %s: %s", namespace, exc)
        svc_data = {"items": []}

    for item in svc_data.get("items") or []:
        name = (item.get("metadata") or {}).get("name")
        if not name or not _is_motor_service(name):
            continue
        line = _collect_service_line(namespace, name, f"{name}.{namespace}.svc.cluster.local")
        if line:
            lines.append(line)

    content = "\n".join(dict.fromkeys(lines))
    if content:
        content += "\n"
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    logger.info("Wrote %d service host entries to %s", len(lines), output_path)
    return len(lines)
