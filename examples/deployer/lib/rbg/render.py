# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Generate the RBG, business Services and startup ConfigMap without cluster writes."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

import lib.constant as C
from lib.container_snapshot import validate_container_snapshot_config
from lib.generator.kv_cache_store import (
    resolve_external_master,
    split_plain_master_address,
)
from lib.generator.kv_conductor import gen_kv_conductor_identity_env, normalize_kv_conductor_config
from lib.generator.storage import build_storage_pvc_docs
from lib.prepare_utils import configmap_assets
from lib.rbg.config import (
    apply_rbg_defaults,
    deployment_settings,
    dns_name,
    instance_ports_enabled,
    integer,
    kv_enabled,
    pod_template,
    rbg_options,
    role_settings,
    validate_instance_ports,
    validate_rbg_gang_configuration,
)
from lib.rbg.pods import load_documents, managed_labels, render_role
from lib.runtime_env import apply_decode_timeout
from lib.utils import (
    apply_service_node_port,
    get_coordinator_service_name,
    resolve_model_name,
)


def service_identity(facts: dict[str, Any]) -> str:
    identity = (
        (facts.get("runtime_configmap") or {})
        .get("metadata", {})
        .get("annotations", {})
        .get(C.RBG_SERVICE_ID_ANNOTATION)
    )
    if identity:
        return identity
    if facts.get("rbg"):
        raise ValueError("Existing RBG requires motor-config annotation motor-deployer/service-id")
    return uuid4().hex


def _shell_function(text: str, name: str, values: dict[str, Any]) -> str:
    exports = []
    for key, value in sorted(values.items()):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid shell environment variable: {key}")
        literal = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        if "\x00" in literal:
            raise ValueError(f"Environment variable {key} contains a NUL character")
        exports.append(f"    export {key}={shlex.quote(literal)}")
    replacement = "function " + name + "() {\n" + "\n".join(exports) + "\n}"
    pattern = re.compile(r"^function " + re.escape(name) + r"\(\)\s*\{.*?^\}", re.MULTILINE | re.DOTALL)
    if not pattern.search(text):
        # Pristine startup assets contain calls only; legacy rendering also inserts definitions.
        if text.startswith("#!"):
            shebang, body = text.split("\n", 1)
            return shebang + "\n" + replacement + "\n" + body
        return replacement + "\n" + text
    return pattern.sub(lambda _: replacement, text, count=1)


def render_configmap(
    context: dict,
    config: dict[str, Any],
    env_config: dict[str, Any],
    identity: str,
) -> dict[str, Any]:
    env = deepcopy(env_config)
    for section, values in env.items():
        if section == "version":
            continue
        if not isinstance(values, dict):
            raise TypeError(f"env.json.{section} must be an object")
        conflicts = set(values) & C.RBG_PROTECTED_ENV
        if conflicts:
            raise ValueError(f"env.json.{section} overrides managed fields: {sorted(conflicts)}")
    engine = config.get(C.MOTOR_ENGINE_PREFILL_CONFIG) or config.get(C.MOTOR_ENGINE_UNION_CONFIG, {})
    common = env.setdefault(C.MOTOR_COMMON_ENV, {})
    common.update(
        {
            C.ENV_ENGINE_TYPE: engine.get(C.ENGINE_TYPE, "Unknown"),
            C.ENV_MODEL_NAME: resolve_model_name(engine),
            C.ENV_NORTH_PLATFORM: config.get("north_config", {}).get("name", ""),
            C.ENV_SERVICE_ID: identity,
        }
    )
    env.setdefault("motor_engine_union_env", dict(env.get(C.MOTOR_ENGINE_PREFILL_ENV, {})))
    conductor_port = config.get(C.KV_CONDUCTOR_CONFIG, {}).get(C.KV_CONDUCTOR_PORT, 0)
    if conductor_port:
        conductor = normalize_kv_conductor_config(config)
        conductor_env = env.setdefault("motor_kv_conductor_env", {})
        conductor_env[C.ENV_KV_CONDUCTOR_PORT] = integer(conductor_port, "kv_conductor port", 1)
        conductor_env.update({item[C.NAME]: item[C.VALUE] for item in gen_kv_conductor_identity_env(conductor)})
    data = {name: path.read_text(encoding="utf-8") for path, name in configmap_assets(C.RBG_DEPLOYER_ROOT)}
    for filename, functions in C.RBG_FUNCTIONS.items():
        for section, function in functions:
            values = {**common, **env.get(section, {})}
            if function == "set_common_env":
                # Preserve the role-specific Pod values before common.sh starts config synchronization.
                values = {key: value for key, value in values.items() if not key.startswith("MOTOR_INSTANCE_")}
            data[filename] = _shell_function(data[filename], function, values)
    data["user_config.json"] = json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    data["env.json"] = json.dumps(env, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": C.MOTOR_CONFIG_CONFIGMAP_NAME,
            "namespace": context['namespace'],
            "labels": managed_labels(context),
            "annotations": {C.RBG_SERVICE_ID_ANNOTATION: identity},
        },
        "data": data,
    }


def startup_digest(data: dict[str, str], config: dict[str, Any], role: str) -> str:
    """Do not restart engines for replica, HPA or coordination-only changes."""
    effective = deepcopy(config)
    deploy = effective.get(C.MOTOR_DEPLOY_CONFIG, {})
    for key in list(deploy):
        if key.endswith("instances_num") or key == "rbg_config":
            del deploy[key]
    if C.HYBRID_INSTANCES_NUM in config.get(C.MOTOR_DEPLOY_CONFIG, {}):
        deploy[C.HYBRID_INSTANCES_NUM] = True  # Mode changes require a restart; count changes do not.
    effective.pop("scaling_policy", None)
    if role in C.RBG_ENGINE_PREFIXES:
        effective.pop(C.MOTOR_COORDINATOR_CONFIG, None)
        # NodeManager consumes Controller API endpoints, FT switches and its recovery time budget.
        controller = effective.get(C.MOTOR_CONTROLLER_CONFIG, {})
        effective[C.MOTOR_CONTROLLER_CONFIG] = {
            key: value
            for key, value in controller.items()
            if key in (C.FAULT_TOLERANCE_CONFIG, "instance_config", "api_config")
        }
    # Runtime config contains all role sections; only the selected role's section affects its digest.
    for key in list(effective):
        if key.startswith("motor_engine_") and key != f"motor_engine_{role}_config":
            del effective[key]
    scripts = {key: value for key, value in data.items() if key not in ("user_config.json", "env.json")}
    section = {
        C.ROLE_KV_STORE: "motor_kv_cache_store_env",
        C.ROLE_KV_CONDUCTOR: "motor_kv_conductor_env",
        "mf-store": "motor_mf_store_env",
    }.get(role, f"motor_engine_{role}_env" if role in C.RBG_ENGINE_PREFIXES else f"motor_{role}_env")
    consumed = {C.MOTOR_COMMON_ENV, section}
    if role in C.RBG_ENGINE_PREFIXES:
        consumed.add("motor_mf_store_env")  # engine.sh calls set_mf_store_env.
    for filename, functions in C.RBG_FUNCTIONS.items():
        for section, function in functions:
            if section not in consumed:
                scripts[filename] = _shell_function(scripts[filename], function, {})
    payload = json.dumps({"config": effective, "assets": scripts}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_templates() -> tuple[dict, dict, list[dict], list[dict]]:
    documents = load_documents("rbg_template.yaml")
    rbg = next(doc for doc in documents if doc["kind"] == C.RBG_KIND)
    if rbg["apiVersion"] != C.RBG_API_VERSION:
        raise ValueError("rbg_template.yaml must use workloads.x-k8s.io/v1alpha2")
    roles = {role["name"]: role for role in rbg["spec"]["roles"]}
    services = [doc for doc in documents if doc["kind"] == "Service"]
    rbac = [doc for doc in documents if doc["kind"] in {"ServiceAccount", "ClusterRole", "ClusterRoleBinding"}]
    return rbg, roles, services, rbac


def build_services(
    context: dict,
    roles: dict[str, dict],
    config: dict[str, Any],
    facts: dict[str, Any],
    templates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    services = []
    addresses = dict(facts.get("external_service_addresses", {}))
    deploy = config[C.MOTOR_DEPLOY_CONFIG]
    for service in templates:
        metadata, spec = service["metadata"], service["spec"]
        name = metadata["labels"].pop(C.RBG_TEMPLATE_ROLE_LABEL)
        if name not in roles:
            continue
        ports = spec["ports"]
        port = ports[0]["port"]
        purpose = name.replace("-", "_")
        config_key = None
        if name == C.COORDINATOR:
            port_suffixes = {
                C.COORDINATOR_INFER_SERVICE_PORT: "_infer",
                C.COORDINATOR_MANAGEMENT_SERVICE_PORT: "",
                C.COORDINATOR_OBSERVABILITY_SERVICE_PORT: "_obs",
            }
            try:
                purpose += port_suffixes[port]
            except KeyError as exc:
                raise ValueError(
                    f"rbg_template.yaml: coordinator Service {metadata['name']!r} has unsupported "
                    f"spec.ports[0].port={port!r}; expected one of {sorted(port_suffixes)}"
                ) from exc
            if port == C.COORDINATOR_INFER_SERVICE_PORT:
                metadata["name"] = get_coordinator_service_name(deploy)
                config_key = C.COORDINATOR_INFER_NODE_PORT
            elif port == C.COORDINATOR_OBSERVABILITY_SERVICE_PORT:
                config_key = C.COORDINATOR_OBS_NODE_PORT
        elif name == C.CONTROLLER and port == 1027:
            purpose += "_obs"
            config_key = C.CONTROLLER_OBSERVABILITY_NODE_PORT
        if name == C.ROLE_KV_STORE:
            kv = config.get(C.KV_CACHE_STORE_CONFIG, {})
            values = [kv.get(C.KV_CACHE_STORE_PORT, C.DEFAULT_KV_CACHE_STORE_PORT)]
            if kv.get(C.KV_STORE_BACKEND, C.DEFAULT_KV_STORE_BACKEND) == C.MMC_STORE_BACKEND:
                values.extend(
                    [
                        kv.get(C.MMC_CONFIG_STORE_PORT_KEY, C.DEFAULT_MMC_CONFIG_STORE_PORT),
                        kv.get(C.MMC_METRICS_PORT_KEY, C.DEFAULT_MMC_METRICS_PORT),
                    ]
                )
            for item, value in zip(ports, values):
                item["port"] = item["targetPort"] = integer(value, "kv_store port", 1)
        if name == C.ROLE_KV_CONDUCTOR:
            ports[0]["port"] = ports[0]["targetPort"] = integer(
                config[C.KV_CONDUCTOR_CONFIG][C.KV_CONDUCTOR_PORT],
                "kv_conductor port",
                1,
            )
        if name != "mf-store":
            metadata["name"] = f"{metadata['name']}-{context['logical_service_name']}-0-{name}"
        dns_name(metadata["name"], "Service name")
        if metadata["name"] in {f"{context['rbg_name']}-{role}" for role in roles}:
            raise ValueError("Business Service conflicts with a native RBG headless Service")
        metadata["namespace"] = context['namespace']
        metadata.setdefault("labels", {}).update(managed_labels(context, name))
        # ServiceMonitor selects this label on the Service, not its Pod selector.
        if purpose.endswith("_obs"):
            metadata["labels"]["app"] = spec.get("selector", {}).get("app", name)
        elif name in (C.CONTROLLER, C.COORDINATOR):
            # Do not make ServiceMonitor scrape the same Pod via its management/inference Service as well.
            metadata["labels"].pop("app", None)
        spec.setdefault("selector", {}).update({C.RBG_GROUP_LABEL: context['rbg_name'], C.RBG_ROLE_LABEL: name})
        spec["selector"].pop("infer.huawei.com/active", None)
        if config_key:
            apply_service_node_port(service, deploy, config_key)
        existing = facts.get("services", {}).get(metadata["name"])
        if existing and (not config_key or config_key not in deploy):
            live_ports = {(p.get("name"), p["port"]): p for p in existing["spec"]["ports"]}
            for item in ports:
                live = live_ports.get((item.get("name"), item["port"]), {})
                if spec.get("type") == "NodePort" and "nodePort" in live:
                    item["nodePort"] = live["nodePort"]
        for item in ports:
            if not 1 <= item["port"] <= 65535:
                raise ValueError("Service port must be between 1 and 65535")
        services.append(service)
        addresses[purpose] = f"{metadata['name']}.{context['namespace']}.svc.cluster.local"
    return services, addresses


__all__ = [
    "generate_rbg_resources",
    "write_rbg_resources",
]


def _scheduling(config: dict[str, Any], facts: dict[str, Any]) -> tuple[bool, str, str]:
    options = rbg_options(config)
    live = facts.get("rbg")
    annotations = (live or {}).get("metadata", {}).get("annotations", {})
    old_gang = annotations.get(C.RBG_GANG_ANNOTATION, "false") == "true" if live else True
    old_scheduler = (
        pod_template(live["spec"]["roles"][0])["spec"].get("schedulerName", "volcano") if live else "volcano"
    )
    gang, scheduler = (
        options.get("gang_scheduling", old_gang),
        options.get("scheduler_name", old_scheduler),
    )
    queue = config[C.MOTOR_DEPLOY_CONFIG].get(C.SCHEDULING_QUEUE, annotations.get(C.RBG_VOLCANO_QUEUE_ANNOTATION, ""))
    if queue is None:
        queue = ""
    if not isinstance(queue, str):
        raise TypeError("scheduling_queue must be a string")
    queue = queue.strip()
    if not isinstance(gang, bool) or not isinstance(scheduler, str) or not scheduler:
        raise ValueError("rbg_config.gang_scheduling must be boolean and scheduler_name must be nonempty")
    if live and (
        gang != old_gang or scheduler != old_scheduler or queue != annotations.get(C.RBG_VOLCANO_QUEUE_ANNOTATION, "")
    ):
        raise ValueError("Changing gang, scheduler or queue requires redeployment")
    if scheduler == "default-scheduler" and queue:
        raise ValueError("default-scheduler cannot consume a Volcano queue")
    return gang, scheduler, queue


def _runtime_rbac(context: dict, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    renames = {}
    for doc in docs:
        metadata = doc["metadata"]
        metadata.setdefault("labels", {}).update(managed_labels(context))
        if doc["kind"].startswith("Cluster"):
            old = metadata["name"]
            metadata["name"] = dns_name(f"{old}-{context['namespace']}-{context['rbg_name']}", "runtime RBAC name")
            renames[old] = metadata["name"]
            metadata.pop("namespace", None)
        else:
            metadata["namespace"] = context['namespace']
        for subject in doc.get("subjects", []):
            if subject["kind"] == "ServiceAccount":
                subject["namespace"] = context['namespace']
    for doc in docs:
        if "roleRef" in doc:
            doc["roleRef"]["name"] = renames.get(doc["roleRef"]["name"], doc["roleRef"]["name"])
    return docs


def _external_kv(
    context: dict,
    config: dict[str, Any],
    facts: dict[str, Any],
    roles: dict,
) -> None:
    if not kv_enabled(config):
        return
    kv = config.get(C.KV_CACHE_STORE_CONFIG, {})
    explicit = resolve_external_master(kv)
    target = kv.get(C.TARGET_JOB_ID)
    addresses = facts.setdefault("external_service_addresses", {})
    if explicit:
        host, _ = split_plain_master_address(explicit)
        if host:
            addresses["kv_store"] = host
        roles.pop(C.ROLE_KV_STORE, None)
    elif target and target != context['namespace']:
        if facts.get("kv_store_reusable") is False:
            addresses.pop("kv_store", None)
            return
        if not addresses.get("kv_store"):
            raise ValueError("External KV target requires facts.external_service_addresses.kv_store")
        roles.pop(C.ROLE_KV_STORE, None)
    elif addresses.get("kv_store"):
        roles.pop(C.ROLE_KV_STORE, None)


def _check_existing_roles(roles: list[dict[str, Any]], live: dict[str, Any] | None) -> None:
    if not live:
        return
    old_roles = {role["name"]: role for role in live["spec"]["roles"]}
    removed = old_roles.keys() - {role["name"] for role in roles}
    if removed:
        raise ValueError(f"Keep disabled roles in rbg_template.yaml with zero replicas: {sorted(removed)}")
    for role in roles:
        old = old_roles.get(role["name"])
        if not old or not old.get("replicas") or not role.get("replicas"):
            continue
        for name in ("standalonePattern", "customComponentsPattern", "leaderWorkerPattern"):
            if (name in old) != (name in role):
                raise ValueError("Changing role pattern requires redeployment")
            if name not in role or name == "standalonePattern":
                continue
            before, after = old[name], role[name]
            if name == "leaderWorkerPattern":
                old_size, new_size = before.get("size", 1), after.get("size", 1)
            else:
                old_size, new_size = before["components"][0]["size"], after["components"][0]["size"]
            if old_size != new_size or before.get("restartPolicyConfig") != after.get("restartPolicyConfig"):
                raise ValueError("Changing member count or restart policy requires redeployment")


def generate_rbg_resources(
    user_config: dict[str, Any],
    env_config: dict[str, Any],
    *,
    facts: dict[str, Any] | None = None,
) -> dict:
    """Return a complete resource bundle without API writes, shell execution or global state mutation."""
    from lib.config_validator import validate_kv_cache_store_config

    from lib.log_mount import apply_log_mount, common_log_root

    config, env, facts = deepcopy(user_config), deepcopy(env_config), deepcopy(facts or {})
    for values in env.values():
        if isinstance(values, dict) and C.ENV_MOTOR_INSTANCE_PORTS_ENABLE in values:
            values[C.ENV_MOTOR_INSTANCE_PORTS_ENABLE] = (
                "1" if instance_ports_enabled(values[C.ENV_MOTOR_INSTANCE_PORTS_ENABLE]) else "0"
            )
    context = deployment_settings(config)
    env.setdefault("motor_engine_union_env", deepcopy(env.get(C.MOTOR_ENGINE_PREFILL_ENV, {})))
    validate_kv_cache_store_config(config)
    apply_decode_timeout(config, env)
    validate_container_snapshot_config(config)
    if (config.get(C.MOTOR_CONTAINER_SNAPSHOT_CONFIG) or {}).get(C.ENABLE_SNAPSHOT, False):
        raise ValueError("RBG deployment does not implement container snapshot")
    apply_rbg_defaults(config)
    live = facts.get("rbg")
    if live:
        metadata = live["metadata"]
        if (
            metadata.get("name") != context['rbg_name']
            or metadata.get("namespace") != context['namespace']
            or metadata.get("labels", {}).get(C.RBG_METRIC_LABEL) != f"{context['logical_service_name']}-0"
        ):
            raise ValueError("Updating RBG cannot change deployment identity")
    roles = role_settings(config)
    _external_kv(context, config, facts, roles)
    for name in roles:
        dns_name(f"{context['rbg_name']}-{name}", "native role Service name")
    gang, scheduler, queue = _scheduling(config, facts)
    rbg, catalog, service_templates, rbac = _load_templates()
    missing_roles = roles.keys() - catalog.keys()
    if missing_roles:
        raise ValueError(f"rbg_template.yaml is missing required roles: {sorted(missing_roles)}")
    services, addresses = build_services(context, roles, config, facts, service_templates)
    identity = service_identity(facts)
    runtime = render_configmap(context, config, env, identity)
    log_root = common_log_root(env)
    rendered = []
    for name, plan in roles.items():
        role = render_role(
            catalog[name],
            plan,
            context,
            config,
            env,
            facts,
            addresses,
            scheduler,
            identity,
        )
        apply_log_mount(pod_template(role)["spec"], log_root)
        pod_template(role).setdefault("metadata", {}).setdefault("annotations", {})[C.RBG_STARTUP_DIGEST_ANNOTATION] = (
            startup_digest(runtime["data"], config, name)
        )
        rendered.append(role)
    # Like the ISS template, retain disabled roles at zero replicas. Preserve an
    # existing template while stopping it; activation renders the requested config.
    old_roles = {role["name"]: role for role in (live or {}).get("spec", {}).get("roles", [])}
    for name, template in catalog.items():
        if name in roles:
            continue
        role = deepcopy(old_roles.get(name, template))
        role["replicas"] = 0
        role["scalingAdapter"] = {"enable": False}
        pod_template(role)["spec"]["schedulerName"] = scheduler
        rendered.append(role)
    _check_existing_roles(rendered, facts.get("rbg"))
    annotations = {C.RBG_GANG_ANNOTATION: str(gang).lower()}
    if queue:
        annotations[C.RBG_VOLCANO_QUEUE_ANNOTATION] = queue
    rbg["metadata"].update(name=context['rbg_name'], namespace=context['namespace'])
    rbg["metadata"].setdefault("labels", {}).update(managed_labels(context))
    rbg["metadata"].setdefault("annotations", {}).update(annotations)
    rbg["spec"]["roles"] = rendered
    validate_rbg_gang_configuration(rbg)
    validate_instance_ports(config, env, roles)
    if live:
        old_roles = {role["name"]: role for role in live["spec"]["roles"]}
        for role in rendered:
            if role["replicas"] > 0 and "scalingAdapter" in old_roles.get(role["name"], {}):
                role["scalingAdapter"] = deepcopy(old_roles[role["name"]]["scalingAdapter"])
    prerequisites = [*_runtime_rbac(context, rbac), runtime]
    prerequisites.extend(build_storage_pvc_docs(str(C.RBG_TEMPLATE_ROOT / "storage_pvc_template.yaml"), config))
    return {"rbg": rbg, "services": services, "prerequisites": prerequisites, "user_config": config}


def write_rbg_resources(resources: dict, output_dir: str | Path) -> list[Path]:
    """Write the rendered RBG, business Services and prerequisite resources to local YAML files."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "prerequisites": resources["prerequisites"],
        "services": resources["services"],
        "rbg": [resources["rbg"]],
    }
    paths = []
    for name, items in documents.items():
        path = output_dir / f"{name}.yaml"
        path.write_text(yaml.safe_dump_all(items, sort_keys=False, allow_unicode=True), encoding="utf-8")
        paths.append(path)
    return paths
