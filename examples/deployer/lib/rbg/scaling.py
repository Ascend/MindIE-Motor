# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Generate and validate scaling resources for RoleBasedGroup deployments."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy

import lib.constant as C
from lib.scaling_policy import build_hpa_spec
from lib.rbg.config import apply_rbg_defaults, pod_template, role_settings, validate_instance_ports
from lib.update_config_whitelist import collect_changed_paths


def _metadata(rbg: dict) -> tuple[str, str]:
    metadata = rbg.get(C.METADATA)
    if not isinstance(metadata, dict):
        raise ValueError("rbg.metadata must be a dict")
    name = metadata.get(C.NAME)
    namespace = metadata.get(C.NAMESPACE)
    if not isinstance(name, str) or not name:
        raise ValueError("rbg.metadata.name must be a non-empty string")
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("rbg.metadata.namespace must be a non-empty string")
    return name, namespace


def _role_map(rbg: dict) -> dict[str, dict]:
    spec = rbg.get(C.SPEC)
    if not isinstance(spec, dict) or not isinstance(spec.get(C.ROLES), list):
        raise ValueError("rbg.spec.roles must be a list")
    result = {}
    for index, role in enumerate(spec[C.ROLES]):
        if not isinstance(role, dict):
            raise ValueError(f"rbg.spec.roles[{index}] must be a dict")
        name = role.get(C.NAME)
        if not isinstance(name, str) or not name:
            raise ValueError(f"rbg.spec.roles[{index}].name must be a non-empty string")
        if name in result:
            raise ValueError(f"rbg contains duplicate role '{name}'")
        result[name] = role
    return result


def _managed_labels(rbg_name: str) -> dict[str, str]:
    return {
        C.RBG_GROUP_LABEL: rbg_name,
        C.RBG_MANAGED_LABEL: C.RBG_MANAGED_VALUE,
    }


def _gang_enabled(rbg: dict) -> bool:
    annotations = rbg[C.METADATA].get(C.ANNOTATIONS, {})
    if not isinstance(annotations, dict):
        raise ValueError("rbg.metadata.annotations must be a dict")
    value = annotations.get(C.RBG_GANG_ANNOTATION, "false")
    if value not in ("true", "false"):
        raise ValueError(f"rbg annotation '{C.RBG_GANG_ANNOTATION}' must be 'true' or 'false'")
    return value == "true"


def _validate_percentage(value: object, path: str, *, allow_zero: bool = False) -> None:
    if not isinstance(value, str) or not C.RBG_PERCENTAGE_PATTERN.fullmatch(value):
        raise ValueError(f"{path} must be a percentage string")
    number = int(value[:-1])
    minimum = 0 if allow_zero else 1
    if not minimum <= number <= 100:
        interval = "[0,100]" if allow_zero else "(0,100]"
        raise ValueError(f"{path} must be in {interval}")


def _validate_int_or_percentage(value: object, path: str, *, allow_zero: bool) -> None:
    if isinstance(value, bool):
        raise ValueError(f"{path} must be an integer or percentage string")
    if isinstance(value, int):
        minimum = 0 if allow_zero else 1
        if value < minimum:
            raise ValueError(f"{path} must be >= {minimum}")
        return
    _validate_percentage(value, path, allow_zero=True)


def _pods_per_instance(role: dict) -> int:
    if "standalonePattern" in role:
        return 1
    leader_worker = role.get("leaderWorkerPattern")
    if isinstance(leader_worker, dict):
        size = leader_worker.get("size", 1)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError(f"role '{role[C.NAME]}' leaderWorkerPattern.size must be a positive integer")
        return size
    custom = role.get("customComponentsPattern")
    if not isinstance(custom, dict) or not isinstance(custom.get("components"), list) or not custom["components"]:
        raise ValueError(f"role '{role[C.NAME]}' has no supported role instance pattern")
    total = 0
    for index, component in enumerate(custom["components"]):
        if not isinstance(component, dict):
            raise ValueError(f"role '{role[C.NAME]}' component {index} must be a dict")
        size = component.get("size", 1)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise ValueError(f"role '{role[C.NAME]}' component {index} size must be a positive integer")
        total += size
    return total


def _validate_rolling_update(config: object, path: str) -> None:
    if not isinstance(config, dict):
        raise ValueError(f"{path} must be a dict")
    for field, allow_zero in (("maxSkew", False), ("partition", True), ("maxUnavailable", False)):
        if field in config:
            _validate_int_or_percentage(config[field], f"{path}.{field}", allow_zero=allow_zero)


def _validate_policies(policies: object, roles: dict[str, dict], gang_enabled: bool) -> list[dict]:
    if not isinstance(policies, list) or not policies:
        raise ValueError("coordinated_policy.policies must be a non-empty list")
    policies = deepcopy(policies)
    engine_roles = set(roles) & C.RBG_ENGINE_ROLES
    policy_names = set()
    assigned_roles = set()
    for index, policy in enumerate(policies):
        path = f"coordinated_policy.policies[{index}]"
        if not isinstance(policy, dict):
            raise ValueError(f"{path} must be a dict")
        name = policy.get(C.NAME)
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}.name must be a non-empty string")
        if name in policy_names:
            raise ValueError(f"duplicate coordinated policy name '{name}'")
        policy_names.add(name)
        policy_roles = policy.get(C.ROLES)
        if (
            not isinstance(policy_roles, list)
            or not policy_roles
            or any(not isinstance(role, str) or not role for role in policy_roles)
        ):
            raise ValueError(f"{path}.roles must be a non-empty string list")
        if len(policy_roles) != len(set(policy_roles)):
            raise ValueError(f"{path}.roles contains duplicates")
        for role_name in policy_roles:
            if role_name not in engine_roles:
                raise ValueError(f"policy role '{role_name}' is not an enabled engine role")
            if role_name in assigned_roles:
                raise ValueError(f"engine role '{role_name}' belongs to more than one coordinated policy")
            assigned_roles.add(role_name)
        strategy = policy.get("strategy")
        if not isinstance(strategy, dict):
            raise ValueError(f"{path}.strategy must be a dict")
        if "scaling" not in strategy and "rollingUpdate" not in strategy:
            raise ValueError(f"{path}.strategy requires scaling or rollingUpdate")
        scaling = strategy.get("scaling")
        if scaling is not None:
            if not isinstance(scaling, dict):
                raise ValueError(f"{path}.strategy.scaling must be a dict")
            max_skew = scaling.setdefault("maxSkew", "100%")
            progression = scaling.setdefault("progression", "OrderReady")
            _validate_percentage(max_skew, f"{path}.strategy.scaling.maxSkew")
            if progression not in ("OrderReady", "OrderScheduled"):
                raise ValueError(f"{path}.strategy.scaling.progression has unsupported value {progression!r}")
            if progression == "OrderScheduled" and any(_pods_per_instance(roles[role]) > 1 for role in policy_roles):
                raise ValueError("OrderScheduled is not supported for roles with more than one pod per instance")
            if gang_enabled and max_skew != "100%":
                raise ValueError("group gang scheduling requires scaling.maxSkew to be '100%'")
        if "rollingUpdate" in strategy:
            _validate_rolling_update(strategy["rollingUpdate"], f"{path}.strategy.rollingUpdate")
    return policies


def _policy_config(user_config: dict) -> tuple[bool, object]:
    deploy_config = user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
    if not isinstance(deploy_config, dict):
        raise ValueError(f"{C.MOTOR_DEPLOY_CONFIG} must be a dict")
    rbg_config = deploy_config.get("rbg_config", {})
    if not isinstance(rbg_config, dict):
        raise ValueError("motor_deploy_config.rbg_config must be a dict")
    if "coordinated_policy" not in rbg_config:
        return False, None
    coordinated_policy = rbg_config["coordinated_policy"]
    if not isinstance(coordinated_policy, dict):
        raise ValueError("motor_deploy_config.rbg_config.coordinated_policy must be a dict")
    return True, coordinated_policy.get("policies")


def _build_policy(user_config: dict, rbg: dict, roles: dict[str, dict], is_update: bool) -> dict | None:
    configured, policies = _policy_config(user_config)
    if is_update and not configured:
        return None
    selected = role_settings(user_config)
    engine_roles = [name for name in roles if name in C.RBG_ENGINE_ROLES and name in selected]
    if not engine_roles:
        raise ValueError("rbg must contain at least one enabled engine role")
    if not configured:
        policies = [
            {
                C.NAME: "engine-scaling",
                C.ROLES: engine_roles,
                "strategy": {"scaling": {"maxSkew": "100%", "progression": "OrderReady"}},
            }
        ]
    policies = _validate_policies(policies, roles, _gang_enabled(rbg))
    name, namespace = _metadata(rbg)
    return {
        "apiVersion": C.RBG_API_VERSION,
        C.KIND: C.RBG_POLICY_KIND,
        C.METADATA: {
            C.NAME: name,
            C.NAMESPACE: namespace,
            C.LABELS: _managed_labels(name),
        },
        C.SPEC: {"policies": policies},
    }


def _component_templates(role: dict) -> Iterator[dict]:
    if "standalonePattern" in role or "leaderWorkerPattern" in role:
        yield pod_template(role)
        return
    custom = role.get("customComponentsPattern")
    if isinstance(custom, dict):
        for component in custom.get("components", []):
            if isinstance(component, dict) and isinstance(component.get(C.TEMPLATE), dict):
                yield component[C.TEMPLATE]


def _metric_scope(role: dict, rbg_name: str) -> str:
    scopes = set()
    for template in _component_templates(role):
        labels = template.get(C.METADATA, {}).get(C.LABELS, {})
        if isinstance(labels, dict) and isinstance(labels.get(C.RBG_METRIC_LABEL), str):
            scopes.add(labels[C.RBG_METRIC_LABEL])
    if len(scopes) > 1:
        raise ValueError(f"role '{role[C.NAME]}' has inconsistent metric scope labels")
    return next(iter(scopes), f"{rbg_name}-0")


def _role_environment(role: dict) -> dict[str, str]:
    result = {}
    for template in _component_templates(role):
        containers = template.get(C.SPEC, {}).get(C.CONTAINERS, [])
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict):
                continue
            for item in container.get(C.ENV, []):
                if not isinstance(item, dict) or C.VALUE not in item:
                    continue
                name, value = item.get(C.NAME), item.get(C.VALUE)
                if isinstance(name, str) and isinstance(value, (str, int)) and not isinstance(value, bool):
                    result[name] = str(value)
    return result


def _decimal(value: object, path: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{path} must be a decimal integer")
    if isinstance(value, str) and not value.isdecimal():
        raise ValueError(f"{path} must be a decimal integer")
    result = int(value)
    if result < minimum:
        raise ValueError(f"{path} must be >= {minimum}")
    return result


def _validate_port_capacity(user_config: dict, role_name: str, role: dict, maximum: int) -> None:
    environment = _role_environment(role)
    validate_instance_ports(
        user_config,
        {f"motor_engine_{role_name}_env": environment},
        {role_name: {C.NAME: role_name, C.REPLICAS: maximum}},
    )


def _build_hpa(user_config: dict, rbg: dict, role_name: str, role: dict, policy_config: object) -> dict:
    replicas = _decimal(role.get(C.REPLICAS, 0), f"role '{role_name}' replicas", minimum=0)
    if replicas == 0:
        raise ValueError(f"cannot enable HPA for zero-replica role '{role_name}'; scale it to at least 1 first")
    name, namespace = _metadata(rbg)
    spec = build_hpa_spec(
        role_name,
        policy_config,
        replicas,
        selector={"kubernetes_namespace": namespace, "infer_huawei_com_inferservice_name": _metric_scope(role, name)},
        target_ref={"apiVersion": C.RBG_API_VERSION, C.KIND: C.RBG_SCALING_ADAPTER_KIND, C.NAME: f"{name}-{role_name}"},
    )
    maximum = spec["maxReplicas"]
    _validate_port_capacity(user_config, role_name, role, maximum)
    if "behavior" in policy_config:
        if not isinstance(policy_config["behavior"], dict):
            raise ValueError(f"scaling_policy.{role_name}.behavior must be a dict")
        spec["behavior"] = deepcopy(policy_config["behavior"])
    return {
        "apiVersion": C.RBG_HPA_API_VERSION,
        C.KIND: C.RBG_HPA_KIND,
        C.METADATA: {
            C.NAME: f"{name}-{role_name}-hpa",
            C.NAMESPACE: namespace,
            C.LABELS: _managed_labels(name),
        },
        C.SPEC: spec,
    }


def _build_hpas(user_config: dict, rbg: dict, roles: dict[str, dict], is_update: bool) -> dict | None:
    if C.SCALING_POLICY not in user_config:
        return None if is_update else {}
    scaling_policy = user_config[C.SCALING_POLICY]
    if not isinstance(scaling_policy, dict):
        raise ValueError(f"{C.SCALING_POLICY} must be a dict")
    result = {}
    for role_name, policy_config in scaling_policy.items():
        if role_name not in C.RBG_ENGINE_ROLES or role_name not in roles:
            raise ValueError(f"scaling_policy role '{role_name}' is not an enabled engine role")
        result[role_name] = _build_hpa(user_config, rbg, role_name, roles[role_name], policy_config)
    return result


def _validate_preserved_policy(current: dict | None, rbg: dict, roles: dict[str, dict]) -> None:
    if current is None or current.get("policy") is None:
        return
    policy = current["policy"]
    if not isinstance(policy, dict):
        raise ValueError("current.policy must be a dict")
    spec = policy.get(C.SPEC)
    if not isinstance(spec, dict):
        raise ValueError("current.policy.spec must be a dict")
    _validate_policies(spec.get("policies"), roles, _gang_enabled(rbg))


def _validate_current_hpa_identity(
    role_name: str,
    hpa: object,
    rbg_name: str,
    namespace: str,
    roles: dict[str, dict],
) -> dict:
    path = f"current.hpas.{role_name}"
    if role_name not in C.RBG_ENGINE_ROLES or role_name not in roles:
        raise ValueError(f"{path} does not reference an enabled engine role")
    if not isinstance(hpa, dict):
        raise ValueError(f"{path} must be a dict")
    if hpa.get("apiVersion") != C.RBG_HPA_API_VERSION or hpa.get(C.KIND) != C.RBG_HPA_KIND:
        raise ValueError(f"{path} must be an {C.RBG_HPA_API_VERSION} {C.RBG_HPA_KIND}")
    metadata = hpa.get(C.METADATA)
    if not isinstance(metadata, dict):
        raise ValueError(f"{path}.metadata must be a dict")
    expected_name = f"{rbg_name}-{role_name}-hpa"
    if metadata.get(C.NAME) != expected_name:
        raise ValueError(f"{path}.metadata.name must be {expected_name!r}")
    if metadata.get(C.NAMESPACE) != namespace:
        raise ValueError(f"{path}.metadata.namespace must be {namespace!r}")
    labels = metadata.get(C.LABELS)
    if not isinstance(labels, dict):
        raise ValueError(f"{path}.metadata.labels must be a dict")
    for label, value in _managed_labels(rbg_name).items():
        if labels.get(label) != value:
            raise ValueError(f"{path}.metadata.labels[{label!r}] must be {value!r}")
    spec = hpa.get(C.SPEC)
    if not isinstance(spec, dict):
        raise ValueError(f"{path}.spec must be a dict")
    target = spec.get("scaleTargetRef")
    if not isinstance(target, dict):
        raise ValueError(f"{path}.spec.scaleTargetRef must be a dict")
    expected_target = {
        "apiVersion": C.RBG_API_VERSION,
        C.KIND: C.RBG_SCALING_ADAPTER_KIND,
        C.NAME: f"{rbg_name}-{role_name}",
    }
    for field, value in expected_target.items():
        if target.get(field) != value:
            raise ValueError(f"{path}.spec.scaleTargetRef.{field} must be {value!r}")
    return spec


def validate_role_hpa_spec(
    user_config: dict,
    role_name: str,
    role: dict,
    spec: dict,
    rbg_name: str,
    namespace: str,
) -> None:
    path = f"current.hpas.{role_name}.spec"
    replicas = _decimal(role.get(C.REPLICAS, 0), f"role '{role_name}' replicas", minimum=0)
    if replicas == 0:
        raise ValueError(f"cannot preserve HPA for zero-replica role '{role_name}'")
    minimum = _decimal(spec.get("minReplicas", 1), f"{path}.minReplicas")
    if "maxReplicas" not in spec:
        raise ValueError(f"{path}.maxReplicas is required")
    maximum = _decimal(spec["maxReplicas"], f"{path}.maxReplicas")
    if maximum < minimum:
        raise ValueError(f"{path} requires minReplicas <= maxReplicas")
    metrics = spec.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ValueError(f"{path}.metrics must be a non-empty list")
    expected_scope = {
        "kubernetes_namespace": namespace,
        "infer_huawei_com_inferservice_name": _metric_scope(role, rbg_name),
    }
    external_metrics = 0
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict) or metric.get("type") != "External":
            continue
        external_metrics += 1
        external = metric.get("external")
        metric_reference = external.get("metric") if isinstance(external, dict) else None
        selector = metric_reference.get(C.SELECTOR) if isinstance(metric_reference, dict) else None
        labels = selector.get(C.MATCHLABELS) if isinstance(selector, dict) else None
        if not isinstance(labels, dict):
            raise ValueError(f"{path}.metrics[{index}] external selector labels must be a dict")
        for label, value in expected_scope.items():
            if labels.get(label) != value:
                raise ValueError(f"{path}.metrics[{index}] selector label {label!r} must be {value!r}")
    if external_metrics == 0:
        raise ValueError(f"{path}.metrics must contain an External metric")
    _validate_port_capacity(user_config, role_name, role, maximum)


def _validate_current_hpas(
    user_config: dict,
    current: dict | None,
    rbg: dict,
    roles: dict[str, dict],
    *,
    preserve: bool,
) -> None:
    if current is None:
        return
    current_hpas = current.get("hpas")
    if not isinstance(current_hpas, dict):
        raise ValueError("current.hpas must be a dict")
    rbg_name, namespace = _metadata(rbg)
    for role_name, hpa in current_hpas.items():
        if not isinstance(role_name, str) or not role_name:
            raise ValueError("current.hpas keys must be non-empty role names")
        spec = _validate_current_hpa_identity(role_name, hpa, rbg_name, namespace, roles)
        if preserve:
            validate_role_hpa_spec(user_config, role_name, roles[role_name], spec, rbg_name, namespace)


def _validate_current_shape(current: dict | None, *, is_update: bool) -> None:
    if current is None:
        if is_update:
            raise ValueError("current is required when updating RBG scaling resources")
        return
    if not isinstance(current, dict):
        raise ValueError("current must be a dict or None")
    missing = {"policy", "hpas"} - current.keys()
    if missing:
        raise ValueError(f"current is missing required keys: {sorted(missing)}")
    unexpected = current.keys() - {"policy", "hpas"}
    if unexpected:
        raise ValueError(f"current contains unexpected keys: {sorted(unexpected)}")
    if current["policy"] is not None and not isinstance(current["policy"], dict):
        raise ValueError("current.policy must be a dict or None")
    if not isinstance(current["hpas"], dict):
        raise ValueError("current.hpas must be a dict")


def generate_scaling_resources(
    user_config: dict,
    rbg: dict,
    *,
    is_update: bool = False,
    current: dict | None = None,
) -> dict:
    """Generate a same-name CoordinatedPolicy and role-indexed HPA resources.

    During updates, ``None`` means preserve the online resource. An empty HPA
    mapping means explicitly disable all managed HPAs.
    """
    if not isinstance(user_config, dict):
        raise ValueError("user_config must be a dict")
    if not isinstance(rbg, dict):
        raise ValueError("rbg must be a dict")
    _validate_current_shape(current, is_update=is_update)
    _metadata(rbg)
    roles = _role_map(rbg)
    policy = _build_policy(user_config, rbg, roles, is_update)
    if policy is None:
        previous = (current.get("policy") or {}).get("spec", {}).get("policies", [])
        default = _build_policy(user_config, rbg, roles, False)
        # Refresh only the generated default when the business topology changes.
        # Explicit user policies retain the existing update contract.
        if (
            len(previous) == 1
            and previous[0].get("name") == "engine-scaling"
            and previous[0].get("strategy") == default["spec"]["policies"][0]["strategy"]
            and previous[0].get("roles") != default["spec"]["policies"][0]["roles"]
        ):
            policy = default
        else:
            _validate_preserved_policy(current, rbg, roles)
    hpas = _build_hpas(user_config, rbg, roles, is_update)
    if hpas is None and any(roles[name].get("replicas") == 0 for name in current["hpas"] if name in roles):
        hpas = {
            name: deepcopy(hpa)
            for name, hpa in current["hpas"].items()
            if name in roles and roles[name].get("replicas", 0) > 0
        }
        _validate_current_hpas(user_config, {"hpas": hpas}, rbg, roles, preserve=True)
    _validate_current_hpas(user_config, current, rbg, roles, preserve=hpas is None)
    return {
        "policy": policy,
        "hpas": hpas,
    }


def _deploy_config(config: dict, path: str) -> dict:
    if not isinstance(config, dict):
        raise ValueError(f"{path} must be a dict")
    deploy_config = config.get(C.MOTOR_DEPLOY_CONFIG)
    if not isinstance(deploy_config, dict):
        raise ValueError(f"{path}.{C.MOTOR_DEPLOY_CONFIG} must be a dict")
    return deploy_config


def _instance_count(value: object, path: str, *, allow_zero: bool) -> int:
    return _decimal(value, path, minimum=0 if allow_zero else 1)


def _manual_targets(user_config: dict, baseline: dict, roles: dict[str, dict]) -> dict[str, int]:
    deploy_config = _deploy_config(user_config, "user_config")
    baseline_deploy = _deploy_config(baseline, "baseline")
    current_mode = deploy_config.get(C.DEPLOY_MODE_CONFIG_KEY)
    baseline_mode = baseline_deploy.get(C.DEPLOY_MODE_CONFIG_KEY)
    if current_mode != baseline_mode:
        raise ValueError(
            f"{C.MOTOR_DEPLOY_CONFIG}.{C.DEPLOY_MODE_CONFIG_KEY} cannot change from "
            f"{baseline_mode!r} to {current_mode!r} during manual scaling"
        )

    current_static = deepcopy(user_config)
    baseline_static = deepcopy(baseline)
    for config in (current_static, baseline_static):
        apply_rbg_defaults(config)
        static_deploy = config[C.MOTOR_DEPLOY_CONFIG]
        for key in C.RBG_INSTANCE_KEYS_BY_ROLE.values():
            static_deploy.pop(key, None)
    changed_paths = collect_changed_paths(current_static, baseline_static)
    if changed_paths:
        raise ValueError(
            "manual RBG scaling only allows instance number changes; other changes: " + ", ".join(sorted(changed_paths))
        )

    targets = {}
    for role_name, key in C.RBG_INSTANCE_KEYS_BY_ROLE.items():
        current_has_key = key in deploy_config
        baseline_has_key = key in baseline_deploy
        if current_has_key != baseline_has_key:
            raise ValueError(f"{C.MOTOR_DEPLOY_CONFIG}.{key} cannot be added or removed during manual scaling")
        if not current_has_key:
            continue
        allow_zero = role_name == C.ROLE_DECODE
        current_value = _instance_count(deploy_config[key], f"{C.MOTOR_DEPLOY_CONFIG}.{key}", allow_zero=True)
        baseline_value = _instance_count(
            baseline_deploy[key], f"baseline.{C.MOTOR_DEPLOY_CONFIG}.{key}", allow_zero=True
        )
        if current_value == baseline_value:
            continue
        _instance_count(current_value, f"{C.MOTOR_DEPLOY_CONFIG}.{key}", allow_zero=allow_zero)
        if role_name not in roles:
            raise ValueError(f"cannot scale role '{role_name}': it is not present in the online RBG")
        targets[role_name] = current_value
    return targets
