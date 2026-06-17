#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Auto-discover pyMotor observability endpoints and generate runtime configs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_COORDINATOR_PORT = 1027
DEFAULT_ENGINE_PORT = 10001
DEFAULT_OBS_HOST = "localhost"
DEFAULT_PROMETHEUS_PORT = 9090
DEFAULT_GRAFANA_PORT = 3000
DEFAULT_TEMPO_PORT = 3200
DEFAULT_OTLP_GRPC_PORT = 4317
DEFAULT_OTLP_HTTP_PORT = 4318
DEFAULT_PORT_FORWARD_BASE = 19000

ENGINE_POD_RE = re.compile(
    r"^(?P<base>vllm|mindie-server|mindie-llm|sglang)(?:-(?P<role>p|d|e)(?P<idx>\d+))(?:-|$)",
    re.IGNORECASE,
)
COORDINATOR_POD_KEYWORDS = ("coordinator", "mindie-motor-coordinator")

_PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
)


def _kubectl_env() -> Dict[str, str]:
    env = os.environ.copy()
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)
    return env


def _kubectl_path() -> Optional[str]:
    return shutil.which("kubectl")


@dataclass
class PortForwardSpec:
    namespace: str
    pod_ip: str
    remote_port: int
    local_port: int
    pod_name: str

    def to_env_value(self) -> str:
        return f"{self.namespace}|{self.pod_ip}|{self.remote_port}|{self.local_port}|{self.pod_name}"


@dataclass
class CoordinatorPodMatch:
    pod_ip: str
    pod_name: str
    is_primary: bool
    ready_count: int
    total_count: int


@dataclass
class DiscoveryResult:
    namespace: str
    node_ip: str
    obs_host: str
    mode: str
    runtime: str
    coordinator_target: str
    coordinator_pod_name: str
    coordinator_is_primary: bool
    engine_targets: List[Dict[str, Any]]
    port_forwards: List[PortForwardSpec]
    warnings: List[str]

    @property
    def prefill_count(self) -> int:
        return sum(1 for item in self.engine_targets if item["labels"].get("pd_role") == "prefill")

    @property
    def decode_count(self) -> int:
        return sum(1 for item in self.engine_targets if item["labels"].get("pd_role") == "decode")


def _run_kubectl_json(args: Sequence[str]) -> Dict[str, Any]:
    kubectl = _kubectl_path()
    if kubectl is None:
        raise FileNotFoundError("kubectl not found in PATH")
    cmd = [kubectl, *args, "-o", "json"]
    output = subprocess.run(cmd, check=True, capture_output=True, text=True, env=_kubectl_env())
    return json.loads(output.stdout)


def _read_user_config_job_id(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    cfg = Path(path)
    if not cfg.is_file():
        return None
    try:
        payload = json.loads(cfg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    motor_cfg = payload.get("motor_deploy_config")
    if not isinstance(motor_cfg, dict):
        return None
    job_id = motor_cfg.get("job_id")
    return str(job_id) if job_id else None


def _is_kubectl_ready() -> bool:
    kubectl = _kubectl_path()
    if kubectl is None:
        return False
    kubectl_env = _kubectl_env()
    try:
        subprocess.run(
            [kubectl, "version", "--client"],
            check=True,
            capture_output=True,
            text=True,
            env=kubectl_env,
        )
        subprocess.run(
            [kubectl, "get", "ns"],
            check=True,
            capture_output=True,
            text=True,
            env=kubectl_env,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _service_name(service: Dict[str, Any]) -> str:
    return str(service.get("metadata", {}).get("name", ""))


def _service_namespace(service: Dict[str, Any]) -> str:
    return str(service.get("metadata", {}).get("namespace", ""))


def _service_ports(service: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = service.get("spec", {})
    ports = spec.get("ports", [])
    return ports if isinstance(ports, list) else []


def _has_keyword(text: str, keywords: Sequence[str]) -> bool:
    text_lower = text.lower()
    return any(key in text_lower for key in keywords)


def _is_engine_pod(name: str) -> bool:
    if ENGINE_POD_RE.search(name):
        return True
    return _has_keyword(name, ("engine", "mindie-motor-engine"))


def _infer_engine_identity_from_pod(name: str, counters: Dict[str, int]) -> Tuple[str, str]:
    match = ENGINE_POD_RE.search(name)
    if match:
        role_char = (match.group("role") or "p").lower()
        idx = match.group("idx") or "0"
        pd_role = "decode" if role_char == "d" else "prefill"
        return pd_role, f"{role_char}{idx}"
    return _infer_engine_identity(name, counters)


def _pod_ready_counts(pod: Dict[str, Any]) -> Tuple[int, int]:
    """Return (ready, total) container counts, matching kubectl READY column."""
    statuses = pod.get("status", {}).get("containerStatuses") or []
    if not statuses:
        return 0, 0
    total = len(statuses)
    ready = sum(1 for item in statuses if item.get("ready"))
    return ready, total


def _is_running_coordinator_pod(pod: Dict[str, Any]) -> bool:
    phase = str(pod.get("status", {}).get("phase", ""))
    pod_ip = str(pod.get("status", {}).get("podIP", ""))
    return phase == "Running" and bool(pod_ip)


def _is_primary_coordinator_pod(pod: Dict[str, Any]) -> bool:
    """Primary coordinator in HA: kubectl READY shows N/N with N > 0 (e.g. 1/1)."""
    if not _is_running_coordinator_pod(pod):
        return False
    ready, total = _pod_ready_counts(pod)
    return total > 0 and ready == total


def _list_coordinator_pods(namespace: str) -> List[Dict[str, Any]]:
    pod_json = _run_kubectl_json(["get", "pods", "-n", namespace])
    pods: List[Dict[str, Any]] = []
    for pod in pod_json.get("items", []):
        pod_name = str(pod.get("metadata", {}).get("name", ""))
        if _has_keyword(pod_name, COORDINATOR_POD_KEYWORDS):
            pods.append(pod)
    return pods


def _discover_coordinator_pod(
    namespace: str,
    port: int = DEFAULT_COORDINATOR_PORT,
) -> Optional[CoordinatorPodMatch]:
    primary_pods: List[Dict[str, Any]] = []
    fallback_pods: List[Dict[str, Any]] = []
    for pod in _list_coordinator_pods(namespace):
        if _is_primary_coordinator_pod(pod):
            primary_pods.append(pod)
        elif _is_running_coordinator_pod(pod):
            fallback_pods.append(pod)

    selected_pool = primary_pods or fallback_pods
    if not selected_pool:
        return None

    pod = sorted(selected_pool, key=lambda item: str(item.get("metadata", {}).get("name", "")))[0]
    ready, total = _pod_ready_counts(pod)
    return CoordinatorPodMatch(
        pod_ip=str(pod["status"]["podIP"]),
        pod_name=str(pod["metadata"]["name"]),
        is_primary=bool(primary_pods),
        ready_count=ready,
        total_count=total,
    )


def _find_namespace_from_services() -> Optional[str]:
    try:
        svc_json = _run_kubectl_json(["get", "svc", "-A"])
    except subprocess.CalledProcessError:
        return None
    candidates: List[str] = []
    for svc in svc_json.get("items", []):
        name = _service_name(svc)
        if not _has_keyword(name, ("coordinator", "mindie-motor-coordinator")):
            continue
        for port in _service_ports(svc):
            node_port = port.get("nodePort")
            service_port = int(port.get("port") or 0)
            target_port = str(port.get("targetPort", ""))
            port_name = str(port.get("name", ""))
            is_observability = (
                service_port == DEFAULT_COORDINATOR_PORT
                or target_port == str(DEFAULT_COORDINATOR_PORT)
                or _has_keyword(port_name, ("metrics", "observ", "obs"))
            )
            if node_port and is_observability:
                candidates.append(_service_namespace(svc))
                break
    if not candidates:
        return None
    # Stable and predictable selection.
    return sorted(candidates)[0]


def _resolve_namespace(args: argparse.Namespace) -> str:
    if args.namespace:
        return args.namespace
    env_namespace = os.getenv("MOTOR_NAMESPACE")
    if env_namespace:
        return env_namespace
    job_id = _read_user_config_job_id(args.user_config or os.getenv("MOTOR_USER_CONFIG"))
    if job_id:
        return job_id
    if _is_kubectl_ready():
        ns = _find_namespace_from_services()
        if ns:
            return ns
    return "default"


def _has_explicit_namespace(args: argparse.Namespace) -> bool:
    return bool(args.namespace or os.getenv("MOTOR_NAMESPACE"))


def _resolve_node_ip(namespace: str, args: argparse.Namespace, warnings: List[str]) -> str:
    if args.node_ip:
        return args.node_ip
    env_node_ip = os.getenv("MOTOR_NODE_IP")
    if env_node_ip:
        return env_node_ip

    if not _is_kubectl_ready():
        warnings.append("kubectl not ready, fallback to 127.0.0.1 as node IP.")
        return "127.0.0.1"

    # 1) Primary coordinator pod hostIP (READY N/N in HA master/standby).
    try:
        primary_host_ip = ""
        fallback_host_ip = ""
        for pod in sorted(
            _list_coordinator_pods(namespace), key=lambda item: str(item.get("metadata", {}).get("name", ""))
        ):
            host_ip = str(pod.get("status", {}).get("hostIP", ""))
            if not host_ip:
                continue
            if _is_primary_coordinator_pod(pod):
                primary_host_ip = host_ip
                break
            if not fallback_host_ip:
                fallback_host_ip = host_ip
        if primary_host_ip:
            return primary_host_ip
        if fallback_host_ip:
            warnings.append("primary coordinator pod not found, using coordinator hostIP fallback.")
            return fallback_host_ip
    except subprocess.CalledProcessError:
        pass

    # 2) Cluster node InternalIP.
    try:
        node_json = _run_kubectl_json(["get", "nodes"])
        for node in node_json.get("items", []):
            for addr in node.get("status", {}).get("addresses", []):
                if addr.get("type") == "InternalIP" and addr.get("address"):
                    return str(addr["address"])
    except subprocess.CalledProcessError:
        pass

    warnings.append("failed to resolve node IP from kubernetes, fallback to 127.0.0.1.")
    return "127.0.0.1"


def _match_nodeport_for_service(
    service: Dict[str, Any],
    expected_port: int,
    allow_keywords: Sequence[str],
) -> Optional[int]:
    for port in _service_ports(service):
        node_port = port.get("nodePort")
        if not node_port:
            continue
        service_port = int(port.get("port") or 0)
        target_port = str(port.get("targetPort", ""))
        name = str(port.get("name", ""))
        if service_port == expected_port or target_port == str(expected_port) or _has_keyword(name, allow_keywords):
            return int(node_port)
    return None


def _find_service_nodeport(
    services: Sequence[Dict[str, Any]],
    expected_port: int,
    service_keywords: Sequence[str],
    port_keywords: Sequence[str],
) -> Optional[Tuple[str, int]]:
    best_match: Optional[Tuple[str, int]] = None
    fallback_match: Optional[Tuple[str, int]] = None
    for svc in services:
        name = _service_name(svc)
        matched = _match_nodeport_for_service(svc, expected_port, port_keywords)
        if matched is None:
            continue
        if _has_keyword(name, service_keywords):
            best_match = (name, matched)
            break
        if fallback_match is None:
            fallback_match = (name, matched)
    return best_match or fallback_match


def _infer_engine_identity(name: str, counters: Dict[str, int]) -> Tuple[str, str]:
    lowered = name.lower()
    pd_role = "prefill"
    if "decode" in lowered:
        pd_role = "decode"
    elif "prefill" in lowered:
        pd_role = "prefill"

    instance_match = re.search(r"([pd]\d+)", lowered)
    if instance_match:
        return pd_role, instance_match.group(1)

    if pd_role == "decode":
        instance_id = f"d{counters['decode']}"
        counters["decode"] += 1
    else:
        instance_id = f"p{counters['prefill']}"
        counters["prefill"] += 1
    return pd_role, instance_id


def _infer_dp_rank(name: str, labels: Optional[Dict[str, Any]] = None) -> str:
    labels = labels or {}
    for key in ("dp_rank", "dp-rank", "rank"):
        if labels.get(key) is not None:
            return str(labels[key])
    lowered = name.lower()
    match = re.search(r"(?:dp|rank)[-_]?(\d+)", lowered)
    if match:
        return match.group(1)
    return ""


def _discover_engine_targets_from_services(
    services: Sequence[Dict[str, Any]],
    node_ip: str,
    engine_port: int,
    cluster_label: str,
) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    counters = {"prefill": 0, "decode": 0}
    for svc in services:
        name = _service_name(svc)
        if not _has_keyword(name, ("engine", "mindie-motor-engine")):
            continue
        node_port = _match_nodeport_for_service(
            svc,
            expected_port=engine_port,
            allow_keywords=("metrics", "mgmt", "manage", "prometheus"),
        )
        if node_port is None:
            continue
        pd_role, instance_id = _infer_engine_identity(name, counters)
        targets.append(
            {
                "target": f"{node_ip}:{node_port}",
                "labels": {
                    "motor_component": "engine",
                    "pd_role": pd_role,
                    "role": pd_role,
                    "instance_id": instance_id,
                    "cluster": cluster_label,
                    "source": "real",
                },
            }
        )
    return targets


def _discover_engine_targets_from_pods(
    namespace: str,
    engine_port: int,
    cluster_label: str,
) -> List[Dict[str, Any]]:
    pod_json = _run_kubectl_json(["get", "pods", "-n", namespace])
    targets: List[Dict[str, Any]] = []
    counters = {"prefill": 0, "decode": 0}
    for pod in pod_json.get("items", []):
        metadata = pod.get("metadata", {})
        pod_name = str(pod.get("metadata", {}).get("name", ""))
        if not _is_engine_pod(pod_name):
            continue
        phase = str(pod.get("status", {}).get("phase", ""))
        pod_ip = str(pod.get("status", {}).get("podIP", ""))
        if phase != "Running" or not pod_ip:
            continue
        pd_role, instance_id = _infer_engine_identity_from_pod(pod_name, counters)
        pod_labels = metadata.get("labels", {})
        dp_rank = _infer_dp_rank(pod_name, pod_labels if isinstance(pod_labels, dict) else {})
        targets.append(
            {
                "target": f"{pod_ip}:{engine_port}",
                "labels": {
                    "motor_component": "engine",
                    "pd_role": pd_role,
                    "role": pd_role,
                    "instance_id": instance_id,
                    "cluster": cluster_label,
                    "source": "real",
                    "pod_ip": pod_ip,
                    "pod_name": pod_name,
                    "pod_namespace": namespace,
                    "dp_rank": dp_rank,
                },
            }
        )
    return targets


def _split_target(target: str) -> Tuple[str, int]:
    host, port = target.rsplit(":", 1)
    return host.strip("[]"), int(port)


def _register_docker_port_forward(
    result: DiscoveryResult,
    *,
    host: str,
    remote_port: int,
    pod_name: str,
    next_port: int,
) -> Tuple[str, int]:
    local_port = next_port
    result.port_forwards.append(
        PortForwardSpec(
            namespace=result.namespace,
            pod_ip=host,
            remote_port=remote_port,
            local_port=local_port,
            pod_name=pod_name,
        )
    )
    return f"host.docker.internal:{local_port}", next_port + 1


def _apply_docker_gateway(result: DiscoveryResult, base_port: int) -> None:
    next_port = base_port
    coord_host, coord_port = _split_target(result.coordinator_target)
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", coord_host) and coord_host not in ("127.0.0.1", "0.0.0.0"):
        result.coordinator_target, next_port = _register_docker_port_forward(
            result,
            host=coord_host,
            remote_port=coord_port,
            pod_name=result.coordinator_pod_name or "mindie-motor-coordinator",
            next_port=next_port,
        )

    for item in result.engine_targets:
        target = str(item.get("target", ""))
        try:
            host, remote_port = _split_target(target)
        except (ValueError, TypeError):
            continue
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
            continue
        labels = item.setdefault("labels", {})
        if labels.get("pod_ip") != host:
            if host == "127.0.0.1":
                item["target"] = f"host.docker.internal:{remote_port}"
            continue
        pod_name = str(labels.get("pod_name") or f"pod-{next_port}")
        labels["original_target"] = target
        item["target"], next_port = _register_docker_port_forward(
            result,
            host=host,
            remote_port=remote_port,
            pod_name=pod_name,
            next_port=next_port,
        )


def _discover(namespace: str, node_ip: str, args: argparse.Namespace) -> DiscoveryResult:
    warnings: List[str] = []
    obs_host = args.obs_host or os.getenv("OBS_HOST") or node_ip or DEFAULT_OBS_HOST
    engine_port = args.engine_mgmt_port
    cluster_label = namespace
    runtime = args.runtime

    mode = "fallback"
    coordinator_host = node_ip
    coordinator_port = DEFAULT_COORDINATOR_PORT
    coordinator_pod_name = ""
    coordinator_is_primary = False
    engine_targets: List[Dict[str, Any]] = []
    port_forwards: List[PortForwardSpec] = []

    if _is_kubectl_ready():
        try:
            svc_json = _run_kubectl_json(["get", "svc", "-n", namespace])
            services = svc_json.get("items", [])
            mode = "kubernetes"

            coord_match = _find_service_nodeport(
                services=services,
                expected_port=DEFAULT_COORDINATOR_PORT,
                service_keywords=("coordinator", "mindie-motor-coordinator"),
                port_keywords=("metrics", "observ", "obs"),
            )
            coord_pod = _discover_coordinator_pod(namespace)
            if coord_pod:
                coordinator_host = coord_pod.pod_ip
                coordinator_port = DEFAULT_COORDINATOR_PORT
                coordinator_pod_name = coord_pod.pod_name
                coordinator_is_primary = coord_pod.is_primary
                if coordinator_is_primary:
                    warnings.append(
                        f"using primary coordinator pod {coordinator_pod_name} "
                        f"(READY {coord_pod.ready_count}/{coord_pod.total_count}) "
                        f"at {coordinator_host}:{coordinator_port}."
                    )
                    if coord_match:
                        _, node_port = coord_match
                        warnings.append(
                            f"coordinator NodePort {node_ip}:{node_port} skipped; "
                            "HA master/standby requires scraping the primary pod directly."
                        )
                else:
                    warnings.append(
                        f"no primary coordinator pod (READY N/N) found; "
                        f"using {coordinator_pod_name} "
                        f"(READY {coord_pod.ready_count}/{coord_pod.total_count}) "
                        f"at {coordinator_host}:{coordinator_port}."
                    )
            elif coord_match:
                _, coordinator_port = coord_match
                warnings.append(f"coordinator pod not found, using NodePort {node_ip}:{coordinator_port}.")
            else:
                warnings.append(
                    f"coordinator NodePort not found, fallback to default {node_ip}:{DEFAULT_COORDINATOR_PORT}."
                )

            if runtime == "docker":
                engine_targets = _discover_engine_targets_from_pods(
                    namespace=namespace,
                    engine_port=engine_port,
                    cluster_label=cluster_label,
                )
                if engine_targets:
                    warnings.append("docker runtime uses PodIP targets via host tcp forwarders.")
            if not engine_targets:
                engine_targets = _discover_engine_targets_from_services(
                    services=services,
                    node_ip=node_ip,
                    engine_port=engine_port,
                    cluster_label=cluster_label,
                )
            if not engine_targets:
                engine_targets = _discover_engine_targets_from_pods(
                    namespace=namespace,
                    engine_port=engine_port,
                    cluster_label=cluster_label,
                )
                if engine_targets:
                    warnings.append("engine NodePort service not found, fallback to PodIP targets.")

        except subprocess.CalledProcessError as exc:
            if _has_explicit_namespace(args):
                raise RuntimeError(f"kubernetes discovery failed for explicit namespace '{namespace}': {exc}") from exc
            warnings.append(f"kubernetes discovery failed: {exc}. fallback to static defaults.")
            mode = "fallback"
            engine_targets = []
    else:
        if _has_explicit_namespace(args):
            raise RuntimeError(
                f"kubectl is unavailable or cluster is unreachable for explicit namespace '{namespace}'."
            )
        warnings.append("kubectl unavailable or cluster unreachable, using fallback static discovery.")

    if not engine_targets:
        # Keep a deterministic fallback shape for dashboards and verification.
        engine_targets = [
            {
                "target": f"{node_ip}:{engine_port}",
                "labels": {
                    "motor_component": "engine",
                    "pd_role": "prefill",
                    "role": "prefill",
                    "instance_id": "p0",
                    "cluster": cluster_label,
                    "source": "local",
                },
            },
            {
                "target": f"{node_ip}:{engine_port}",
                "labels": {
                    "motor_component": "engine",
                    "pd_role": "prefill",
                    "role": "prefill",
                    "instance_id": "p1",
                    "cluster": cluster_label,
                    "source": "local",
                },
            },
            {
                "target": f"{node_ip}:{engine_port}",
                "labels": {
                    "motor_component": "engine",
                    "pd_role": "decode",
                    "role": "decode",
                    "instance_id": "d0",
                    "cluster": cluster_label,
                    "source": "local",
                },
            },
        ]
        warnings.append("engine targets fallback to default p0/p1/d0 layout.")

    coordinator_target = f"{coordinator_host}:{coordinator_port}"

    result = DiscoveryResult(
        namespace=namespace,
        node_ip=node_ip,
        obs_host=obs_host,
        mode=mode,
        runtime=runtime,
        coordinator_target=coordinator_target,
        coordinator_pod_name=coordinator_pod_name,
        coordinator_is_primary=coordinator_is_primary,
        engine_targets=engine_targets,
        port_forwards=port_forwards,
        warnings=warnings,
    )
    if runtime == "docker":
        _apply_docker_gateway(result, args.port_forward_base)
    return result


def _render_job(
    name: str,
    targets: Sequence[Dict[str, Any]],
    metrics_path: Optional[str] = None,
    honor_labels: bool = False,
) -> List[str]:
    lines: List[str] = [f"  - job_name: {name}"]
    if metrics_path:
        lines.append(f"    metrics_path: {metrics_path}")
    if honor_labels:
        lines.append("    honor_labels: true")
    lines.append("    static_configs:")
    for item in targets:
        target = item["target"]
        labels = item.get("labels", {})
        lines.append("      - targets:")
        lines.append(f'          - "{target}"')
        if labels:
            lines.append("        labels:")
            for key, value in labels.items():
                lines.append(f"          {key}: {value}")
    return lines


def _build_prometheus_config(result: DiscoveryResult) -> str:
    namespace = result.namespace
    coordinator_labels = {
        "motor_component": "coordinator",
        "cluster": namespace,
        "source": "real" if result.mode == "kubernetes" else "local",
    }
    if result.coordinator_pod_name:
        coordinator_labels["coordinator_pod"] = result.coordinator_pod_name
        coordinator_labels["coordinator_role"] = "primary" if result.coordinator_is_primary else "standby"
    coordinator_targets = [{"target": result.coordinator_target, "labels": coordinator_labels}]

    lines = [
        "# Auto-generated by scripts/discover-targets.py",
        "global:",
        "  scrape_interval: 5s",
        "  evaluation_interval: 15s",
        "  metric_name_validation_scheme: utf8",
        "  external_labels:",
        "    monitor: motor-observability",
        "",
        "scrape_configs:",
    ]
    lines.extend(_render_job("prometheus", [{"target": "localhost:9090"}]))
    lines.append("")
    lines.extend(_render_job("motor-coordinator", coordinator_targets, metrics_path="/metrics"))
    lines.append("")
    lines.extend(
        _render_job(
            "motor-coordinator-instance",
            [
                {
                    "target": result.coordinator_target,
                    "labels": {
                        **coordinator_labels,
                        "motor_metric_scope": "instance",
                    },
                }
            ],
            metrics_path="/metrics?type=instance",
        )
    )
    lines.append("")
    lines.extend(
        _render_job(
            "motor-coordinator-role-prefill",
            [
                {
                    "target": result.coordinator_target,
                    "labels": {
                        **coordinator_labels,
                        "motor_metric_scope": "role",
                        "role": "prefill",
                        "pd_role": "prefill",
                    },
                }
            ],
            metrics_path="/metrics?type=role&role=prefill",
        )
    )
    lines.append("")
    lines.extend(
        _render_job(
            "motor-coordinator-role-decode",
            [
                {
                    "target": result.coordinator_target,
                    "labels": {
                        **coordinator_labels,
                        "motor_metric_scope": "role",
                        "role": "decode",
                        "pd_role": "decode",
                    },
                }
            ],
            metrics_path="/metrics?type=role&role=decode",
        )
    )
    lines.append("")
    lines.extend(
        _render_job(
            "motor-coordinator-dp",
            [
                {
                    "target": result.coordinator_target,
                    "labels": {
                        **coordinator_labels,
                        "motor_metric_scope": "dp",
                    },
                }
            ],
            metrics_path="/metrics?type=dp",
        )
    )
    lines.append("")
    lines.extend(
        _render_job(
            "motor-coordinator-node",
            [
                {
                    "target": result.coordinator_target,
                    "labels": {
                        **coordinator_labels,
                        "motor_metric_scope": "node",
                    },
                }
            ],
            metrics_path="/metrics?type=node",
        )
    )
    lines.append("")
    lines.extend(_render_job("motor-engine", result.engine_targets, metrics_path="/metrics", honor_labels=True))
    lines.append("")
    lines.extend(
        _render_job(
            "vllm-profiling",
            result.engine_targets,
            metrics_path="/metrics",
            honor_labels=True,
        )
    )
    lines.append("")
    lines.extend(_render_job("ascend-npu-exporter", [{"target": "host.docker.internal:8082"}]))
    lines.append("")
    lines.extend(_render_job("node-exporter", [{"target": "node-exporter:9100"}]))
    lines.append("")
    lines.extend(_render_job("cadvisor", [{"target": "cadvisor:8080"}]))
    return "\n".join(lines) + "\n"


def _build_env(result: DiscoveryResult) -> str:
    obs_host = result.obs_host
    lines = [
        "# Auto-generated by scripts/discover-targets.py",
        f"MOTOR_NAMESPACE={result.namespace}",
        f"MOTOR_NODE_IP={result.node_ip}",
        f"CLUSTER_LABEL={result.namespace}",
        f"OBS_HOST={obs_host}",
        f"OBS_RUNTIME={result.runtime}",
        f"OTLP_HTTP_ENDPOINT=http://{obs_host}:{DEFAULT_OTLP_HTTP_PORT}/v1/traces",
        f"OTLP_GRPC_ENDPOINT=http://{obs_host}:{DEFAULT_OTLP_GRPC_PORT}",
        "PROMETHEUS_CONFIG_FILE=./generated/prometheus.yml",
        f"DISCOVERY_MODE={result.mode}",
        f"ENGINE_PREFILL_TARGETS={result.prefill_count}",
        f"ENGINE_DECODE_TARGETS={result.decode_count}",
        f"GRAFANA_PORT={os.getenv('GRAFANA_PORT', str(DEFAULT_GRAFANA_PORT))}",
        f"PROMETHEUS_PORT={os.getenv('PROMETHEUS_PORT', str(DEFAULT_PROMETHEUS_PORT))}",
        f"TEMPO_QUERY_PORT={os.getenv('TEMPO_QUERY_PORT', str(DEFAULT_TEMPO_PORT))}",
        f"OTEL_GRPC_PORT={os.getenv('OTEL_GRPC_PORT', str(DEFAULT_OTLP_GRPC_PORT))}",
        f"OTEL_HTTP_PORT={os.getenv('OTEL_HTTP_PORT', str(DEFAULT_OTLP_HTTP_PORT))}",
        f"PORT_FORWARD_COUNT={len(result.port_forwards)}",
    ]
    for idx, spec in enumerate(result.port_forwards):
        value = spec.to_env_value().replace("'", "'\"'\"'")
        lines.append(f"PORT_FORWARD_{idx}='{value}'")
    return "\n".join(lines) + "\n"


def _build_summary(result: DiscoveryResult) -> str:
    lines = [
        f"Discovery mode: {result.mode}",
        f"Runtime: {result.runtime}",
        f"Namespace: {result.namespace}",
        f"Cluster label: {result.namespace}",
        f"Node IP: {result.node_ip}",
        f"Coordinator: {result.coordinator_target}",
        f"Coordinator pod: {result.coordinator_pod_name or 'n/a'}",
        f"Coordinator primary: {result.coordinator_is_primary}",
        f"Engine prefill targets: {result.prefill_count}",
        f"Engine decode targets: {result.decode_count}",
        f"Port forwards: {len(result.port_forwards)}",
    ]
    for spec in result.port_forwards:
        lines.append(
            f"- {spec.namespace}/{spec.pod_name} {spec.pod_ip}:{spec.remote_port} -> localhost:{spec.local_port}"
        )
    if result.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {item}" for item in result.warnings)
    return "\n".join(lines) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover pyMotor observability targets.")
    parser.add_argument("--namespace", help="Kubernetes namespace / job_id.")
    parser.add_argument("--node-ip", help="Node IP for NodePort access.")
    parser.add_argument("--user-config", help="Path to pyMotor user_config.json.")
    parser.add_argument("--obs-host", help="Observability host for OTLP endpoint generation.")
    parser.add_argument(
        "--runtime",
        choices=("docker", "native"),
        default=os.getenv("MOTOR_DISCOVERY_RUNTIME", "docker"),
        help="Runtime target for generated configs (default: docker).",
    )
    parser.add_argument(
        "--port-forward-base",
        type=int,
        default=int(os.getenv("MOTOR_PORT_FORWARD_BASE", str(DEFAULT_PORT_FORWARD_BASE))),
        help="First local port for Docker PodIP bridge forwards (default: 19000).",
    )
    parser.add_argument(
        "--engine-mgmt-port",
        type=int,
        default=int(os.getenv("MOTOR_ENGINE_MGMT_PORT", str(DEFAULT_ENGINE_PORT))),
        help="Engine management metrics port (default: 10001).",
    )
    parser.add_argument(
        "--output-dir",
        default="generated",
        help="Output directory for generated files (default: generated).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    warnings: List[str] = []
    try:
        namespace = _resolve_namespace(args)
        if _has_explicit_namespace(args) and not _is_kubectl_ready():
            raise RuntimeError(
                f"kubectl is unavailable or cluster is unreachable for explicit namespace '{namespace}'."
            )
        node_ip = _resolve_node_ip(namespace=namespace, args=args, warnings=warnings)
        result = _discover(namespace=namespace, node_ip=node_ip, args=args)
    except RuntimeError as exc:
        print(f"[discover-targets] error: {exc}", file=sys.stderr)
        return 2
    result.warnings = [*warnings, *result.warnings]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    prometheus_file = out_dir / "prometheus.yml"
    discovered_env_file = out_dir / "discovered.env"
    summary_file = out_dir / "discovery-summary.txt"

    prometheus_file.write_text(_build_prometheus_config(result), encoding="utf-8")
    discovered_env_file.write_text(_build_env(result), encoding="utf-8")
    summary_file.write_text(_build_summary(result), encoding="utf-8")

    print(summary_file.read_text(encoding="utf-8").strip())
    print(f"Generated: {prometheus_file}")
    print(f"Generated: {discovered_env_file}")
    print(f"Generated: {summary_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
