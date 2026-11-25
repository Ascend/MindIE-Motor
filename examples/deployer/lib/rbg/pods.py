# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Reuse Motor Pod templates and apply RBG identity and runtime configuration."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import yaml

import lib.constant as C
from lib.a5_host_nic import (
    env_requests_a5_host_nic_overlay,
    env_requests_a5_ubg_host_network,
)
from lib.container_ipc import apply_k8s_host_ipc, host_ipc_enabled
from lib.generator.engine import (
    apply_a5_dns_config,
    apply_a5_engine_pod_config,
    apply_a5_workload,
    apply_engine_node_selector_overrides,
    apply_pd_heterogeneous_node_selector,
    set_container_npu,
    set_weight_mount,
)
from lib.generator.kv_cache_store import (
    gen_kv_store_env,
    resolve_external_master,
    split_plain_master_address,
)
from lib.generator.render import configure_render_sidecar
from lib.generator.storage import apply_dshm_size, apply_storage_volumes
from lib.rbg.config import integer, kv_enabled, pod_template
from lib.utils import apply_node_selector_override, resolve_workload_image


def load_documents(filename: str) -> list[dict[str, Any]]:
    with (C.RBG_TEMPLATE_ROOT / filename).open(encoding="utf-8") as stream:
        return [doc for doc in yaml.safe_load_all(stream) if doc]


def managed_labels(context: Any, role: str | None = None) -> dict[str, str]:
    labels = {
        C.RBG_GROUP_LABEL: context['rbg_name'],
        C.RBG_MANAGED_LABEL: C.RBG_MANAGED_VALUE,
        C.RBG_METRIC_LABEL: f"{context['logical_service_name']}-0",
    }
    if role is not None:
        labels[C.RBG_ROLE_LABEL] = role
    return labels


def merge_env(container: dict[str, Any], values: list[dict[str, Any]]) -> None:
    """Replace whole entries, so value and valueFrom cannot coexist after an update."""
    items = {item["name"]: deepcopy(item) for item in container.get("env", [])}
    items.update({item["name"]: deepcopy(item) for item in values})
    container["env"] = list(items.values())


def value_env(values: dict[str, Any]) -> list[dict[str, str]]:
    return [{"name": name, "value": str(value)} for name, value in values.items()]


def field_env(name: str, field: str) -> dict[str, Any]:
    return {"name": name, "valueFrom": {"fieldRef": {"fieldPath": field}}}


def apply_user_metadata(role: dict[str, Any], config: dict[str, Any]) -> None:
    template = pod_template(role)
    for field in ("labels", "annotations"):
        values = config.get(f"additional_{field}", {})
        if not isinstance(values, dict):
            raise TypeError(f"additional_{field} must be an object")
        for key, value in values.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or key in C.RBG_IO_KEYS
                or key in {"app", C.RBG_MANAGED_LABEL, C.RBG_METRIC_LABEL, "scheduling.k8s.io/group-name"}
                or key.startswith((C.RBG_PREFIX, "motor-deployer/", "infer.huawei.com/"))
            ):
                raise ValueError(f"additional_{field}.{key} is reserved or not a string")
        role.setdefault(field, {}).update(values)
        template.setdefault("metadata", {}).setdefault(field, {}).update(values)


def clean_template(template: dict[str, Any]) -> None:
    metadata = template.setdefault("metadata", {})
    for field in ("labels", "annotations"):
        values = metadata.setdefault(field, {})
        for key in list(values):
            if key in C.RBG_IO_KEYS or key.startswith("infer.huawei.com/"):
                del values[key]
    spec = template["spec"]
    spec.pop("nodeName", None)
    spec["restartPolicy"] = "Always"


def kv_environment(config: dict[str, Any], addresses: dict[str, str], *, master: bool = False) -> list[dict[str, Any]]:
    if not kv_enabled(config):
        return []
    kv = deepcopy(config.get(C.KV_CACHE_STORE_CONFIG, {}))
    kv.setdefault(C.KV_CACHE_STORE_PORT, C.DEFAULT_KV_CACHE_STORE_PORT)
    backend = kv.setdefault(C.KV_STORE_BACKEND, C.DEFAULT_KV_STORE_BACKEND)
    explicit = resolve_external_master(kv)
    host, port = split_plain_master_address(explicit)
    address = host if explicit else addresses.get("kv_store", "")
    if master:
        return gen_kv_store_env(kv, service_name=address, external=bool(explicit), external_port=port)
    values = {C.ENV_KV_STORE_BACKEND: backend}
    if address:
        values[C.ENV_KVS_MASTER_SERVICE] = address
    if explicit:
        values[C.ENV_KV_STORE_EXTERNAL] = "1"
        if host:
            values[C.ENV_KV_CACHE_STORE_PORT] = str(port)
    if backend == C.MMC_STORE_BACKEND:
        values[C.ENV_MMC_LOCAL_CONFIG_PATH] = C.DEFAULT_MMC_LOCAL_CONFIG_PATH
        if kv.get(C.MMC_LOCAL_SERVICE_CONFIG_KEY):
            values[C.ENV_MMC_LOCAL_SERVICE_MODE] = kv[C.MMC_LOCAL_SERVICE_CONFIG_KEY]
    elif backend == C.YUANRONG_STORE_BACKEND and kv.get(C.YUANRONG_ETCD_ADDRESS_KEY):
        values[C.ENV_ETCD_ADDRESS] = kv[C.YUANRONG_ETCD_ADDRESS_KEY]
    return value_env(values)


def _hardware(
    template: dict[str, Any],
    container: dict[str, Any],
    plan: dict,
    config: dict[str, Any],
    env: dict[str, Any],
    facts: dict[str, Any],
) -> None:
    deploy, spec = config[C.MOTOR_DEPLOY_CONFIG], template["spec"]
    hardware = deploy.get(C.HARDWARE_TYPE, C.HARDWARE_TYPE_800I_A2)
    selector = spec.setdefault("nodeSelector", {})
    if hardware in C.HARDWARE_TYPE_A2 or hardware in C.HARDWARE_TYPE_A3:
        accelerator = facts.get("accelerator_type")
        if not accelerator:
            raise ValueError("facts.accelerator_type is required for A2/A3; render does not query the cluster")
        selector.update({C.ACCELERATOR: C.ACCELERATOR_910, C.ACCELERATOR_TYPE: accelerator})
    elif hardware in C.HARDWARE_TYPE_A5:
        selector.pop(C.ACCELERATOR_TYPE, None)
        selector[C.ACCELERATOR] = C.ACCELERATOR_A5
    prefix = C.RBG_ENGINE_PREFIXES[plan['name']]
    if plan['name'] in (C.ROLE_PREFILL, C.ROLE_DECODE):
        apply_pd_heterogeneous_node_selector(spec, config, prefix)
    required = dict(selector)
    apply_engine_node_selector_overrides(spec, deploy, prefix)
    if any(selector.get(key) != value for key, value in required.items()):
        raise ValueError(f"{plan['name']} node selector overrides required hardware constraints")
    npu = integer(deploy.get(f"{prefix}_pod_npu_num", 1), f"{prefix}_pod_npu_num", 1)
    set_container_npu(container, npu, deploy)
    annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
    if hardware in C.HARDWARE_TYPE_A3 or hardware in C.HARDWARE_TYPE_A5:
        annotations.setdefault("sp-block", str(plan['pods_per_instance'] * npu))
    else:
        annotations.pop("sp-block", None)
    apply_a5_engine_pod_config(
        spec,
        container,
        deploy,
        host_nic_overlay=env_requests_a5_host_nic_overlay(env),
        ubg_host_network=env_requests_a5_ubg_host_network(env),
    )
    apply_a5_workload(template, deploy)


def render_role(
    role: dict[str, Any],
    plan: dict,
    context: dict,
    config: dict[str, Any],
    env: dict[str, Any],
    facts: dict[str, Any],
    addresses: dict[str, str],
    scheduler: str,
    service_id: str,
) -> dict[str, Any]:
    """Apply Motor configuration to a copied native RBG role template."""
    role = deepcopy(role)
    deploy = config[C.MOTOR_DEPLOY_CONFIG]
    section = config.get(C.RBG_ROLE_CONFIGS[plan['name']], {})
    template = pod_template(role)
    clean_template(template)
    role["replicas"] = plan['replicas']
    role.setdefault("podManagementPolicy", "Parallel")
    role.setdefault("annotations", {})[C.RBG_PREFIX + "role-instance-pattern"] = "Stateful"
    if plan['name'] in (C.CONTROLLER, C.COORDINATOR):
        role["rolloutStrategy"]["rollingUpdate"]["maxUnavailable"] = plan['replicas']
    if plan['name'] in C.RBG_ENGINE_PREFIXES:
        if "leaderWorkerPattern" in role:
            pattern = role["leaderWorkerPattern"]
            # RBG size includes the leader; the remaining size - 1 Pods are workers.
            pattern["size"] = plan['pods_per_instance']
        else:
            pattern = role["customComponentsPattern"]
            pattern["components"][0]["size"] = plan['pods_per_instance']
        restart = pattern.setdefault("restartPolicyConfig", {})
        restart["type"] = plan['restart_policy']
        restart.setdefault("baseDelaySeconds", 30)
        restart.setdefault("maxDelaySeconds", 600)
    labels = managed_labels(context, plan['name'])
    role.setdefault("labels", {}).update(labels)
    template.setdefault("metadata", {}).setdefault("labels", {}).update(labels)
    spec = template["spec"]
    spec["schedulerName"] = scheduler
    container = spec["containers"][0]
    container["image"] = resolve_workload_image(config, section)
    job_name = f"{context['namespace']}-{context['logical_service_name']}"
    if plan['name'] not in C.RBG_ENGINE_PREFIXES:
        job_name += f"-{plan['name']}-{service_id}"
        live = next(
            (r for r in (facts.get("rbg") or {}).get("spec", {}).get("roles", []) if r["name"] == plan['name']),
            None,
        )
        if live:
            old_env = pod_template(live)["spec"]["containers"][0].get("env", [])
            job_name = next((e["value"] for e in old_env if e["name"] == C.ENV_JOB_NAME), job_name)
    values = {
        C.ENV_ROLE: plan['name'].replace("-", "_"),
        C.ENV_JOB_NAME: job_name,
        "MOTOR_DEPLOY_MODE": "rbg",
    }
    for purpose in (
        C.CONTROLLER,
        C.COORDINATOR,
        "coordinator_infer",
        "coordinator_obs",
    ):
        values[purpose.upper() + "_SERVICE"] = addresses[purpose]
    merge_env(container, value_env(values))
    if plan['name'] in C.RBG_ENGINE_PREFIXES:
        runtime_env = {
            **env.get(C.MOTOR_COMMON_ENV, {}),
            **env.get(f"motor_engine_{plan['name']}_env", {}),
        }
        merge_env(
            container,
            value_env(
                {
                    key: value
                    for key, value in runtime_env.items()
                    if key.startswith("MOTOR_INSTANCE_") or key in ("ASCEND_BASE_PORT", "HIXL_LISTEN_PORT")
                }
            ),
        )
        merge_env(
            container,
            value_env(
                {
                    "INFER_SERVICE_INDEX": "0",
                    "INSTANCE_ROLE": plan['name'],
                    "INFER_SERVICE_NAME": f"{context['logical_service_name']}-0",
                }
            ),
        )
        merge_env(
            container,
            [field_env("INSTANCE_INDEX", f"metadata.labels['{C.RBG_INDEX_LABEL}']")],
        )
        _hardware(template, container, plan, config, env, facts)
        set_weight_mount(
            spec,
            container,
            deploy.get(C.WEIGHT_MOUNT_PATH, C.DEFAULT_WEIGHT_MOUNT_PATH),
        )
        apply_storage_volumes(spec, container, config)
        apply_dshm_size(spec, config)
        apply_k8s_host_ipc(spec, host_ipc_enabled(env))
        if section.get(C.ENGINE_TYPE) == "sglang":
            merge_env(container, [field_env(C.ENV_SGLANG_HOST_IP, "status.podIP")])
    else:
        selector_key = {
            C.CONTROLLER: C.CONTROLLER_NODE_SELECTOR,
            C.COORDINATOR: C.COORDINATOR_NODE_SELECTOR,
            C.ROLE_KV_STORE: C.KV_POOL_NODE_SELECTOR,
            C.ROLE_KV_CONDUCTOR: C.KV_CONDUCTOR_NODE_SELECTOR,
            "mf-store": C.MF_STORE_NODE_SELECTOR,
        }[plan['name']]
        apply_node_selector_override(spec, deploy, selector_key)
        apply_a5_dns_config(spec, deploy)
    if (plan['name'] in C.RBG_ENGINE_PREFIXES) or plan['name'] == C.COORDINATOR:
        merge_env(container, kv_environment(config, addresses))
        if "mf_store" in addresses:
            protocol = "device_rdma" if deploy.get(C.HARDWARE_TYPE) in C.HARDWARE_TYPE_A2 else "sdma"
            merge_env(
                container,
                value_env(
                    {
                        C.ENV_ASCEND_MF_STORE_URL: f"tcp://{addresses['mf_store']}:{C.DEFAULT_MF_STORE_PORT}",
                        C.ENV_ASCEND_MF_TRANSFER_PROTOCOL: protocol,
                    }
                ),
            )
    if plan['name'] == C.COORDINATOR:
        set_weight_mount(
            spec,
            container,
            deploy.get(C.WEIGHT_MOUNT_PATH, C.DEFAULT_WEIGHT_MOUNT_PATH),
            read_only=True,
        )
        if "kv_conductor" in addresses:
            merge_env(
                container,
                value_env({C.ENV_KV_CONDUCTOR_SERVICE: addresses["kv_conductor"]}),
            )
        configure_render_sidecar(spec, config)
    elif plan['name'] == C.ROLE_KV_STORE:
        merge_env(container, kv_environment(config, addresses, master=True))
    elif plan['name'] == C.ROLE_KV_CONDUCTOR:
        if "kv_store" not in addresses:
            raise ValueError("KV Conductor requires a resolved kv_store address")
        merge_env(
            container,
            value_env(
                {
                    C.ENV_KVS_MASTER_SERVICE: addresses["kv_store"],
                    C.ENV_KV_CONDUCTOR_PORT: integer(
                        config[C.KV_CONDUCTOR_CONFIG][C.KV_CONDUCTOR_PORT], "kv_conductor port", 1
                    ),
                }
            ),
        )
    elif plan['name'] == "mf-store":
        merge_env(container, value_env({C.ENV_ASCEND_MF_STORE_PORT: C.DEFAULT_MF_STORE_PORT}))
    apply_user_metadata(role, section)
    return role
