# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Deployment configuration, role planning and validation."""

from __future__ import annotations

import re
from typing import Any

import lib.constant as C
from lib.generator.kv_cache_store import resolve_external_master


def apply_rbg_defaults(config: dict[str, Any]) -> None:
    """Apply effective RBG-only defaults shared by rendering and scaling updates."""
    fault_tolerance = config.setdefault(C.MOTOR_CONTROLLER_CONFIG, {}).setdefault(C.FAULT_TOLERANCE_CONFIG, {})
    if fault_tolerance.get("enable_scale_p2d", False):
        raise ValueError("RBG deployment does not implement P2D")
    fault_tolerance["enable_scale_p2d"] = False


def pod_template(role: dict[str, Any]) -> dict[str, Any]:
    for name in ("standalonePattern", "leaderWorkerPattern"):
        if name in role:
            template = role[name].get("template")
            if not isinstance(template, dict):
                raise ValueError(f"{role['name']}.{name} requires an inline template")
            return template
    return role["customComponentsPattern"]["components"][0]["template"]


def integer(value: Any, path: str, minimum: int = 0) -> int:
    """Reject lossy casts and booleans for Kubernetes replica and resource counts."""
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or int(value) < minimum:
        raise ValueError(f"{path} must be an integer >= {minimum}")
    return int(value)


def dns_name(value: Any, path: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value):
        raise ValueError(f"{path} must be a DNS label of at most 63 characters")
    return value


def rbg_options(config: dict[str, Any]) -> dict[str, Any]:
    options = config[C.MOTOR_DEPLOY_CONFIG].get("rbg_config", {})
    if not isinstance(options, dict):
        raise TypeError("motor_deploy_config.rbg_config must be an object")
    return options


def deployment_settings(user_config: dict[str, Any]) -> dict:
    deploy = user_config[C.MOTOR_DEPLOY_CONFIG]
    if deploy.get(C.DEPLOY_MODE_CONFIG_KEY) != "rbg":
        raise ValueError("motor_deploy_config.deploy_mode must be rbg")
    namespace = dns_name(deploy.get("job_id"), "job_id")
    # InferServiceSet's stock name is vllm for every engine; retain its business DNS by default.
    logical = dns_name(rbg_options(user_config).get("logical_service_name", "vllm"), "logical_service_name")
    name = dns_name(rbg_options(user_config).get("name", logical), "rbg_config.name")
    return {
        "namespace": namespace,
        "rbg_name": name,
        "logical_service_name": logical,
    }


def dp_enabled(config: dict[str, Any], role: str) -> bool:
    ft = config.get(C.MOTOR_CONTROLLER_CONFIG, {}).get(C.FAULT_TOLERANCE_CONFIG, {})
    native = config.get(C.RBG_ROLE_CONFIGS[role], {}).get(C.ENGINE_CONFIG, {})
    return (
        role in {C.ROLE_DECODE, C.ROLE_UNION}
        and bool(ft.get(C.ENABLE_FAULT_TOLERANCE, True))
        and bool(ft.get(C.ENABLE_DP_SCALE_DOWN, False))
        and any(native.get(key) in (True, 1) for key in (C.ENABLE_FAULT_TOLERANCE, C.ENABLE_FAULT_TOLERANCE_KEBAB))
    )


def role_settings(user_config: dict[str, Any]) -> dict[str, dict]:
    """Preserve zero-replica Decode, while excluding roles outside the selected topology."""
    deploy = user_config[C.MOTOR_DEPLOY_CONFIG]
    options = rbg_options(user_config)
    policies = options.get("restart_policy_by_role", {})
    if not isinstance(policies, dict):
        raise TypeError("restart_policy_by_role must be an object")
    roles = {}
    for name in (C.CONTROLLER, C.COORDINATOR):
        standby = (
            user_config.get(C.RBG_ROLE_CONFIGS[name], {}).get(C.STANDBY_CONFIG, {}).get(C.ENABLE_MASTER_STANDBY, False)
        )
        roles[name] = {"name": name, "replicas": 2 if standby else 1, "pods_per_instance": 1, "restart_policy": "None"}
    engines = [C.ROLE_UNION] if C.HYBRID_INSTANCES_NUM in deploy else [C.ROLE_PREFILL, C.ROLE_DECODE]
    if C.E_INSTANCES_NUM in deploy:
        engines.append(C.ROLE_ENCODE)
    for name in engines:
        prefix = C.RBG_ENGINE_PREFIXES[name]
        replicas = integer(deploy.get(f"{prefix}_instances_num", 1), f"{prefix}_instances_num")
        size = integer(
            deploy.get(f"single_{prefix}_instance_pod_num", 1),
            f"single_{prefix}_instance_pod_num",
            1,
        )
        integer(deploy.get(f"{prefix}_pod_npu_num", 1), f"{prefix}_pod_npu_num", 1)
        degraded = dp_enabled(user_config, name)
        policy = policies.get(name, "None" if degraded else C.RBG_RECREATE)
        if policy not in ("None", C.RBG_RECREATE) or (degraded and policy != "None"):
            raise ValueError(f"restart_policy_by_role.{name} conflicts with DP or is unsupported")
        roles[name] = {"name": name, "replicas": replicas, "pods_per_instance": size, "restart_policy": policy}
    if set(policies) - set(engines):
        raise ValueError("restart_policy_by_role contains a disabled or non-engine role")
    kv = user_config.get(C.KV_CACHE_STORE_CONFIG, {})
    if (
        kv_enabled(user_config)
        and kv.get(C.KV_STORE_BACKEND, C.DEFAULT_KV_STORE_BACKEND) != C.YUANRONG_STORE_BACKEND
        and not resolve_external_master(kv)
    ):
        roles[C.ROLE_KV_STORE] = {
            "name": C.ROLE_KV_STORE,
            "replicas": 1,
            "pods_per_instance": 1,
            "restart_policy": "None",
        }
    if user_config.get(C.KV_CONDUCTOR_CONFIG, {}).get(C.KV_CONDUCTOR_PORT, 0) != 0:
        roles[C.ROLE_KV_CONDUCTOR] = {
            "name": C.ROLE_KV_CONDUCTOR,
            "replicas": 1,
            "pods_per_instance": 1,
            "restart_policy": "None",
        }
    engine = user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG) or user_config.get(C.MOTOR_ENGINE_UNION_CONFIG, {})
    if engine.get(C.ENGINE_TYPE) == "sglang":
        roles["mf-store"] = {"name": "mf-store", "replicas": 1, "pods_per_instance": 1, "restart_policy": "None"}
    return roles


def kv_enabled(config: dict[str, Any]) -> bool:
    engine = config.get(C.MOTOR_ENGINE_PREFILL_CONFIG) or config.get(C.MOTOR_ENGINE_UNION_CONFIG, {})
    connector = engine.get(C.ENGINE_CONFIG, {}).get(C.KV_TRANSFER_CONFIG, {}).get(C.KV_CONNECTOR)
    return connector == C.MULTI_CONNECTOR or bool(config.get(C.KV_CACHE_STORE_CONFIG))


def validate_rbg_gang_configuration(rbg: dict[str, Any]) -> None:
    enabled = rbg.get("metadata", {}).get("annotations", {}).get(C.RBG_GANG_ANNOTATION, "false") == "true"
    schedulers = set()
    hardware_policies = set()
    for role in rbg["spec"]["roles"]:
        if role.get("replicas", 1) == 0:
            continue
        template = pod_template(role)
        schedulers.add(template["spec"].get("schedulerName", "default-scheduler"))
        annotations = template.get("metadata", {}).get("annotations", {})
        strategy = annotations.get(C.HUAWEI_SCHEDULE_POLICY_ANNOTATION, "")
        if strategy.endswith("-sp"):
            raise ValueError("huawei.com/schedule_policy *-sp requires unsupported IO affinity identity injection")
        if "sp-block" in annotations or strategy:
            hardware_policies.add((annotations.get("sp-block"), strategy))
        if enabled:
            if role.get("podManagementPolicy", "Parallel") != "Parallel" or role.get("dependencies"):
                raise ValueError("gang requires Parallel roles without dependencies")
            components = role.get("customComponentsPattern", {}).get("components", [])
            for component in components:
                metadata = {
                    **component.get("template", {}).get("metadata", {}).get("annotations", {}),
                    **component.get("annotations", {}),
                }
                if any("depends-on" in key for key in metadata):
                    raise ValueError("gang does not support component dependencies")
    if len(schedulers) != 1:
        raise ValueError("All RBG roles must use the same schedulerName")
    if hardware_policies and schedulers != {"volcano"}:
        raise ValueError("Ascend hardware scheduling annotations require Volcano")
    if enabled and schedulers != {"volcano"}:
        raise ValueError("Volcano group gang requires schedulerName=volcano")
    if enabled and len(hardware_policies) > 1:
        raise ValueError("One RBG PodGroup cannot preserve different per-instance hardware scheduling policies")


def instance_ports_enabled(value: Any) -> bool:
    """Use the same switch values for validation, Pod env and startup scripts."""
    normalized = str(value).lower()
    if normalized not in ("", "0", "false", "1", "true"):
        raise ValueError(f"{C.ENV_MOTOR_INSTANCE_PORTS_ENABLE} must be a boolean, 0 or 1")
    return normalized in ("1", "true")


def _instance_port_stride(value: Any, name: str) -> int:
    result = integer(value, name, 1)
    if result > 65535:
        raise ValueError(f"{name} must be <= 65535")
    return result


def validate_instance_ports(
    config: dict[str, Any],
    env: dict[str, Any],
    roles: dict[str, dict],
) -> None:
    """Check the largest explicit instance offset before an HPA can create that ordinal."""
    for name, plan in roles.items():
        if plan['name'] not in C.RBG_ENGINE_PREFIXES:
            continue
        values = {
            **env.get(C.MOTOR_COMMON_ENV, {}),
            **env.get(f"motor_engine_{name}_env", {}),
        }
        enabled = instance_ports_enabled(values.get(C.ENV_MOTOR_INSTANCE_PORTS_ENABLE, "0"))
        if not enabled:
            continue
        if name not in (C.ROLE_PREFILL, C.ROLE_DECODE):
            raise ValueError("Explicit per-instance ports support only prefill/decode")
        nm = config.get(f"motor_engine_{name}_config", {}).get(C.MOTOR_NODEMANAGER_CONFIG, {})
        if nm.get("port_allocator_config", {}).get("enable", True) is not False:
            raise ValueError("Explicit per-instance ports require port_allocator_config.enable=false")
        largest = max(0, plan['replicas'] - 1)
        stride = _instance_port_stride(
            values.get("MOTOR_INSTANCE_PORT_STRIDE", 100),
            "MOTOR_INSTANCE_PORT_STRIDE",
        )
        engine = config.get(f"motor_engine_{name}_config", {}).get(C.ENGINE_CONFIG, {})
        checks = [
            (
                nm.get("api_config", {}).get("node_manager_port"),
                stride,
                "node_manager_port",
            ),
            (nm.get("endpoint_config", {}).get("base_port"), stride, "base_port"),
            (engine.get("data_parallel_rpc_port"), stride, "data_parallel_rpc_port"),
        ]
        for variable, stride_name, default in (
            ("ASCEND_BASE_PORT", "MOTOR_INSTANCE_ASCEND_PORT_STRIDE", 4000),
            ("HIXL_LISTEN_PORT", "MOTOR_INSTANCE_STORE_PORT_STRIDE", 200),
        ):
            if variable not in values:
                raise ValueError(f"{name} instance ports require {variable}")
            checks.append(
                (
                    values[variable],
                    _instance_port_stride(values.get(stride_name, default), stride_name),
                    variable,
                )
            )
        kv_stride = _instance_port_stride(
            values.get("MOTOR_INSTANCE_KV_PORT_STRIDE", 100),
            "MOTOR_INSTANCE_KV_PORT_STRIDE",
        )

        checks.extend((port, kv_stride, "kv_port") for port in _kv_ports(engine.get("kv_transfer_config", {})))
        for port, step, key in checks:
            base = integer(port, f"{name}.{key}", 1)
            if base + largest * step > 65535:
                raise ValueError(f"{name}.{key} exceeds 65535 at instance {largest}")


def _kv_ports(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [port for key, value in node.items() for port in ([value] if key == "kv_port" else _kv_ports(value))]
    if isinstance(node, list):
        return [port for value in node for port in _kv_ports(value)]
    return []
