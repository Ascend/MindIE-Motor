# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Deploy-time K8s NodePort conflict detection and yaml rewrite.

Checks cluster-wide nodePort occupancy before kubectl apply. On conflict,
prompts per port (y=auto-assign, <port>=use that port, N=keep conflicting
port like deploy without remap). Rewrites only accepted remaps in generated
yaml. Does not touch Pod-internal ports (1025/1027/...).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass

import yaml

import lib.constant as C
from lib.utils import is_observability_service_name, load_yaml, logger, write_yaml

_KUBECTL = "kubectl"
NODEPORT_MIN = 30000
NODEPORT_MAX = 32767
_KIND_INFER_SERVICE_SET = "InferServiceSet"
_CONTAINER_PORT_COORD_INFER = 1025
_CONTAINER_PORT_COORD_METRICS = 1027

_SHOWLOG_ROLE_COORDINATOR = "coordinator"
_SHOWLOG_ROLE_CONTROLLER = "controller"


@dataclass(frozen=True)
class PlannedNodePort:
    port: int
    yaml_path: str
    service_name: str
    service_namespace: str
    purpose: str
    container_port: int | None


@dataclass(frozen=True)
class ClusterNodePort:
    port: int
    namespace: str
    service_name: str


def _purpose_for(service_name: str, container_port: int | None) -> str:
    name = str(service_name or "").lower()
    if "infer" in name or container_port == _CONTAINER_PORT_COORD_INFER:
        return "Coordinator infer"
    obs_name = is_observability_service_name(name)
    if "controller" in name and obs_name:
        return "Controller observability"
    if obs_name and "coordinator" not in name and container_port == _CONTAINER_PORT_COORD_METRICS:
        return "Controller observability"
    if "observability" in name and "coordinator" not in name:
        return "Controller observability"
    if obs_name or container_port == _CONTAINER_PORT_COORD_METRICS:
        return "Coordinator metrics"
    return f"Service {service_name or 'unknown'}"


def _showlog_role_for(purpose: str) -> str:
    if purpose.startswith("Controller"):
        return _SHOWLOG_ROLE_CONTROLLER
    return _SHOWLOG_ROLE_COORDINATOR


def _config_key_for(purpose: str) -> str:
    if purpose == "Coordinator infer":
        return C.COORDINATOR_INFER_NODE_PORT
    if purpose == "Coordinator metrics":
        return C.COORDINATOR_OBS_NODE_PORT
    if purpose == "Controller observability":
        return C.CONTROLLER_OBSERVABILITY_NODE_PORT
    return "nodePort"


def _planned_from_service_spec(
    *,
    yaml_path: str,
    svc_name: str,
    svc_ns: str,
    svc_spec: dict,
) -> list[PlannedNodePort]:
    planned: list[PlannedNodePort] = []
    for port_entry in (svc_spec or {}).get("ports") or []:
        if not isinstance(port_entry, dict):
            continue
        node_port = port_entry.get("nodePort")
        if not node_port:
            continue
        try:
            port = int(node_port)
        except (TypeError, ValueError):
            logger.warning(
                "[NodePort] skip non-integer nodePort=%r in %s service=%s",
                node_port,
                yaml_path,
                svc_name,
            )
            continue
        container_port = port_entry.get("port")
        try:
            container_port_i = int(container_port) if container_port is not None else None
        except (TypeError, ValueError):
            container_port_i = None
        planned.append(
            PlannedNodePort(
                port=port,
                yaml_path=yaml_path,
                service_name=svc_name,
                service_namespace=svc_ns,
                purpose=_purpose_for(svc_name, container_port_i),
                container_port=container_port_i,
            )
        )
    return planned


def _collect_from_service_doc(doc: dict, yaml_path: str) -> list[PlannedNodePort]:
    meta = doc.get("metadata") or {}
    return _planned_from_service_spec(
        yaml_path=yaml_path,
        svc_name=meta.get("name") or "",
        svc_ns=meta.get("namespace") or "",
        svc_spec=doc.get("spec") or {},
    )


def _collect_from_infer_service_set(doc: dict, yaml_path: str) -> list[PlannedNodePort]:
    """Collect nodePorts nested under InferServiceSet roles[].services[]."""
    meta = doc.get("metadata") or {}
    svc_ns = meta.get("namespace") or ""
    planned: list[PlannedNodePort] = []
    template = (doc.get("spec") or {}).get("template") or {}
    if not isinstance(template, dict):
        return []
    roles = template.get("roles") or []
    if not isinstance(roles, list):
        return []
    for role in roles:
        if not isinstance(role, dict):
            continue
        role_name = (role.get("name") or "").lower()
        for svc in role.get("services") or []:
            if not isinstance(svc, dict):
                continue
            svc_name = svc.get("name") or ""
            # Prefer role context for controller observability naming.
            if role_name == "controller" and is_observability_service_name(svc_name):
                ports = (svc.get("spec") or {}).get("ports") or []
                for port_entry in ports:
                    if not isinstance(port_entry, dict) or not port_entry.get("nodePort"):
                        continue
                    try:
                        port = int(port_entry["nodePort"])
                    except (TypeError, ValueError):
                        continue
                    try:
                        container_port_i = int(port_entry["port"]) if port_entry.get("port") is not None else None
                    except (TypeError, ValueError):
                        container_port_i = None
                    planned.append(
                        PlannedNodePort(
                            port=port,
                            yaml_path=yaml_path,
                            service_name=svc_name,
                            service_namespace=svc_ns,
                            purpose="Controller observability",
                            container_port=container_port_i,
                        )
                    )
                continue
            planned.extend(
                _planned_from_service_spec(
                    yaml_path=yaml_path,
                    svc_name=svc_name,
                    svc_ns=svc_ns,
                    svc_spec=svc.get("spec") or {},
                )
            )
    return planned


def _rewrite_ports_in_spec(svc_spec: dict, remapping: dict[int, int]) -> bool:
    changed = False
    for port_entry in (svc_spec or {}).get("ports") or []:
        if not isinstance(port_entry, dict):
            continue
        node_port = port_entry.get("nodePort")
        if node_port is None:
            continue
        try:
            old_port = int(node_port)
        except (TypeError, ValueError):
            continue
        new_port = remapping.get(old_port)
        if new_port is None:
            continue
        port_entry["nodePort"] = new_port
        changed = True
    return changed


def _rewrite_service_doc(doc: dict, remapping: dict[int, int]) -> bool:
    return _rewrite_ports_in_spec(doc.get("spec") or {}, remapping)


def _rewrite_infer_service_set(doc: dict, remapping: dict[int, int]) -> bool:
    changed = False
    template = (doc.get("spec") or {}).get("template") or {}
    if not isinstance(template, dict):
        return False
    roles = template.get("roles") or []
    if not isinstance(roles, list):
        return False
    for role in roles:
        if not isinstance(role, dict):
            continue
        for svc in role.get("services") or []:
            if not isinstance(svc, dict):
                continue
            if _rewrite_ports_in_spec(svc.get("spec") or {}, remapping):
                changed = True
    return changed


def collect_planned_nodeports(yaml_files: list[str]) -> list[PlannedNodePort]:
    planned: list[PlannedNodePort] = []
    for yaml_path in yaml_files:
        if not yaml_path:
            continue
        try:
            docs = load_yaml(yaml_path, single_doc=False)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("[NodePort] skip unreadable yaml %s: %s", yaml_path, exc)
            continue
        if not isinstance(docs, list):
            docs = [docs]
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get(C.KIND)
            if kind == C.SERVICE:
                planned.extend(_collect_from_service_doc(doc, yaml_path))
            elif kind == _KIND_INFER_SERVICE_SET:
                planned.extend(_collect_from_infer_service_set(doc, yaml_path))
    return planned


def collect_cluster_nodeports() -> dict[int, ClusterNodePort]:
    """Return map nodePort -> owner Service across all namespaces."""
    cmd = [_KUBECTL, "get", "svc", "-A", "-o", "json"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"[NodePort] failed to query cluster services: {exc}") from exc
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"[NodePort] kubectl get svc -A failed: {err}")

    try:
        data = json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"[NodePort] invalid kubectl json output: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("[NodePort] invalid kubectl json output: expected object")

    used: dict[int, ClusterNodePort] = {}
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        ns = meta.get("namespace") or ""
        name = meta.get("name") or ""
        for port_entry in (item.get("spec") or {}).get("ports") or []:
            if not isinstance(port_entry, dict):
                continue
            node_port = port_entry.get("nodePort")
            if not node_port:
                continue
            try:
                port = int(node_port)
            except (TypeError, ValueError):
                continue
            used[port] = ClusterNodePort(port=port, namespace=ns, service_name=name)
    return used


def _is_self_owned(planned: PlannedNodePort, owner: ClusterNodePort, job_id: str) -> bool:
    """Treat same job namespace + same service name as our own (re-deploy)."""
    planned_ns = planned.service_namespace or job_id
    return owner.namespace == planned_ns and owner.service_name == planned.service_name


def find_conflicts(
    planned: list[PlannedNodePort],
    cluster: dict[int, ClusterNodePort],
    job_id: str,
) -> list[tuple[PlannedNodePort, ClusterNodePort]]:
    conflicts: list[tuple[PlannedNodePort, ClusterNodePort]] = []
    seen_ports: set[int] = set()
    for item in planned:
        owner = cluster.get(item.port)
        if owner is None:
            continue
        if _is_self_owned(item, owner, job_id):
            continue
        if item.port in seen_ports:
            continue
        seen_ports.add(item.port)
        conflicts.append((item, owner))
    return conflicts


def allocate_free_port(
    old_port: int,
    cluster: dict[int, ClusterNodePort],
    planned: list[PlannedNodePort],
    reserved_extra: set[int] | None = None,
) -> int:
    """Pick one free NodePort to replace ``old_port``."""
    reserved = set(cluster.keys()) | {p.port for p in planned}
    if reserved_extra:
        reserved |= reserved_extra
    span = NODEPORT_MAX - NODEPORT_MIN + 1
    candidate = old_port + 1
    if candidate > NODEPORT_MAX:
        candidate = NODEPORT_MIN
    for _ in range(span):
        if candidate not in reserved:
            return candidate
        candidate += 1
        if candidate > NODEPORT_MAX:
            candidate = NODEPORT_MIN
    raise RuntimeError(
        f"[NodePort] no free nodePort in [{NODEPORT_MIN}, {NODEPORT_MAX}] "
        f"to replace {old_port}. If the cluster uses a custom "
        "--service-node-port-range, align yaml ports with that range."
    )


def allocate_free_ports(
    conflicts: list[tuple[PlannedNodePort, ClusterNodePort]],
    cluster: dict[int, ClusterNodePort],
    planned: list[PlannedNodePort],
) -> dict[int, int]:
    """Map old_port -> new_port for each conflicted planned port."""
    remapping: dict[int, int] = {}
    reserved_extra: set[int] = set()
    for item, _owner in conflicts:
        if item.port in remapping:
            continue
        chosen = allocate_free_port(item.port, cluster, planned, reserved_extra)
        remapping[item.port] = chosen
        reserved_extra.add(chosen)
    return remapping


def _format_conflict_guidance(item: PlannedNodePort, owner: ClusterNodePort) -> str:
    config_key = _config_key_for(item.purpose)
    lines = [
        "[NodePort] ERROR: NodePort conflict (same outcome as deploy without remap).",
        f"  Conflicting port: {item.port} ({item.purpose})",
        f"  Already allocated by: namespace={owner.namespace} service={owner.service_name}",
        "  Kubernetes rejects Service create with: nodePort already allocated",
        "",
        "How to fix:",
        f"  1) Manually set motor_deploy_config.{config_key} in user_config.json to a free NodePort",
        "     (remap does not rewrite user_config; keep config in sync before next generate/deploy)",
        "  2) Or edit nodePort in generated yaml under output_yamls/ (or yaml_template/), then redeploy",
        f"  Suggested NodePort range: {NODEPORT_MIN}-{NODEPORT_MAX}",
    ]
    return "\n".join(lines)


def _log_conflict_guidance(item: PlannedNodePort, owner: ClusterNodePort) -> None:
    for line in _format_conflict_guidance(item, owner).splitlines():
        logger.error("%s", line)


def _conflict_file_paths() -> tuple[str, str]:
    os.makedirs(C.OUTPUT_ROOT_PATH, exist_ok=True)
    coordinator_path = os.path.join(C.OUTPUT_ROOT_PATH, C.NODEPORT_CONFLICT_COORDINATOR_FILE)
    controller_path = os.path.join(C.OUTPUT_ROOT_PATH, C.NODEPORT_CONFLICT_CONTROLLER_FILE)
    return coordinator_path, controller_path


def clear_nodeport_conflict_files() -> None:
    for path in _conflict_file_paths():
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("")


def write_nodeport_conflict_files(
    declined: list[tuple[PlannedNodePort, ClusterNodePort]],
) -> None:
    """Write showlog warning files for declined (N / non-TTY) conflicts."""
    clear_nodeport_conflict_files()
    if not declined:
        return
    coordinator_path, controller_path = _conflict_file_paths()
    buckets = {
        _SHOWLOG_ROLE_COORDINATOR: [],
        _SHOWLOG_ROLE_CONTROLLER: [],
    }
    for item, owner in declined:
        buckets[_showlog_role_for(item.purpose)].append(_format_conflict_guidance(item, owner))
    with open(coordinator_path, "w", encoding="utf-8") as fh:
        if buckets[_SHOWLOG_ROLE_COORDINATOR]:
            fh.write("\n\n".join(buckets[_SHOWLOG_ROLE_COORDINATOR]) + "\n")
    with open(controller_path, "w", encoding="utf-8") as fh:
        if buckets[_SHOWLOG_ROLE_CONTROLLER]:
            fh.write("\n\n".join(buckets[_SHOWLOG_ROLE_CONTROLLER]) + "\n")


def _validate_user_port(
    port: int,
    *,
    old_port: int,
    cluster: dict[int, ClusterNodePort],
    planned: list[PlannedNodePort],
    remapping: dict[int, int],
    job_id: str,
) -> None:
    if port < NODEPORT_MIN or port > NODEPORT_MAX:
        raise RuntimeError(
            f"[NodePort] invalid port {port}: out of range. Suggested NodePort range: {NODEPORT_MIN}-{NODEPORT_MAX}"
        )
    owner = cluster.get(port)
    if owner is not None:
        # Allow replacing with a port already owned by the same planned service (unlikely).
        matching = next((p for p in planned if p.port == old_port), None)
        if matching is None or not _is_self_owned(matching, owner, job_id):
            raise RuntimeError(
                f"[NodePort] invalid port {port}: already allocated by "
                f"namespace={owner.namespace} service={owner.service_name}. "
                f"Suggested NodePort range: {NODEPORT_MIN}-{NODEPORT_MAX}"
            )
    reserved_targets = set(remapping.values())
    # Effective ports of other planned services (already-remapped targets included).
    planned_others = {remapping.get(p.port, p.port) for p in planned if p.port != old_port}
    if port in reserved_targets or port in planned_others:
        raise RuntimeError(
            f"[NodePort] invalid port {port}: already reserved by this deploy plan. "
            f"Suggested NodePort range: {NODEPORT_MIN}-{NODEPORT_MAX}"
        )


def _prompt_one_conflict(
    item: PlannedNodePort,
    owner: ClusterNodePort,
    *,
    cluster: dict[int, ClusterNodePort],
    planned: list[PlannedNodePort],
    remapping: dict[int, int],
    job_id: str,
) -> int | None:
    """Return new port, or None when user chooses N (keep conflicting port)."""
    logger.info(
        "[NodePort] CONFLICT: %d (%s) already allocated by namespace=%s service=%s",
        item.port,
        item.purpose,
        owner.namespace,
        owner.service_name,
    )
    logger.info(
        "[NodePort] Enter y=auto-assign, <port>=use that port, N=keep conflicting port (same as deploy without remap)."
    )
    logger.info("[NodePort] Suggested NodePort range: %d-%d", NODEPORT_MIN, NODEPORT_MAX)

    if not sys.stdin.isatty():
        logger.error("[NodePort] stdin is not a TTY; treating as N for port %d.", item.port)
        _log_conflict_guidance(item, owner)
        return None

    while True:
        try:
            answer = input(f"[NodePort] {item.port} ({item.purpose}) -> y / <port> / N: ").strip()
        except EOFError:
            logger.error("[NodePort] no interactive input; treating as N for port %d.", item.port)
            _log_conflict_guidance(item, owner)
            return None

        lowered = answer.lower()
        if lowered in ("", "n", "no"):
            _log_conflict_guidance(item, owner)
            return None
        if lowered in ("y", "yes"):
            new_port = allocate_free_port(
                item.port,
                cluster,
                planned,
                reserved_extra=set(remapping.values()),
            )
            logger.info("[NodePort] auto-assign: %d -> %d (%s)", item.port, new_port, item.purpose)
            return new_port
        if answer.isdigit():
            new_port = int(answer)
            try:
                _validate_user_port(
                    new_port,
                    old_port=item.port,
                    cluster=cluster,
                    planned=planned,
                    remapping=remapping,
                    job_id=job_id,
                )
            except RuntimeError as exc:
                logger.error("%s", exc)
                continue
            logger.info("[NodePort] user-assign: %d -> %d (%s)", item.port, new_port, item.purpose)
            return new_port
        logger.error(
            "[NodePort] invalid input %r. Enter y, a port number in %d-%d, or N.",
            answer,
            NODEPORT_MIN,
            NODEPORT_MAX,
        )


def rewrite_yaml_nodeports(yaml_files: list[str], remapping: dict[int, int]) -> None:
    if not remapping:
        return
    for yaml_path in yaml_files:
        try:
            docs = load_yaml(yaml_path, single_doc=False)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("[NodePort] skip unreadable yaml during rewrite %s: %s", yaml_path, exc)
            continue
        if not isinstance(docs, list):
            docs = [docs]
        changed = False
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            kind = doc.get(C.KIND)
            if kind == C.SERVICE:
                if _rewrite_service_doc(doc, remapping):
                    changed = True
            elif kind == _KIND_INFER_SERVICE_SET:
                if _rewrite_infer_service_set(doc, remapping):
                    changed = True
        if changed:
            write_yaml(docs, yaml_path, single_doc=False)
            logger.info("[NodePort] updated %s", yaml_path)


def _log_final_port_summary(planned: list[PlannedNodePort], remapping: dict[int, int]) -> None:
    by_purpose: dict[str, int] = {}
    for item in planned:
        final_port = remapping.get(item.port, item.port)
        # Keep first occurrence per purpose label for the common three ports.
        by_purpose.setdefault(item.purpose, final_port)
    infer = by_purpose.get("Coordinator infer", "-")
    coord_obs = by_purpose.get("Coordinator metrics", "-")
    ctrl_obs = by_purpose.get("Controller observability", "-")
    logger.info(
        "[NodePort] summary: infer=%s coordinator_obs=%s controller_obs=%s",
        infer,
        coord_obs,
        ctrl_obs,
    )


def resolve_and_rewrite_nodeports(
    yaml_files: list[str],
    job_id: str,
    dry_run: bool = False,
) -> dict[int, int]:
    """Detect NodePort conflicts, prompt per port, rewrite yaml. Returns remapping.

    When ``dry_run=True``, only print conflicts and proposed auto remapping; do not
    prompt or rewrite files. Returns an empty remapping (suggestions are log-only).
    """
    files = [f for f in yaml_files if f]
    if not files:
        return {}

    # dry-run must not wipe existing showlog warning files from a prior real deploy.
    if not dry_run:
        clear_nodeport_conflict_files()

    planned = collect_planned_nodeports(files)
    if not planned:
        logger.info("[NodePort] no nodePort found in generated yaml, skip.")
        return {}

    try:
        cluster = collect_cluster_nodeports()
    except RuntimeError as exc:
        logger.warning("[NodePort] skip conflict check: %s", exc)
        _log_final_port_summary(planned, {})
        return {}

    conflicts = find_conflicts(planned, cluster, job_id)
    remapping: dict[int, int] = {}
    declined: list[tuple[PlannedNodePort, ClusterNodePort]] = []

    if not conflicts:
        logger.info("[NodePort] no conflict.")
        _log_final_port_summary(planned, remapping)
        return remapping

    if dry_run:
        # In-memory suggestion only: do not return remapping (real deploy still uses original ports).
        proposed = allocate_free_ports(conflicts, cluster, planned)
        logger.info("[NodePort] conflict(s) detected (dry-run, yaml not rewritten):")
        for item, owner in conflicts:
            new_port = proposed[item.port]
            logger.info(
                "  %d busy <- namespace=%s service=%s (%s); proposed %d -> %d",
                item.port,
                owner.namespace,
                owner.service_name,
                item.purpose,
                item.port,
                new_port,
            )
        logger.info("[NodePort] Suggested NodePort range: %d-%d", NODEPORT_MIN, NODEPORT_MAX)
        logger.info("[NodePort] dry-run proposed remapping=%s (not applied)", proposed)
        _log_final_port_summary(planned, {})
        return {}

    for item, owner in conflicts:
        new_port = _prompt_one_conflict(
            item,
            owner,
            cluster=cluster,
            planned=planned,
            remapping=remapping,
            job_id=job_id,
        )
        if new_port is None:
            declined.append((item, owner))
            continue
        remapping[item.port] = new_port

    write_nodeport_conflict_files(declined)

    if remapping:
        rewrite_yaml_nodeports(files, remapping)
        logger.info("[NodePort] remapping applied: %s", remapping)
    elif declined:
        logger.error(
            "[NodePort] no remapping applied; deploy continues with conflicting ports "
            "(same as without this feature). Remap does not rewrite user_config.json; "
            "manually update motor_deploy_config.*_node_port if you change ports later."
        )

    _log_final_port_summary(planned, remapping)
    return remapping
