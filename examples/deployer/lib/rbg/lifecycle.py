# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Concrete RBG deployment, update and scoped cleanup operations."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path

import lib.constant as C
from lib.generator.k8s_utils import run_cmd_get_output
from lib.nodeport_allocator import check_rbg_nodeports
from lib.rbg.config import deployment_settings, dns_name
from lib.rbg.scaling import _deploy_config, _instance_count, _manual_targets, _role_map, _validate_port_capacity
from lib.utils import logger


def get_rbg_baseline_config(user_config: dict) -> dict | None:
    """Read the deployed RBG config; only a missing ConfigMap returns None."""
    namespace = deployment_settings(user_config)["namespace"]
    out = run_cmd_get_output(
        [
            "kubectl",
            "get",
            "configmap",
            C.MOTOR_CONFIG_CONFIGMAP_NAME,
            "-n",
            namespace,
            "--ignore-not-found=true",
            "-o",
            "json",
        ]
    )
    if not out:
        return None
    try:
        obj = json.loads(out)
        if obj["metadata"]["name"] != C.MOTOR_CONFIG_CONFIGMAP_NAME or obj["metadata"]["namespace"] != namespace:
            raise ValueError("Configuration target mismatch")
        config = json.loads(obj["data"]["user_config.json"])
        if not isinstance(config, dict):
            raise TypeError("user_config.json must be an object")
        return config
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Read ConfigMap/motor-config in {namespace} failed: {exc}") from exc


def _read_managed_resource(resource: str, name: str, settings: dict) -> dict:
    found = _deletion_query(resource, settings["namespace"], name=name)
    if not found:
        raise FileNotFoundError(f"{resource}/{name} does not exist in {settings['namespace']}")
    obj = found[0]
    metadata = obj["metadata"]
    labels = metadata.get("labels", {})
    if labels.get(C.RBG_MANAGED_LABEL) != C.RBG_MANAGED_VALUE or labels.get(C.RBG_GROUP_LABEL) != settings["rbg_name"]:
        raise ValueError(f"{resource}/{name} is not managed by this RBG deployment")
    expected = (
        C.RBG_KIND
        if resource.startswith("rolebasedgroups")
        else {"configmap": "ConfigMap", "service": "Service"}[resource]
    )
    api = C.RBG_API_VERSION if expected == C.RBG_KIND else "v1"
    if obj.get("kind") != expected or obj.get("apiVersion") != api or metadata.get("deletionTimestamp"):
        raise ValueError(f"Invalid or deleting {resource}/{name}")
    if not metadata.get("uid") or not metadata.get("resourceVersion"):
        raise ValueError(f"Missing UID/resourceVersion on {resource}/{name}")
    return obj


def _patch_rbg_object(resource: str, live: dict, patch: list[dict], *, dry_run: bool = False) -> dict:
    """Submit one guarded native patch; confirm an uncertain write without resending."""
    meta = live["metadata"]
    args = ["kubectl", "patch", resource, meta["name"]]
    if meta.get("namespace"):
        args.extend(["-n", meta["namespace"]])
    if dry_run:
        args.append("--dry-run=server")
    uncertain = False
    try:
        with tempfile.TemporaryDirectory(prefix="motor-patch-") as directory:
            path = Path(directory) / "patch.json"
            path.write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")
            result = json.loads(run_cmd_get_output([*args, "--type=json", "--patch-file", str(path), "-o", "json"]))
    except RuntimeError as exc:
        if dry_run or not any(word in str(exc).lower() for word in ("timed out", "timeout", "deadline exceeded")):
            raise
        uncertain = True
        try:
            remaining = _deletion_query(resource, meta.get("namespace"), name=meta["name"])
        except (RuntimeError, ValueError) as query_error:
            raise RuntimeError(f"Patch outcome unconfirmed: cannot read {resource}/{meta['name']}") from query_error
        if not remaining:
            raise RuntimeError(f"Patch outcome unconfirmed: {resource}/{meta['name']} is absent") from exc
        result = remaining[0]
    actual_meta = result["metadata"]
    if (
        result.get("apiVersion") != live.get("apiVersion")
        or result.get("kind") != live.get("kind")
        or actual_meta.get("deletionTimestamp")
        or not actual_meta.get("resourceVersion")
        or any(actual_meta.get(key) != meta.get(key) for key in ("name", "namespace", "uid"))
    ):
        raise RuntimeError(f"Patch returned a replaced {resource}/{meta['name']}")
    if not uncertain:
        return result
    for change in patch:
        if change["op"] == "test":
            continue
        actual = result
        try:
            for part in change["path"].split("/")[1:]:
                key = part.replace("~1", "/").replace("~0", "~")
                actual = actual[int(key)] if isinstance(actual, list) else actual[key]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Patch outcome unconfirmed at {change['path']}") from exc
        if not _rbg_contains(actual, change["value"]):
            raise RuntimeError(f"Patch outcome unconfirmed at {change['path']}")
    return result


def _rbg_patch_tests(live: dict) -> list[dict]:
    return [
        {"op": "test", "path": "/metadata/uid", "value": live["metadata"]["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": live["metadata"]["resourceVersion"]},
    ]


def _rbg_contains(actual: object, desired: object) -> bool:
    """Compare declared native fields, allowing API defaulted object members."""
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(k in actual and _rbg_contains(actual[k], v) for k, v in desired.items())
    if isinstance(desired, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(desired)
            and all(_rbg_contains(a, b) for a, b in zip(actual, desired))
        )
    return actual == desired


def _native_service_names(name: str, roles: Iterable[str]) -> set[str]:
    """Include current and compatible headless names without claiming their ownership."""
    return {(prefix + name + "-" + role)[:63].rstrip("-") for role in roles for prefix in ("", "s-")}


def update_rbg_runtime_config(user_config: dict, baseline_config: dict) -> None:
    """Patch only the effective config already merged by the deployment entry point.

    UID and resourceVersion tests reject replacement or concurrent ConfigMap edits.
    API failures and invalid online data propagate without being treated as absence.
    """
    settings = deployment_settings(user_config)
    _read_managed_resource(C.RBG_RESOURCE, settings["rbg_name"], settings)
    runtime = _read_managed_resource("configmap", C.MOTOR_CONFIG_CONFIGMAP_NAME, settings)
    metadata = runtime["metadata"]
    if not metadata.get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION):
        raise ValueError("motor-config is missing the persisted service identity")
    baseline = json.loads(runtime["data"]["user_config.json"])
    if deployment_settings(baseline) != settings:
        raise ValueError("Runtime configuration belongs to a different deployment identity")
    if baseline != baseline_config:
        raise RuntimeError("Runtime configuration conflict: motor-config changed since it was read; reload and retry")
    content = json.dumps(user_config, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if user_config == baseline:
        return
    patch = _rbg_patch_tests(runtime) + [
        {"op": "replace", "path": "/data/user_config.json", "value": content},
    ]
    _patch_rbg_object("configmap", runtime, patch)


def _service_field_patch(current: dict, desired: dict, path: str) -> list[dict]:
    """Overlay explicit generator fields; omission never proves removal ownership."""
    changes = []
    for key, value in desired.items():
        field = path + "/" + key.replace("~", "~0").replace("/", "~1")
        if value is None:
            raise ValueError(f"{field}: null/removal intent is not supported yet")
        if key not in current:
            defaults = {
                "/spec/type": "ClusterIP",
                "/spec/sessionAffinity": "None",
                "/spec/publishNotReadyAddresses": False,
            }
            if field in defaults and value == defaults[field]:
                continue
            if path.startswith("/spec/ports/") and (
                (key == "protocol" and value == "TCP") or (key == "targetPort" and value == current.get("port"))
            ):
                continue
            if isinstance(value, dict):
                _service_field_patch({}, value, field)
            changes.append({"op": "add", "path": field, "value": deepcopy(value)})
        elif isinstance(value, dict) and isinstance(current[key], dict):
            changes.extend(_service_field_patch(current[key], value, field))
        elif field == "/spec/ports" and isinstance(value, list):
            if len(value) != len(current[key]):
                raise ValueError("Port list membership change requires explicit removal semantics")
            for index, port in enumerate(value):
                changes.extend(_service_field_patch(current[key][index], port, field + f"/{index}"))
        elif current[key] != value:
            changes.append({"op": "replace", "path": field, "value": deepcopy(value)})
    return changes


def _update_rbg_service(user_config: dict, service: dict) -> None:
    """Update an existing rendered business Service without creating it or changing ownership.

    Missing fields and ports are preserved. Supported type upgrades retain allocations;
    additions/renames and other transitions require redeployment.
    """
    settings = deployment_settings(user_config)
    name = service["metadata"]["name"]
    target = f"Service/{name} in {settings['namespace']}"
    try:
        _patch_rbg_service(settings, service)
    except (RuntimeError, ValueError, KeyError, TypeError, FileNotFoundError) as exc:
        raise RuntimeError(f"Update {target} failed: {exc}") from exc


def _patch_rbg_service(settings: dict, service: dict, *, validate_only: bool = False) -> None:
    """Check deployment/owner evidence and submit guarded field differences."""
    metadata = service["metadata"]
    name = metadata["name"]
    role = metadata.get("labels", {}).get(C.RBG_ROLE_LABEL)
    invalid_identity = (
        (service.get("apiVersion") != "v1")
        or (service.get("kind") != "Service")
        or (metadata.get("namespace") != settings["namespace"])
        or (metadata.get("labels", {}).get(C.RBG_MANAGED_LABEL) != C.RBG_MANAGED_VALUE)
    )
    if invalid_identity or (metadata.get("labels", {}).get(C.RBG_GROUP_LABEL) != settings["rbg_name"]) or (not role):
        raise ValueError("Desired Service has invalid deployment identity")
    rbg = _read_managed_resource(C.RBG_RESOURCE, settings["rbg_name"], settings)
    roles = {item["name"] for item in rbg["spec"]["roles"]}
    native_names = _native_service_names(settings["rbg_name"], roles)
    if role not in roles or name in native_names:
        raise ValueError("Not an eligible business Service (role/native headless name)")
    runtime = _read_managed_resource("configmap", C.MOTOR_CONFIG_CONFIGMAP_NAME, settings)
    if (
        not runtime["metadata"].get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION)
        or deployment_settings(json.loads(runtime["data"]["user_config.json"])) != settings
    ):
        raise ValueError("Missing or conflicting persisted deployment identity")
    live = _read_managed_resource("service", name, settings)
    lm = live["metadata"]
    if lm.get("labels", {}).get(C.RBG_ROLE_LABEL) != role:
        raise ValueError("Service role does not match the declared role")
    _verify_rbg_owner(live, rbg)
    # Metadata outside the supported update fields requires separate ownership checks.
    for key, value in metadata.items():
        if key not in {
            "name",
            "namespace",
            "labels",
            "annotations",
            "uid",
            "resourceVersion",
            "creationTimestamp",
            "generation",
            "managedFields",
            "deletionTimestamp",
        } and value != lm.get(key):
            raise ValueError(f"metadata.{key} update requires separate ownership semantics")
    desired_spec = deepcopy(service["spec"])
    online_spec = deepcopy(live["spec"])
    old_type = online_spec.get("type", "ClusterIP")
    new_type = desired_spec.get("type", old_type)
    # NodePort -> LoadBalancer preserves existing allocations. Reverse conversion
    # requires deciding whether LB-only fields should be removed, so remains excluded.
    if new_type != old_type and (old_type, new_type) not in {("ClusterIP", "NodePort"), ("NodePort", "LoadBalancer")}:
        raise ValueError("Unsupported Service type conversion; redeploy required")
    if new_type != old_type and online_spec.get("clusterIP") == "None":
        raise ValueError("Headless type conversion unsupported; redeploy required")
    for key in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy", "healthCheckNodePort"):
        if key in desired_spec and desired_spec[key] != online_spec.get(key):
            raise ValueError(f"spec.{key} address transition is outside the supported update subset; redeploy required")
    if (
        "loadBalancerClass" in desired_spec
        and desired_spec["loadBalancerClass"] != online_spec.get("loadBalancerClass")
        and (new_type != "LoadBalancer" or old_type == "LoadBalancer")
    ):
        raise ValueError("loadBalancerClass can only be selected when entering LoadBalancer type; redeploy required")
    affinity = desired_spec.get("sessionAffinity", online_spec.get("sessionAffinity", "None"))
    affinity_config = desired_spec.get("sessionAffinityConfig", online_spec.get("sessionAffinityConfig"))
    if affinity == "None" and affinity_config is not None:
        raise ValueError(
            "sessionAffinity=None conflicts with retained sessionAffinityConfig; align explicit removal intent"
        )
    for key, value in ((C.RBG_GROUP_LABEL, settings["rbg_name"]), (C.RBG_ROLE_LABEL, role)):
        if {**online_spec.get("selector", {}), **desired_spec.get("selector", {})}.get(key) != value:
            raise ValueError("Service selector must retain group and role isolation")
    if "infer.huawei.com/active" in desired_spec.get("selector", {}):
        raise ValueError("Unsupported active selector")

    # Match ports by stable name, falling back to protocol/port for unnamed ports.
    # Keep online order so field patches never transfer allocations between members.
    def port_key(port: dict) -> tuple:
        return ("name", port["name"]) if port.get("name") else (port.get("protocol", "TCP"), port["port"])

    old_ports = online_spec.get("ports", [])
    declared = desired_spec.get("ports", [])
    if len(old_ports) == len(declared) == 1 and not declared[0].get("name"):
        # A single unnamed port has no competing member to receive its allocation.
        old_by_key = {("single",): old_ports[0]}
        new_by_key = {("single",): declared[0]}
    else:
        old_by_key = {port_key(p): p for p in old_ports}
        new_by_key = {}
        for port in declared:
            key = port_key(port)
            if not port.get("name"):
                matches = [port_key(old) for old in old_ports if (old.get("protocol", "TCP"), old["port"]) == key]
                if len(matches) == 1:
                    key = matches[0]
            if key in new_by_key:
                raise ValueError("Duplicate or ambiguous Service port identity")
            new_by_key[key] = port
    if len(old_by_key) != len(old_ports) or len(new_by_key) != len(declared):
        raise ValueError("Duplicate or ambiguous Service port identity")
    if not new_by_key.keys() <= old_by_key.keys():
        raise ValueError("Adding or renaming Service ports requires redeployment")
    # Omission is preservation, including whole ports and their allocated nodePort.
    desired_spec["ports"] = [{**old, **new_by_key.get(key, {})} for key, old in old_by_key.items()]
    for old, port in zip(old_ports, desired_spec["ports"]):
        if old.get("nodePort") and not port.get("nodePort"):
            raise ValueError("Explicit nodePort release is not supported")
    changes = _service_field_patch(online_spec, desired_spec, "/spec")
    for section in ("labels", "annotations"):
        declared = metadata.get(section, {})
        if (
            section == "annotations"
            and C.RBG_COORDINATION_ANNOTATION in declared
            and declared[C.RBG_COORDINATION_ANNOTATION] != lm.get(section, {}).get(C.RBG_COORDINATION_ANNOTATION)
        ):
            raise ValueError("Coordination request annotation requires the Policy update path")
        changes.extend(_service_field_patch(lm, {section: declared}, "/metadata") if declared else [])
    if not changes:
        return
    effective = deepcopy(service)
    effective["spec"] = {**online_spec, **desired_spec}
    check_rbg_nodeports([effective], current={name: live})
    if validate_only:
        return
    _patch_rbg_object("service", live, _rbg_patch_tests(live) + changes)


def _deletion_query(
    resource: str,
    namespace: str | None,
    *,
    name: str | None = None,
    selector: str | None = None,
    timeout_seconds: float = 60,
) -> list[dict]:
    """Read deletion candidates; only an explicit empty result means absence."""
    command = ["kubectl", "get", resource]
    if name:
        command.extend([name, "--ignore-not-found=true"])
    if namespace == "*":
        command.append("--all-namespaces")
    elif namespace:
        command.extend(["-n", namespace])
    if selector:
        command.extend(["-l", selector])
    output = run_cmd_get_output([*command, "-o", "json"], timeout=timeout_seconds)
    if not output and name:
        return []
    value = json.loads(output)
    if name:
        if value["metadata"]["name"] != name or (namespace and value["metadata"].get("namespace") != namespace):
            raise ValueError(f"Mismatched object returned for {resource}/{name}")
        return [value]
    if not isinstance(value.get("items"), list):
        raise TypeError(f"Invalid list response for {resource}")
    return value["items"]


def _delete_with_uid(resource: str, namespace: str | None, collection: str, obj: dict) -> None:
    """Use kubectl's native raw DELETE body, then require absence of the same name."""
    name, uid = obj["metadata"]["name"], obj["metadata"]["uid"]
    try:
        current = _deletion_query(resource, namespace, name=name)
        if not current:
            return
        if current[0]["metadata"]["uid"] != uid:
            raise ValueError("Object UID changed before deletion")
        options = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "propagationPolicy": "Foreground",
            "preconditions": {"uid": uid, "resourceVersion": obj["metadata"]["resourceVersion"]},
        }
        with tempfile.TemporaryDirectory(prefix="motor-delete-") as directory:
            path = Path(directory) / "delete-options.json"
            path.write_text(json.dumps(options), encoding="utf-8")
            try:
                run_cmd_get_output(
                    [
                        "kubectl",
                        "delete",
                        "--raw",
                        collection + "/" + name,
                        "-f",
                        str(path),
                    ]
                )
            except RuntimeError as exc:
                # Only a known NotFound or transport timeout permits outcome confirmation.
                if "(NotFound)" not in str(exc) and "timed out" not in str(exc):
                    raise
                remaining = _deletion_query(resource, namespace, name=name)
                if not remaining:
                    return
                raise RuntimeError("DELETE outcome not confirmed; object remains") from exc
        deadline = time.monotonic() + 120
        while True:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError("Deletion incomplete after 120s")
            remaining = _deletion_query(resource, namespace, name=name, timeout_seconds=min(60, budget))
            if not remaining:
                return
            if remaining[0]["metadata"]["uid"] != uid:
                raise ValueError("Object replaced during deletion")
            if time.monotonic() >= deadline:
                raise TimeoutError("Deletion incomplete after 120s")
            time.sleep(1)
    except (RuntimeError, ValueError, KeyError, TimeoutError) as exc:
        raise RuntimeError(f"Delete {resource}/{name} in {namespace or 'cluster scope'} failed: {exc}") from exc


def cleanup_rbg(namespace: str) -> None:
    """Clean an explicitly selected RBG deployment; never fall back to legacy deletion."""
    configs = _deletion_query("configmap", namespace, name=C.MOTOR_CONFIG_CONFIGMAP_NAME)
    if not configs:
        return
    config = json.loads(configs[0].get("data", {}).get("user_config.json", "null"))
    deploy = config.get(C.MOTOR_DEPLOY_CONFIG, {}) if isinstance(config, dict) else {}
    if (
        not isinstance(deploy, dict)
        or deploy.get(C.CONFIG_JOB_ID) != namespace
        or deploy.get(C.DEPLOY_MODE_CONFIG_KEY) != C.DEPLOY_MODE_RBG
    ):
        raise ValueError("Runtime configuration does not identify an RBG in the cleanup namespace")
    delete_rbg(config)


def delete_rbg(user_config: dict) -> None:
    """Delete verified native attachments in dependency order; retain data/external resources."""
    from lib.rbg.render import _load_templates, _runtime_rbac

    settings = deployment_settings(user_config)
    namespace, name = settings["namespace"], settings["rbg_name"]
    group = f"{C.RBG_GROUP_LABEL}={name}"
    selector = f"{C.RBG_MANAGED_LABEL}={C.RBG_MANAGED_VALUE},{group}"
    custom = f"/apis/{C.RBG_API_VERSION}/namespaces/{namespace}"
    core = f"/api/v1/namespaces/{namespace}"
    managed_hpas = []
    hpas = _deletion_query(C.RBG_HPA_RESOURCE, namespace)
    groups = _deletion_query(C.RBG_RESOURCE, namespace, name=name)
    role_names = set(C.RBG_ENGINE_ROLES)
    if groups:
        role_names.update(role["name"] for role in groups[0]["spec"]["roles"])
    adapter_names = {f"{name}-{role}" for role in role_names}
    hpa_names = {f"{adapter}-hpa" for adapter in adapter_names}
    for hpa in hpas:
        hm = hpa["metadata"]
        target = hpa.get("spec", {}).get("scaleTargetRef", {})
        # Association selects candidates; strict validation below still proves ownership.
        related = (
            hm.get("labels", {}).get(C.RBG_GROUP_LABEL) == name
            or any(
                owner.get("kind") == C.RBG_KIND and owner.get("name") == name for owner in hm.get("ownerReferences", [])
            )
            or target.get("name") in adapter_names
            or hm.get("name") in hpa_names
        )
        if not related:
            continue
        if target.get("kind") == C.RBG_SCALING_ADAPTER_KIND:
            if groups:
                _verify_managed_hpa(hpa, groups[0])
            else:
                _verify_orphan_hpa(hpa, user_config)
            managed_hpas.append(hpa)
            continue
        raise RuntimeError(f"Deletion blocked: HPA/{hm['name']} ownership is ambiguous")
    runtime = _deletion_query("configmap", namespace, name=C.MOTOR_CONFIG_CONFIGMAP_NAME)
    services = _deletion_query("services", namespace, selector=selector)
    # Native headless Services inherit common labels but belong to workload controllers.
    services = [
        obj
        for obj in services
        if obj["metadata"]["name"]
        not in _native_service_names(
            name,
            [str(obj["metadata"].get("labels", {}).get(C.RBG_ROLE_LABEL, ""))],
        )
    ]
    policies = _deletion_query(C.RBG_POLICY_RESOURCE, namespace, name=name)
    _, _, _, templates = _load_templates()
    expected_rbac = _runtime_rbac(settings, templates)
    bindings = _deletion_query("clusterrolebindings.rbac.authorization.k8s.io", None)
    namespace_bindings = _deletion_query("rolebindings.rbac.authorization.k8s.io", "*")
    namespace_pods = _deletion_query("pods", namespace)
    pvcs = _deletion_query("persistentvolumeclaims", namespace)
    for pvc in pvcs:
        for owner in pvc["metadata"].get("ownerReferences", []):
            if owner.get("apiVersion") != C.RBG_API_VERSION or owner.get("kind") not in {
                C.RBG_KIND,
                "RoleInstanceSet",
                "RoleInstance",
            }:
                continue
            ancestor = owner
            # RI -> RIS -> RBG is the fixed native chain, not an arbitrary owner graph.
            for kind, resource in (
                ("RoleInstance", C.RBG_ROLE_INSTANCE_RESOURCE),
                ("RoleInstanceSet", C.RBG_ROLE_INSTANCE_SET_RESOURCE),
            ):
                if ancestor.get("kind") != kind:
                    continue
                if not ancestor.get("name") or not ancestor.get("uid"):
                    break
                parents = _deletion_query(resource, namespace, name=ancestor["name"])
                if not parents or parents[0]["metadata"].get("uid") != ancestor["uid"]:
                    break
                owners = parents[0]["metadata"].get("ownerReferences", [])
                if len(owners) != 1 or owners[0].get("apiVersion") != C.RBG_API_VERSION:
                    break
                ancestor = owners[0]
            if ancestor.get("kind") == C.RBG_KIND and ancestor.get("name") and ancestor["name"] != name:
                continue
            raise ValueError(f"PVC/{pvc['metadata']['name']} has a related or ambiguous workload GC owner")
    permissions = []
    for expected in expected_rbac:
        kind = expected["kind"]
        resource, collection = _native_rbg_resource(expected)
        scope = expected["metadata"].get("namespace")
        found = _deletion_query(resource, scope, name=expected["metadata"]["name"])
        for obj in found:
            if kind == "ServiceAccount" and any(
                pod.get("spec", {}).get("serviceAccountName") == obj["metadata"]["name"]
                and pod["metadata"].get("labels", {}).get(C.RBG_GROUP_LABEL) != name
                for pod in namespace_pods
            ):
                raise ValueError("Runtime ServiceAccount is used by an external Pod")
            if kind == "ClusterRoleBinding" and any(obj.get(k) != expected.get(k) for k in ("subjects", "roleRef")):
                raise ValueError("Runtime binding does not match the deployment's expected subjects/roleRef")
            if kind == "ClusterRole":
                permitted = {item["metadata"]["name"] for item in expected_rbac if item["kind"] == "ClusterRoleBinding"}
                reference = {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": obj["metadata"]["name"],
                }
                if any(
                    b.get("roleRef") == reference and b["metadata"]["name"] not in permitted for b in bindings
                ) or any(b.get("roleRef") == reference for b in namespace_bindings):
                    raise ValueError("Runtime ClusterRole is shared by another binding")
            permissions.append((resource, scope, collection, obj))
    plan = [("services", namespace, core + "/services", obj) for obj in services]
    plan += [(C.RBG_POLICY_RESOURCE, namespace, custom + "/coordinatedpolicies", obj) for obj in policies]
    # Bindings before roles; all runtime permissions and configuration remain until Pods exit.
    permissions.sort(key=lambda item: item[3]["kind"] != "ClusterRoleBinding")
    all_objects = groups + runtime + services + policies + managed_hpas + [item[3] for item in permissions]
    if not all_objects:
        if any(
            pod["metadata"].get("labels", {}).get(C.RBG_GROUP_LABEL) == name for pod in namespace_pods
        ) or _deletion_query(
            C.RBG_DESCENDANT_RESOURCES,
            namespace,
            selector=group,
        ):
            raise ValueError("Runtime identity absent while workloads remain; deletion cannot be confirmed")
        return
    if not runtime:
        raise ValueError("Deletion identity cannot be established: motor-config is absent")
    cm = runtime[0]
    if (
        not cm["metadata"].get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION)
        or deployment_settings(json.loads(cm["data"]["user_config.json"])) != settings
    ):
        raise ValueError("Deletion configuration identity does not match motor-config")
    if groups and (groups[0].get("kind") != C.RBG_KIND or groups[0].get("apiVersion") != C.RBG_API_VERSION):
        raise ValueError("Invalid RBG kind/API in deletion response")
    if cm.get("kind") != "ConfigMap" or cm.get("apiVersion") != "v1":
        raise ValueError("Invalid runtime ConfigMap kind/API")
    uid = groups[0]["metadata"]["uid"] if groups else None
    owner_uids = set()
    for obj in all_objects:
        metadata = obj["metadata"]
        dns_name(metadata["name"], "deletion candidate name")
        labels = metadata.get("labels", {})
        if obj["kind"] == "Service":
            role = dns_name(labels.get(C.RBG_ROLE_LABEL), "Service role label")
            native = _native_service_names(name, [role])
            if metadata["name"] in native:
                raise ValueError("Refusing to directly delete a native headless Service")
        if (
            labels.get(C.RBG_MANAGED_LABEL) != C.RBG_MANAGED_VALUE
            or labels.get(C.RBG_GROUP_LABEL) != name
            or not metadata.get("uid")
            or not metadata.get("resourceVersion")
        ):
            raise ValueError(f"Unverified deletion candidate: {obj['kind']}/{metadata['name']}")
        if obj["kind"] not in {"ClusterRole", "ClusterRoleBinding"} and metadata.get("namespace") != namespace:
            raise ValueError("Deletion candidate belongs to another namespace")
        owners = metadata.get("ownerReferences", [])
        if owners:
            if obj["kind"] not in {"Service", C.RBG_POLICY_KIND, C.RBG_HPA_KIND} or len(owners) != 1:
                raise ValueError("Unexpected owner on deletion candidate (runtime resources must not be GC-owned)")
            owner = owners[0]
            if (
                owner.get("apiVersion") != C.RBG_API_VERSION
                or owner.get("kind") != C.RBG_KIND
                or owner.get("name") != name
                or not owner.get("uid")
            ):
                raise ValueError("Foreign owner on deletion candidate")
            owner_uids.add(owner["uid"])
        elif obj["kind"] in {"Service", C.RBG_POLICY_KIND}:
            identity = cm["metadata"]["annotations"][C.RBG_SERVICE_ID_ANNOTATION]
            if metadata.get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION) != identity:
                raise ValueError("Ownerless attachments require persisted deployment identity")
    if len(owner_uids) > 1 or (uid and owner_uids - {uid}):
        raise ValueError("Attachments belong to a different RBG UID")
    for hpa in managed_hpas:
        resource, collection = _native_rbg_resource(hpa)
        _delete_with_uid(resource, namespace, collection, hpa)
    if groups:
        _delete_with_uid(C.RBG_RESOURCE, namespace, custom + "/rolebasedgroups", groups[0])
    # Foreground GC plus an explicit descendant check protect scripts/SA used by preStop.
    deadline = time.monotonic() + 120
    while True:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise RuntimeError("Deletion incomplete; preserving runtime config and permissions")
        remaining = _deletion_query(
            "pods," + C.RBG_DESCENDANT_RESOURCES,
            namespace,
            selector=group,
            timeout_seconds=min(60, budget),
        )
        if not remaining:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Deletion incomplete: {[(o.get('kind'), o.get('metadata', {}).get('name')) for o in remaining]}; preserving runtime config and permissions"
            )
        time.sleep(1)
    for resource, scope, collection, obj in [*plan, *permissions, ("configmaps", namespace, core + "/configmaps", cm)]:
        _delete_with_uid(resource, scope, collection, obj)


def _verify_rbg_owner(obj: dict, rbg: dict, *, controller: bool = False) -> None:
    owners = obj["metadata"].get("ownerReferences", [])
    expected = {
        "apiVersion": C.RBG_API_VERSION,
        "kind": C.RBG_KIND,
        "name": rbg["metadata"]["name"],
        "uid": rbg["metadata"]["uid"],
    }
    if len(owners) != 1 or any(owners[0].get(k) != v for k, v in expected.items()):
        raise ValueError(f"Foreign owner on {obj['kind']}/{obj['metadata']['name']}")
    if controller and owners[0].get("controller") is not True:
        raise ValueError("Policy trigger Service must have the current RBG as controller owner")


def _verify_managed_hpa(hpa: dict, rbg: dict) -> str:
    settings = {"namespace": rbg["metadata"]["namespace"], "rbg_name": rbg["metadata"]["name"]}
    for original in (rbg, hpa):
        checked = deepcopy(original)
        checked["metadata"].pop("deletionTimestamp", None)
        _verify_native_identity(checked, settings)
    _verify_rbg_owner(hpa, rbg)
    target = hpa["spec"]["scaleTargetRef"]
    if (
        hpa.get("apiVersion") != C.RBG_HPA_API_VERSION
        or hpa.get("kind") != C.RBG_HPA_KIND
        or (target.get("apiVersion") != C.RBG_API_VERSION or target.get("kind") != C.RBG_SCALING_ADAPTER_KIND)
    ):
        raise ValueError("Invalid native HPA target")
    matches = [
        role["name"] for role in rbg["spec"]["roles"] if target.get("name") == f"{settings['rbg_name']}-{role['name']}"
    ]
    if len(matches) != 1:
        raise ValueError("Unknown native Adapter target")
    adapters = _deletion_query(C.RBG_ADAPTER_RESOURCE, settings["namespace"], name=target["name"])
    if adapters:
        _verify_adapter(adapters[0], rbg, matches[0])
    # A deleted adapter does not prevent deleting an HPA whose own UID/owner/target is proven.
    return matches[0]


def _verify_adapter(adapter: dict, rbg: dict, role: str) -> None:
    meta = adapter["metadata"]
    invalid_identity = (
        (adapter.get("apiVersion") != C.RBG_API_VERSION)
        or (adapter.get("kind") != C.RBG_SCALING_ADAPTER_KIND)
        or (meta.get("name") != f"{rbg['metadata']['name']}-{role}")
        or (meta.get("namespace") != rbg["metadata"]["namespace"])
    )
    if invalid_identity or (
        adapter.get("spec", {}).get("scaleTargetRef") != {"name": rbg["metadata"]["name"], "role": role}
    ):
        raise ValueError("Invalid native ScalingAdapter identity/target")
    _verify_native_live(adapter)
    _verify_rbg_owner(adapter, rbg)


def _verify_orphan_hpa(hpa: dict, user_config: dict) -> None:
    """An orphan is deletable only with this deployment's persisted identity stamp."""
    settings = deployment_settings(user_config)
    checked = deepcopy(hpa)
    checked["metadata"].pop("deletionTimestamp", None)
    _verify_native_identity(checked, settings)
    runtime = _read_managed_resource("configmap", C.MOTOR_CONFIG_CONFIGMAP_NAME, settings)
    identity = runtime["metadata"].get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION)
    if (
        not identity
        or deployment_settings(json.loads(runtime["data"]["user_config.json"])) != settings
        or hpa["metadata"].get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION) != identity
    ):
        raise ValueError("Orphan HPA has no matching persisted identity")
    target = hpa["spec"]["scaleTargetRef"]
    role = hpa["metadata"].get("labels", {}).get(C.RBG_ROLE_LABEL)
    owners = hpa["metadata"].get("ownerReferences", [])
    if (
        role not in C.RBG_ENGINE_ROLES
        or len(owners) != 1
        or not owners[0].get("uid")
        or any(
            owners[0].get(k) != v
            for k, v in {
                "apiVersion": C.RBG_API_VERSION,
                "kind": C.RBG_KIND,
                "name": settings["rbg_name"],
            }.items()
        )
        or target
        != {
            "apiVersion": C.RBG_API_VERSION,
            "kind": C.RBG_SCALING_ADAPTER_KIND,
            "name": f"{settings['rbg_name']}-{role}",
        }
    ):
        raise ValueError("Orphan HPA target/owner cannot be proven")


def _set_rbg_adapter(user_config: dict, role: str, enabled: bool) -> dict:
    settings = deployment_settings(user_config)
    rbg = _read_managed_resource(C.RBG_RESOURCE, settings["rbg_name"], settings)
    matches = [(i, item) for i, item in enumerate(rbg["spec"]["roles"]) if item["name"] == role]
    if len(matches) != 1:
        raise ValueError(f"Unknown or duplicate RBG role {role}")
    index, current = matches[0]
    if enabled and (
        not isinstance(current.get("replicas"), int)
        or isinstance(current.get("replicas"), bool)
        or current["replicas"] <= 0
    ):
        raise ValueError("Enable HPA only after manually scaling this role above zero")
    if not enabled:
        for hpa in _deletion_query(C.RBG_HPA_RESOURCE, settings["namespace"]):
            if hpa.get("spec", {}).get("scaleTargetRef", {}).get("name") == f"{settings['rbg_name']}-{role}":
                raise ValueError("Delete and confirm the HPA before disabling its Adapter")
    path = f"/spec/roles/{index}"
    adapter = current.get("scalingAdapter", {})
    if adapter.get("enable", False) == enabled:
        return rbg
    patch = _rbg_patch_tests(rbg) + [{"op": "test", "path": path + "/name", "value": role}]
    if "scalingAdapter" in current:
        patch.append({"op": "add", "path": path + "/scalingAdapter/enable", "value": enabled})
    else:
        patch.append({"op": "add", "path": path + "/scalingAdapter", "value": {"enable": enabled}})
    return _patch_rbg_object(C.RBG_RESOURCE, rbg, patch)


def _enable_rbg_adapter(user_config: dict, rbg: dict, role: str, *, timeout_seconds: int = 120) -> dict:
    """Bind only after RIS publishes its selector; repair unused adapters stuck with an empty selector."""
    namespace, name = rbg["metadata"]["namespace"], rbg["metadata"]["name"]
    target = f"{name}-{role}"
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"RoleInstanceSet {namespace}/{target}: waiting for status.labelSelector")
        output = run_cmd_get_output(
            [
                "kubectl",
                "get",
                C.RBG_ROLE_INSTANCE_SET_RESOURCE,
                target,
                "-n",
                namespace,
                "--ignore-not-found=true",
                "-o",
                "json",
            ],
            timeout=max(0.001, min(60, remaining)),
        )
        if output:
            instance_set = json.loads(output)
            metadata = instance_set["metadata"]
            if (
                instance_set.get("apiVersion") != C.RBG_API_VERSION
                or instance_set.get("kind") != "RoleInstanceSet"
                or metadata.get("name") != target
                or metadata.get("namespace") != namespace
                or metadata.get("deletionTimestamp")
            ):
                raise ValueError(f"Invalid or deleting RoleInstanceSet {namespace}/{target}")
            _verify_rbg_owner(instance_set, rbg, controller=True)
            if instance_set.get("status", {}).get("labelSelector"):
                break
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    live = _read_managed_resource(C.RBG_RESOURCE, name, deployment_settings(user_config))
    if live["metadata"]["uid"] != rbg["metadata"]["uid"]:
        raise ValueError("RBG replaced before enabling its Adapter")
    adapters = _deletion_query(C.RBG_ADAPTER_RESOURCE, namespace, name=target)
    if adapters:
        adapter = adapters[0]
        _verify_adapter(adapter, live, role)
        status = adapter.get("status", {})
        if status.get("phase") == "Bound" and not status.get("selector"):
            # Disabling checks all HPA references first; never interrupt an active HPA to repair its target.
            stopped = _set_rbg_adapter(user_config, role, False)
            _wait_rbg_adapter(stopped, role, absent=True)
    return _set_rbg_adapter(user_config, role, True)


def _wait_rbg_adapter(rbg: dict, role: str, *, absent: bool = False, timeout_seconds: int = 120) -> None:
    """Wait for native creation/binding or removal; never retry API failures as absence."""
    deadline = time.monotonic() + timeout_seconds
    namespace, name = rbg["metadata"]["namespace"], rbg["metadata"]["name"]
    adapter_name = f"{name}-{role}"
    last = "not created"
    observed_uid = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Adapter {namespace}/{adapter_name}: {last}")
        args = [
            "kubectl",
            "get",
            C.RBG_ADAPTER_RESOURCE,
            adapter_name,
            "-n",
            namespace,
            "--ignore-not-found=true",
            "-o",
            "json",
        ]
        output = run_cmd_get_output(args, timeout=max(0.001, min(60, remaining)))
        if not output:
            if absent:
                return
            last = "not created"
        else:
            adapter = json.loads(output)
            if observed_uid and adapter["metadata"].get("uid") != observed_uid:
                raise ValueError("Adapter replaced during dependency waiting")
            observed_uid = adapter["metadata"].get("uid")
            # A terminating object is valid only while waiting for its disappearance.
            checked = deepcopy(adapter)
            if absent:
                checked["metadata"].pop("deletionTimestamp", None)
            _verify_adapter(checked, rbg, role)
            if not absent:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("Adapter binding deadline exhausted")
                latest = json.loads(
                    run_cmd_get_output(
                        [
                            "kubectl",
                            "get",
                            C.RBG_RESOURCE,
                            name,
                            "-n",
                            namespace,
                            "-o",
                            "json",
                        ],
                        timeout=max(0.001, min(60, left)),
                    )
                )
                if latest["metadata"]["uid"] != rbg["metadata"]["uid"]:
                    raise ValueError("RBG replaced while waiting for its Adapter")
                roles = [item for item in latest["spec"]["roles"] if item["name"] == role]
                if len(roles) != 1 or not roles[0].get("scalingAdapter", {}).get("enable"):
                    raise ValueError("Adapter enable intent changed while waiting")
                target = roles[0].get("replicas")
                status = adapter.get("status", {})
                last = f"phase={status.get('phase')} replicas={adapter['spec'].get('replicas')}"
                positive_target = isinstance(target, int) and not isinstance(target, bool) and target > 0
                if (
                    positive_target
                    and status.get("phase") == "Bound"
                    and adapter["spec"].get("replicas") == status.get("replicas") == target
                    and status.get("selector")
                ):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("Adapter scale read deadline exhausted")
                    scale = json.loads(
                        run_cmd_get_output(
                            [
                                "kubectl",
                                "get",
                                "--raw",
                                f"/apis/{C.RBG_API_VERSION}/namespaces/{namespace}/rolebasedgroupscalingadapters/{adapter_name}/scale",
                            ],
                            timeout=max(0.001, min(60, left)),
                        )
                    )
                    invalid_identity = (
                        (scale.get("kind") != "Scale")
                        or (scale.get("metadata", {}).get("name") != adapter_name)
                        or (scale["metadata"].get("namespace") != namespace)
                        or (scale.get("spec", {}).get("replicas") != target)
                    )
                    if (
                        invalid_identity
                        or (scale.get("status", {}).get("replicas") != target)
                        or (not scale["status"].get("selector"))
                    ):
                        raise ValueError("Adapter scale is not initialized consistently")
                    return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Adapter {namespace}/{adapter_name}: {last}")
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def _request_rbg_coordination(user_config: dict, service_name: str) -> None:
    settings = deployment_settings(user_config)
    rbg = _read_managed_resource(C.RBG_RESOURCE, settings["rbg_name"], settings)
    service = _read_managed_resource("service", service_name, settings)
    if service["metadata"].get("labels", {}).get(C.RBG_ROLE_LABEL) != C.CONTROLLER:
        raise ValueError("Coordination must use the managed Controller Service")
    native = _native_service_names(settings["rbg_name"], (role["name"] for role in rbg["spec"]["roles"]))
    if service_name in native:
        raise ValueError("Do not use a native headless Service for coordination")
    _verify_rbg_owner(service, rbg, controller=True)
    patch = _rbg_patch_tests(service)
    annotation = C.RBG_COORDINATION_ANNOTATION
    if "annotations" not in service["metadata"]:
        patch.append({"op": "add", "path": "/metadata/annotations", "value": {annotation: uuid.uuid4().hex}})
    else:
        patch.append(
            {
                "op": "add",
                "path": "/metadata/annotations/motor-deployer~1coordination-request",
                "value": uuid.uuid4().hex,
            }
        )
    _patch_rbg_object("service", service, patch)
    logger.info(
        "Policy persisted; reconciliation requested through Service/%s; consumption is not acknowledged", service_name
    )


def _read_rbg_kv_facts(user_config: dict) -> dict:
    from lib.generator.kv_cache_store import (
        resolve_external_master,
        split_plain_master_address,
    )

    config = user_config.get(C.KV_CACHE_STORE_CONFIG, {})
    explicit = resolve_external_master(config)
    if explicit:
        host, _ = split_plain_master_address(explicit)
        return {"external_service_addresses": {"kv_store": host} if host else {}}
    target = config.get(C.TARGET_JOB_ID)
    if not target or target == user_config[C.MOTOR_DEPLOY_CONFIG]["job_id"]:
        return {}
    dns_name(target, C.TARGET_JOB_ID)
    services = _deletion_query("services", target)
    requested_name = config.get("service_name")
    candidates = []
    for service in services:
        if (
            service.get("kind") != "Service"
            or service.get("apiVersion") != "v1"
            or service["metadata"].get("namespace") != target
            or not service["metadata"].get("uid")
        ):
            raise ValueError("Invalid KV Service list response")
        selector = service.get("spec", {}).get("selector", {})
        if requested_name:
            match = service["metadata"]["name"] == requested_name
        else:
            match = selector.get(C.RBG_ROLE_LABEL) == C.ROLE_KV_STORE or selector.get("app") == C.KV_STORE_APP_LABEL
        if match and service.get("spec", {}).get("clusterIP") != "None":
            candidates.append(service)
    if len(candidates) > 1:
        raise ValueError("KV target Service is ambiguous")
    if not candidates:
        return {"kv_store_reusable": False}
    service = candidates[0]
    selector = service["spec"].get("selector")
    if not isinstance(selector, dict) or not selector:
        raise ValueError("KV reuse requires a real nonempty Service selector")
    expression = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    pods = _deletion_query("pods", target, selector=expression)
    for pod in pods:
        if (
            pod.get("kind") != "Pod"
            or pod.get("apiVersion") != "v1"
            or pod["metadata"].get("namespace") != target
            or not pod["metadata"].get("uid")
        ):
            raise ValueError("Invalid KV Pod list response")
    reusable = not service["metadata"].get("deletionTimestamp") and any(
        pod.get("kind") == "Pod"
        and pod["metadata"].get("namespace") == target
        and not pod["metadata"].get("deletionTimestamp")
        and pod.get("status", {}).get("phase") == "Running"
        and all(pod["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())
        for pod in pods
    )
    return {
        "kv_store_reusable": bool(reusable),
        "external_service_addresses": {
            "kv_store": f"{service['metadata']['name']}.{target}.svc.cluster.local",
        }
        if reusable
        else {},
    }


def _read_rbg_facts(user_config: dict) -> dict:
    from lib.generator.k8s_utils import get_accelerator_type_from_cluster

    settings = deployment_settings(user_config)
    facts = _read_rbg_kv_facts(user_config)
    groups = _deletion_query(C.RBG_RESOURCE, settings["namespace"], name=settings["rbg_name"])
    runtime = _deletion_query("configmap", settings["namespace"], name=C.MOTOR_CONFIG_CONFIGMAP_NAME)
    for obj in groups + runtime:
        _verify_native_identity(obj, settings)
    if groups:
        facts["rbg"] = groups[0]
    if runtime:
        cm = runtime[0]
        if not cm["metadata"].get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION) or (
            deployment_settings(json.loads(cm["data"]["user_config.json"])) != settings
        ):
            raise ValueError("Conflicting persisted runtime identity")
        facts["runtime_configmap"] = cm
    if groups and not runtime:
        raise ValueError("Existing RBG requires its persisted runtime identity")
    selector = f"{C.RBG_MANAGED_LABEL}={C.RBG_MANAGED_VALUE},{C.RBG_GROUP_LABEL}={settings['rbg_name']}"
    services = _deletion_query("services", settings["namespace"], selector=selector)
    facts["services"] = {obj["metadata"]["name"]: obj for obj in services}
    hardware = user_config[C.MOTOR_DEPLOY_CONFIG].get("hardware_type", "800I_A2")
    if hardware in {"800I_A2", "800T_A2", "800I_A3", "800T_A3"}:
        facts["accelerator_type"] = get_accelerator_type_from_cluster(hardware)
    return facts


def _native_rbg_resource(obj: dict) -> tuple[str, str]:
    """Fixed collections used by this lifecycle, never infer arbitrary API paths."""
    meta = obj["metadata"]
    namespace = meta.get("namespace")
    custom = f"/apis/{C.RBG_API_VERSION}/namespaces/{namespace}"
    core = f"/api/v1/namespaces/{namespace}"
    rbac = "/apis/rbac.authorization.k8s.io/v1"
    resources = {
        C.RBG_KIND: (C.RBG_RESOURCE, custom + "/rolebasedgroups", C.RBG_API_VERSION),
        C.RBG_POLICY_KIND: (
            C.RBG_POLICY_RESOURCE,
            custom + "/coordinatedpolicies",
            C.RBG_API_VERSION,
        ),
        C.RBG_HPA_KIND: (
            C.RBG_HPA_RESOURCE,
            f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers",
            C.RBG_HPA_API_VERSION,
        ),
        "ConfigMap": ("configmaps", core + "/configmaps", "v1"),
        "Service": ("services", core + "/services", "v1"),
        "PersistentVolumeClaim": ("persistentvolumeclaims", core + "/persistentvolumeclaims", "v1"),
        "ServiceAccount": ("serviceaccounts", core + "/serviceaccounts", "v1"),
        "ClusterRole": (
            "clusterroles.rbac.authorization.k8s.io",
            rbac + "/clusterroles",
            "rbac.authorization.k8s.io/v1",
        ),
        "ClusterRoleBinding": (
            "clusterrolebindings.rbac.authorization.k8s.io",
            rbac + "/clusterrolebindings",
            "rbac.authorization.k8s.io/v1",
        ),
    }
    resource, collection, api = resources[obj["kind"]]
    if obj.get("apiVersion") != api:
        raise ValueError(f"Invalid API for {obj['kind']}")
    return resource, collection


def _verify_native_identity(obj: dict, settings: dict) -> None:
    if obj.get("kind") == "ConfigMap" and obj.get("metadata", {}).get("name") == C.MOTOR_CONFIG_CONFIGMAP_NAME:
        existing = obj["metadata"].get("labels", {}).get(C.RBG_GROUP_LABEL)
        if existing and existing != settings["rbg_name"]:
            raise ValueError(
                f"Namespace {settings['namespace']} already contains RBG {existing!r}; requested {settings['rbg_name']!r}. "
                "The fixed motor-config currently supports one RBG per namespace."
            )
    _native_rbg_resource(obj)
    _verify_native_metadata(obj, settings)
    _verify_native_live(obj)


def _verify_native_metadata(obj: dict, settings: dict) -> None:
    meta = obj["metadata"]
    dns_name(meta["name"], "managed resource name")
    labels = meta.get("labels", {})
    cluster = obj["kind"] in {"ClusterRole", "ClusterRoleBinding"}
    invalid_namespace = bool(meta.get("namespace")) if cluster else meta.get("namespace") != settings["namespace"]
    if (
        labels.get(C.RBG_MANAGED_LABEL) != C.RBG_MANAGED_VALUE
        or labels.get(C.RBG_GROUP_LABEL) != settings["rbg_name"]
        or invalid_namespace
    ):
        raise ValueError("Native resource has conflicting deployment identity")


def _verify_native_live(obj: dict) -> None:
    meta = obj["metadata"]
    if not meta.get("uid") or not meta.get("resourceVersion") or meta.get("deletionTimestamp"):
        raise ValueError("Resource is deleting or lacks API identity/version")


def _submit_rbg_object(desired: dict, settings: dict, identity: str, *, rbg: dict | None = None) -> dict:
    """Create or guarded-field-patch a concrete generated lifecycle object."""
    obj = deepcopy(desired)
    resource, _ = _native_rbg_resource(obj)
    _verify_native_metadata(obj, settings)
    meta = obj["metadata"]
    attachments = {"Service", C.RBG_POLICY_KIND, C.RBG_HPA_KIND}
    if obj["kind"] in attachments and rbg:
        owner = {
            "apiVersion": C.RBG_API_VERSION,
            "kind": C.RBG_KIND,
            "name": settings["rbg_name"],
            "uid": rbg["metadata"]["uid"],
        }
        if (
            obj["kind"] == "Service"
            and meta.get("labels", {}).get(C.RBG_ROLE_LABEL) == C.CONTROLLER
            and any(port.get("port") == 1026 for port in obj["spec"].get("ports", []))
        ):
            owner["controller"] = True
        meta["ownerReferences"] = [owner]
    online = _deletion_query(resource, meta.get("namespace"), name=meta["name"])
    if obj["kind"] == C.RBG_KIND and (
        (online[0]["metadata"]["uid"] if online else None) != (rbg["metadata"]["uid"] if rbg else None)
    ):
        raise ValueError("RBG existence/UID changed before submission")
    if not online:
        for key in ("uid", "resourceVersion", "generation", "creationTimestamp", "managedFields"):
            meta.pop(key, None)
        obj.pop("status", None)
        uncertain = False
        with tempfile.TemporaryDirectory(prefix="motor-rbg-create-") as directory:
            path = Path(directory) / "resource.json"
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            try:
                result = json.loads(
                    run_cmd_get_output(
                        [
                            "kubectl",
                            "create",
                            "-f",
                            str(path),
                            "-o",
                            "json",
                        ]
                    )
                )
            except RuntimeError as exc:
                if not any(w in str(exc).lower() for w in ("timeout", "timed out", "deadline exceeded")):
                    raise
                uncertain = True
                try:
                    confirmed = _deletion_query(resource, meta.get("namespace"), name=meta["name"])
                except (RuntimeError, ValueError) as query_error:
                    raise RuntimeError(f"Create outcome unconfirmed for {obj['kind']}/{meta['name']}") from query_error
                if not confirmed:
                    raise RuntimeError(f"Create outcome unconfirmed for {obj['kind']}/{meta['name']}") from exc
                result = confirmed[0]
        _verify_native_identity(result, settings)
        if (
            any(result.get(key) != obj.get(key) for key in ("apiVersion", "kind"))
            or result["metadata"]["name"] != meta["name"]
            or result["metadata"].get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION)
            != meta.get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION)
        ):
            raise RuntimeError(f"Create returned a conflicting identity for {obj['kind']}/{meta['name']}")
        if uncertain and not _rbg_contains(result, obj):
            raise RuntimeError(f"Create outcome unconfirmed for {obj['kind']}/{meta['name']}")
        logger.info("Created %s/%s in %s", obj["kind"], meta["name"], meta.get("namespace", "cluster"))
        return result
    live = online[0]
    _verify_native_identity(live, settings)
    if live["metadata"].get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION) != identity:
        # Older already-bound attachments are also proven by their real RBG owner.
        if obj["kind"] in attachments and rbg:
            _verify_rbg_owner(live, rbg)
        elif obj["kind"] in {"ServiceAccount", "ClusterRole", "ClusterRoleBinding"}:
            declared = {k: obj[k] for k in ("rules", "subjects", "roleRef") if k in obj}
            if not _rbg_contains(live, declared):
                raise ValueError("Runtime permission changes require matching persisted identity")
        elif obj["kind"] != C.RBG_KIND:
            raise ValueError("Existing object has no matching persisted deployment identity")
    if obj["kind"] in attachments and live["metadata"].get("ownerReferences"):
        if not rbg:
            raise ValueError("Attachment owned by an RBG but no live group was supplied")
        _verify_rbg_owner(live, rbg)
    elif obj["kind"] not in attachments and live["metadata"].get("ownerReferences"):
        raise ValueError("Runtime/cluster resources must not be GC-owned by a workload")
    if obj["kind"] == "PersistentVolumeClaim":
        if not _rbg_contains(live.get("spec"), obj.get("spec")):
            raise ValueError("Existing PVC differs; lifecycle does not mutate data PVCs")
        return live
    if obj["kind"] == "Service" and live["metadata"].get("ownerReferences"):
        service_update = deepcopy(obj)
        service_update["metadata"].pop("ownerReferences", None)
        _patch_rbg_service(settings, service_update)
        live = _read_managed_resource("service", meta["name"], settings)
    elif obj["kind"] == "Service" and not _rbg_contains(live.get("spec"), obj["spec"]):
        raise ValueError("Pending Service differs from this command; establish its ownership before updating")
    patch = _rbg_patch_tests(live)
    # Service spec changes go through its dedicated allocation/transition checks.
    for key in (
        "spec",
        "data",
        "rules",
        "subjects",
        "roleRef",
        "aggregationRule",
        "automountServiceAccountToken",
        "imagePullSecrets",
    ):
        if key in obj and obj["kind"] != "Service":
            value = deepcopy(obj[key])
            if obj["kind"] == C.RBG_KIND and key == "spec":
                from lib.rbg.render import _check_existing_roles

                # Validate topology against the fresh object used by this guarded Patch.
                _check_existing_roles(value["roles"], live)
                old = {role["name"]: role for role in live["spec"]["roles"]}
                for role in value["roles"]:
                    # ScalingAdapter owns counts only while it remains enabled.
                    previous = old.get(role["name"], {})
                    if role.get("scalingAdapter", {}).get("enable") and previous.get("replicas", 0) > 0:
                        role["replicas"] = previous["replicas"]
                # Compare the complete desired spec after API defaulting. Subset comparison
                # misses removed template fields; raw equality repeatedly removes defaults.
                preview = _patch_rbg_object(
                    resource,
                    live,
                    [*_rbg_patch_tests(live), {"op": "add", "path": "/spec", "value": value}],
                    dry_run=True,
                )
                value = preview["spec"]
            strict_spec = key == "spec" and obj["kind"] in {
                C.RBG_KIND,
                C.RBG_POLICY_KIND,
                C.RBG_HPA_KIND,
            }
            if not _rbg_contains(live.get(key), value) or (strict_spec and live.get(key) != value):
                patch.append({"op": "add", "path": "/" + key, "value": value})
    for section in ("labels", "annotations"):
        patch.extend(_service_field_patch(live["metadata"], {section: meta.get(section, {})}, "/metadata"))
    if "ownerReferences" in meta and meta["ownerReferences"] != live["metadata"].get("ownerReferences"):
        patch.append({"op": "add", "path": "/metadata/ownerReferences", "value": meta["ownerReferences"]})
    if len(patch) == 2:
        return live
    return _patch_rbg_object(resource, live, patch)


def _bind_rbg_attachment(obj: dict, rbg: dict, identity: str, settings: dict) -> dict:
    desired = deepcopy(obj)
    desired["metadata"].setdefault("annotations", {})[C.RBG_SERVICE_ID_ANNOTATION] = identity
    return _submit_rbg_object(desired, settings, identity, rbg=rbg)


def _prepare_rbg_plan(resources: dict, scaling: dict, live_rbg: dict | None = None) -> dict:
    """Copy and validate rendered deployment and scaling resources into a submission plan."""
    plan = deepcopy(resources)
    settings = deployment_settings(plan["user_config"])
    runtime = [
        obj
        for obj in plan["prerequisites"]
        if obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == C.MOTOR_CONFIG_CONFIGMAP_NAME
    ]
    if len(runtime) != 1:
        raise ValueError("Rendered resources must contain exactly one motor-config ConfigMap")
    identity = runtime[0]["metadata"]["annotations"][C.RBG_SERVICE_ID_ANNOTATION]
    if not identity:
        raise ValueError("Missing generated deployment identity")
    if set(scaling) != {"policy", "hpas"}:
        raise ValueError("Scaling resources must contain exactly the policy and hpas fields")
    if live_rbg is None and scaling["policy"] is None:
        raise ValueError("Initial deployment requires a same-name Policy")
    if scaling["hpas"] is not None and not isinstance(scaling["hpas"], dict):
        raise TypeError("Scaling hpas must be a role-to-HPA dictionary or None")
    plan["policy"], plan["hpas"] = deepcopy(scaling["policy"]), deepcopy(scaling["hpas"])
    roles = {role["name"]: role for role in plan["rbg"]["spec"]["roles"]}
    if plan["policy"] and (
        plan["policy"]["metadata"]["name"] != settings["rbg_name"] or plan["policy"].get("kind") != C.RBG_POLICY_KIND
    ):
        raise ValueError("Policy must match RBG name and native kind")
    for role, hpa in (plan["hpas"] or {}).items():
        if (
            role not in roles
            or not isinstance(roles[role].get("replicas"), int)
            or isinstance(roles[role].get("replicas"), bool)
            or roles[role]["replicas"] <= 0
        ):
            raise ValueError("HPA role must exist and have a positive online instance count")
        if hpa.get("kind") != C.RBG_HPA_KIND or hpa["spec"]["scaleTargetRef"] != {
            "apiVersion": C.RBG_API_VERSION,
            "kind": C.RBG_SCALING_ADAPTER_KIND,
            "name": f"{settings['rbg_name']}-{role}",
        }:
            raise ValueError("HPA must target the native ScalingAdapter for the same role")
        hpa["metadata"].setdefault("labels", {})[C.RBG_ROLE_LABEL] = role
    if plan["hpas"] is not None:
        old_roles = {role["name"]: role for role in (live_rbg or {}).get("spec", {}).get("roles", [])}
        for name, role in roles.items():
            role.setdefault("scalingAdapter", {})["enable"] = name in plan["hpas"] and old_roles.get(name, {}).get(
                "scalingAdapter", {}
            ).get("enable", False)
    if live_rbg:
        old = {role["name"]: role for role in live_rbg["spec"]["roles"]}
        deploy = plan["user_config"][C.MOTOR_DEPLOY_CONFIG]
        for name, role in roles.items():
            if role.get("scalingAdapter", {}).get("enable") and name in old and old[name]["replicas"] > 0:
                role["replicas"] = old[name]["replicas"]
                field = C.RBG_INSTANCE_KEYS_BY_ROLE.get(name)
                if field in deploy:
                    deploy[field] = role["replicas"]
        runtime[0]["data"]["user_config.json"] = json.dumps(plan["user_config"], ensure_ascii=False, indent=2) + "\n"
    objects = [*plan["prerequisites"], *plan["services"], plan["rbg"]]
    objects += ([plan["policy"]] if plan["policy"] is not None else []) + list((plan["hpas"] or {}).values())
    seen = set()
    for obj in objects:
        meta = obj["metadata"]
        for key, value in ((C.RBG_MANAGED_LABEL, C.RBG_MANAGED_VALUE), (C.RBG_GROUP_LABEL, settings["rbg_name"])):
            if meta.setdefault("labels", {}).get(key, value) != value:
                raise ValueError("Generated resource conflicts with the deployment's management labels")
            meta["labels"][key] = value
        meta.setdefault("annotations", {})[C.RBG_SERVICE_ID_ANNOTATION] = identity
        _native_rbg_resource(obj)
        _verify_native_metadata(obj, settings)
        target = (obj["kind"], meta.get("namespace"), meta["name"])
        if target in seen:
            raise ValueError("Duplicate generated resource")
        seen.add(target)
    native_names = _native_service_names(settings["rbg_name"], roles)
    if any(s["metadata"]["name"] in native_names for s in plan["services"]):
        raise ValueError("Business Service conflicts with a native headless name")
    plan["identity"] = identity
    plan["expected_rbg_uid"] = live_rbg["metadata"]["uid"] if live_rbg else None
    if plan["policy"] is not None:
        controllers = [
            s
            for s in plan["services"]
            if s["metadata"].get("labels", {}).get(C.RBG_ROLE_LABEL) == C.CONTROLLER
            and any(p.get("port") == 1026 for p in s["spec"].get("ports", []))
        ]
        if len(controllers) != 1:
            raise ValueError("Policy reconciliation requires exactly one Controller management Service")
    return plan


def _check_restart_schema(properties: dict, restart: dict, context: str) -> None:
    fields = properties.get("restartPolicyConfig", {}).get("properties", {})
    for field, value in restart.items():
        schema = fields.get(field)
        if not isinstance(schema, dict) or ("enum" in schema and value not in schema["enum"]):
            raise ValueError(f"CRD {context} does not support restartPolicyConfig.{field}={value!r}; upgrade RBG")


def _check_rbg_schema(version: dict, plural: str, roles: list[dict]) -> None:
    """Check only native patterns/restart fields emitted by this deployment."""
    if plural not in {"rolebasedgroups", "roleinstancesets", "roleinstances"}:
        return
    properties = (
        version.get("schema", {}).get("openAPIV3Schema", {}).get("properties", {}).get("spec", {}).get("properties", {})
    )
    if plural == "rolebasedgroups":
        properties = properties.get("roles", {}).get("items", {}).get("properties", {})
    elif plural == "roleinstancesets":
        template = properties.get("roleInstanceTemplate", {})
        if template.get("x-kubernetes-preserve-unknown-fields") is True:
            return
        properties = template.get("properties", {}).get("spec", {}).get("properties", {})
    for role in roles:
        for pattern in ("standalonePattern", "leaderWorkerPattern", "customComponentsPattern"):
            if pattern not in role:
                continue
            fields = properties
            if plural == "rolebasedgroups":
                if pattern not in fields:
                    raise ValueError(f"CRD {plural} does not support generated {pattern}; upgrade RBG")
                fields = fields[pattern].get("properties", {})
            restart = role[pattern].get("restartPolicyConfig")
            if restart:
                _check_restart_schema(fields, restart, f"{plural}/{pattern}")


def _preflight_rbg_plan(plan: dict, settings: dict) -> None:
    namespace = settings["namespace"]
    namespaces = _deletion_query("namespace", None, name=namespace)
    if not namespaces or namespaces[0].get("kind") != "Namespace" or namespaces[0]["metadata"].get("deletionTimestamp"):
        raise ValueError("Deployment namespace must exist and not be terminating")
    for plural in (
        "rolebasedgroups",
        "coordinatedpolicies",
        "roleinstancesets",
        "roleinstances",
        "rolebasedgroupscalingadapters",
    ):
        crds = _deletion_query(
            "customresourcedefinitions.apiextensions.k8s.io", None, name=f"{plural}.{C.RBG_API_GROUP}"
        )
        if (
            not crds
            or crds[0].get("spec", {}).get("group") != C.RBG_API_GROUP
            or not any(
                version.get("name") == C.RBG_API_VERSION_NAME and version.get("served") is True
                for version in crds[0]["spec"].get("versions", [])
            )
        ):
            raise ValueError(f"Missing served v1alpha2 CRD {plural}")
        version = next(v for v in crds[0]["spec"]["versions"] if v.get("name") == C.RBG_API_VERSION_NAME)
        _check_rbg_schema(version, plural, plan["rbg"]["spec"]["roles"])
    permissions = set()
    objects = [*plan["prerequisites"], *plan["services"], plan["rbg"]]
    objects += ([plan["policy"]] if plan["policy"] is not None else []) + list((plan["hpas"] or {}).values())
    for obj in objects:
        resource, _ = _native_rbg_resource(obj)
        ns = obj["metadata"].get("namespace")
        for verb in ("get", "create") if obj["kind"] == "PersistentVolumeClaim" else ("get", "create", "patch"):
            permissions.add((verb, resource, ns))
        found = _deletion_query(resource, ns, name=obj["metadata"]["name"])
        if found:
            live = found[0]
            _verify_native_identity(live, settings)
            persisted = live["metadata"].get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION)
            if live["kind"] in {"Service", C.RBG_POLICY_KIND, C.RBG_HPA_KIND}:
                if live["metadata"].get("ownerReferences"):
                    groups = _deletion_query(C.RBG_RESOURCE, namespace, name=settings["rbg_name"])
                    if not groups:
                        raise ValueError("Attachment owner is not a live deployment")
                    _verify_rbg_owner(live, groups[0])
                elif persisted != plan["identity"]:
                    raise ValueError("Unbound existing attachment has no matching deployment identity")
            elif live["kind"] == "ConfigMap" and persisted != plan["identity"]:
                raise ValueError("Runtime ConfigMap identity changed")
            if obj["kind"] == "Service" and live["metadata"].get("ownerReferences"):
                _patch_rbg_service(settings, obj, validate_only=True)
            if obj["kind"] == "PersistentVolumeClaim" and not _rbg_contains(live.get("spec"), obj["spec"]):
                raise ValueError("Existing data PVC does not match generated requirements")
            if obj["kind"] in {"ServiceAccount", "ClusterRole", "ClusterRoleBinding"}:
                _preflight_runtime_permission(obj, live, settings, plan["prerequisites"])
    for resource in (
        "services",
        C.RBG_HPA_RESOURCE,
        C.RBG_ADAPTER_RESOURCE,
    ):
        permissions.add(("list", resource, namespace))
    if plan["hpas"] is not None:
        # Enabling and disabling both wait for the native Adapter by name.
        permissions.add(("get", C.RBG_ADAPTER_RESOURCE, namespace))
    for resource in ("services", C.RBG_HPA_RESOURCE):
        permissions.add(("delete", resource, namespace))
    for verb, resource, ns in sorted(permissions, key=str):
        args = [
            "kubectl",
            "auth",
            "can-i",
            verb,
            resource,
            "--quiet",
        ]
        if ns:
            args.extend(["-n", ns])
        run_cmd_get_output(args)
    if plan["hpas"]:
        run_cmd_get_output(
            [
                "kubectl",
                "auth",
                "can-i",
                "get",
                C.RBG_ADAPTER_RESOURCE,
                "--subresource=scale",
                "-n",
                namespace,
                "--quiet",
            ]
        )
    services = _deletion_query(
        "services",
        namespace,
        selector=f"{C.RBG_MANAGED_LABEL}={C.RBG_MANAGED_VALUE},{C.RBG_GROUP_LABEL}={settings['rbg_name']}",
    )
    groups = _deletion_query(C.RBG_RESOURCE, namespace, name=settings["rbg_name"])
    native_names = _native_service_names(settings["rbg_name"], (role["name"] for role in plan["rbg"]["spec"]["roles"]))
    for service in services:
        if service["metadata"]["name"] in native_names:
            continue
        _verify_native_identity(service, settings)
        if service["metadata"].get("ownerReferences"):
            if not groups:
                raise ValueError("Existing Service belongs to an absent RBG")
            _verify_rbg_owner(service, groups[0])
        elif service["metadata"].get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION) != plan["identity"]:
            raise ValueError("Unbound Service has no matching persisted identity")
    for hpa in _deletion_query(C.RBG_HPA_RESOURCE, namespace):
        target = hpa.get("spec", {}).get("scaleTargetRef", {})
        related = hpa["metadata"].get("labels", {}).get(C.RBG_GROUP_LABEL) == settings["rbg_name"]
        related |= target.get("kind") == C.RBG_SCALING_ADAPTER_KIND and any(
            target.get("name") == f"{settings['rbg_name']}-{role['name']}" for role in plan["rbg"]["spec"]["roles"]
        )
        if related:
            if not groups:
                raise ValueError("Related HPA exists without its RBG")
            _verify_managed_hpa(hpa, groups[0])
    check_rbg_nodeports(plan["services"], current={s["metadata"]["name"]: s for s in services})


def _preflight_runtime_permission(desired: dict, live: dict, settings: dict, prerequisites: list[dict]) -> None:
    """Do not mutate shared runtime permissions during an ordinary resource update."""
    namespace, name = settings["namespace"], settings["rbg_name"]
    if live["metadata"].get("ownerReferences"):
        raise ValueError("Runtime permissions must not have workload GC owners")
    if live["kind"] == "ClusterRoleBinding" and any(live.get(k) != desired.get(k) for k in ("subjects", "roleRef")):
        raise ValueError("Runtime binding subjects/roleRef differ from the generated deployment identity")
    if live["kind"] == "ServiceAccount" and any(
        pod.get("spec", {}).get("serviceAccountName") == live["metadata"]["name"]
        and pod["metadata"].get("labels", {}).get(C.RBG_GROUP_LABEL) != name
        for pod in _deletion_query("pods", namespace)
    ):
        raise ValueError("Runtime ServiceAccount is shared by another deployment")
    if live["kind"] == "ClusterRole":
        reference = {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": live["metadata"]["name"]}
        bindings = _deletion_query("clusterrolebindings.rbac.authorization.k8s.io", None)
        bindings += _deletion_query("rolebindings.rbac.authorization.k8s.io", "*")
        expected = {obj["metadata"]["name"]: obj for obj in prerequisites if obj["kind"] == "ClusterRoleBinding"}
        for binding in bindings:
            if binding.get("roleRef") != reference:
                continue
            permitted = expected.get(binding["metadata"]["name"])
            if (
                binding["metadata"].get("namespace")
                or permitted is None
                or binding.get("subjects") != permitted.get("subjects")
                or binding.get("roleRef") != permitted.get("roleRef")
            ):
                raise ValueError("Runtime ClusterRole is shared by another binding")


def _execute_rbg_plan(user_config: dict, plan: dict, *, dry_run: bool = False) -> None:
    """Validate and submit the resource plan, or write local previews when dry_run is enabled."""
    import yaml

    settings = deployment_settings(user_config)
    namespace, name, identity = settings["namespace"], settings["rbg_name"], plan["identity"]
    _preflight_rbg_plan(plan, settings)
    output = Path("output_yamls") / namespace / name
    output.mkdir(parents=True, exist_ok=True)
    for filename, objects in {
        "prerequisites": plan["prerequisites"],
        "services": plan["services"],
        "rbg": [plan["rbg"]],
        "policy": [plan["policy"]] if plan["policy"] is not None else [],
        "hpas": list((plan["hpas"] or {}).values()),
    }.items():
        (output / f"{filename}.yaml").write_text(
            yaml.safe_dump_all(objects, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    if dry_run:
        logger.info("RBG resources previewed at %s; no cluster writes performed", output)
        return
    groups = _deletion_query(C.RBG_RESOURCE, namespace, name=name)
    rbg = groups[0] if groups else None
    if (rbg["metadata"]["uid"] if rbg else None) != plan["expected_rbg_uid"]:
        raise ValueError("RBG existence/UID changed after resource generation; rerun with fresh facts")
    desired_hpas = plan["hpas"]
    try:
        if rbg and desired_hpas is not None:
            _verify_native_identity(rbg, settings)
            for hpa in _deletion_query(C.RBG_HPA_RESOURCE, namespace):
                if hpa["metadata"].get("labels", {}).get(C.RBG_GROUP_LABEL) != name:
                    continue
                role = _verify_managed_hpa(hpa, rbg)
                if role not in desired_hpas or hpa["metadata"]["name"] != desired_hpas[role]["metadata"]["name"]:
                    resource, collection = _native_rbg_resource(hpa)
                    _delete_with_uid(resource, namespace, collection, hpa)
            for role in rbg["spec"]["roles"]:
                if role["name"] not in desired_hpas and role.get("scalingAdapter", {}).get("enable"):
                    stopped = _set_rbg_adapter(user_config, role["name"], False)
                    _wait_rbg_adapter(stopped, role["name"], absent=True)
        if rbg:
            latest = _read_managed_resource(C.RBG_RESOURCE, name, settings)
            if latest["metadata"]["uid"] != rbg["metadata"]["uid"]:
                raise ValueError("RBG replaced before runtime configuration submission")
        for prerequisite in plan["prerequisites"]:
            _submit_rbg_object(prerequisite, settings, identity)
        for service in plan["services"]:
            _submit_rbg_object(service, settings, identity, rbg=rbg)
        if plan["policy"] is not None:
            _submit_rbg_object(plan["policy"], settings, identity, rbg=rbg)
        rbg = _submit_rbg_object(plan["rbg"], settings, identity, rbg=rbg)
        # Bind after real UID retrieval; runtime ConfigMap/SA/PVC/RBAC never enter this owner chain.
        for obj in [*plan["services"], *([plan["policy"]] if plan["policy"] is not None else [])]:
            _bind_rbg_attachment(obj, rbg, identity, settings)
        selector = f"{C.RBG_MANAGED_LABEL}={C.RBG_MANAGED_VALUE},{C.RBG_GROUP_LABEL}={name}"
        keep = {service["metadata"]["name"] for service in plan["services"]}
        native = _native_service_names(name, (role["name"] for role in rbg["spec"]["roles"]))
        for old in _deletion_query("services", namespace, selector=selector):
            if old["metadata"]["name"] not in keep | native:
                _verify_native_identity(old, settings)
                _verify_rbg_owner(old, rbg)
                resource, collection = _native_rbg_resource(old)
                _delete_with_uid(resource, namespace, collection, old)
        for role, hpa in (desired_hpas or {}).items():
            ready_group = _enable_rbg_adapter(user_config, rbg, role)
            _wait_rbg_adapter(ready_group, role)
            _submit_rbg_object(hpa, settings, identity, rbg=ready_group)
        if plan["policy"] is not None:
            controller = [
                s
                for s in plan["services"]
                if s["metadata"].get("labels", {}).get(C.RBG_ROLE_LABEL) == C.CONTROLLER
                and any(p.get("port") == 1026 for p in s["spec"].get("ports", []))
            ]
            if len(controller) != 1:
                raise ValueError("Exactly one Controller management Service is needed for Policy reconciliation")
            _request_rbg_coordination(user_config, controller[0]["metadata"]["name"])
    except Exception:
        logger.error(
            "RBG operation stopped; prior successful requests remain applied; runtime configuration may already be updated"
        )
        raise
    logger.info("RBG/%s resources submitted; workload readiness and business inference are not verified", name)


def deploy_services_rbg(user_config: dict, env_config_path: str, *, dry_run: bool = False) -> None:
    """Read cluster state, render deployment and scaling resources, then validate and submit them."""
    from lib.generator.k8s_utils import get_rbg_scaling_current

    # Deliberate dependency failure before any writes; never substitute a Policy generator.
    from lib.rbg.scaling import generate_scaling_resources
    from lib.rbg.render import generate_rbg_resources

    env = json.loads(Path(env_config_path).read_text(encoding="utf-8"))
    if not isinstance(env, dict):
        raise TypeError("env.json must contain an object")
    facts = _read_rbg_facts(user_config)
    live = facts.get("rbg")
    resources = generate_rbg_resources(user_config, env, facts=facts)
    current = get_rbg_scaling_current(live) if live else {"policy": None, "hpas": {}}
    scaling = generate_scaling_resources(user_config, resources["rbg"], is_update=live is not None, current=current)
    plan = _prepare_rbg_plan(resources, scaling, live)
    _execute_rbg_plan(user_config, plan, dry_run=dry_run)


def _hpa_targets_role(hpa: dict, rbg_name: str, role_name: str) -> bool:
    target = hpa.get(C.SPEC, {}).get("scaleTargetRef", {})
    if not isinstance(target, dict):
        return False
    return (
        target.get("apiVersion") == C.RBG_API_VERSION
        and target.get(C.KIND) == C.RBG_SCALING_ADAPTER_KIND
        and target.get(C.NAME) == f"{rbg_name}-{role_name}"
    )


def _reject_automatic_scaling(namespace: str, rbg_name: str, targets: dict[str, int], roles: dict[str, dict]) -> None:
    for role_name in targets:
        scaling_adapter = roles[role_name].get("scalingAdapter", {})
        if not isinstance(scaling_adapter, dict):
            raise ValueError(f"role '{role_name}' scalingAdapter must be a dict")
        if scaling_adapter.get("enable") is True:
            raise ValueError(f"role '{role_name}' is managed by scalingAdapter and cannot be scaled manually")
    hpas = _deletion_query("horizontalpodautoscalers", namespace)
    adapters = _deletion_query("rolebasedgroupscalingadapters", namespace)
    adapter_names = {
        adapter.get(C.METADATA, {}).get(C.NAME) for adapter in adapters if isinstance(adapter.get(C.METADATA), dict)
    }
    for role_name in targets:
        if any(_hpa_targets_role(hpa, rbg_name, role_name) for hpa in hpas):
            raise ValueError(f"role '{role_name}' is managed by an HPA and cannot be scaled manually")
        if f"{rbg_name}-{role_name}" in adapter_names:
            raise ValueError(f"role '{role_name}' still has a ScalingAdapter and cannot be scaled manually")


def _replica_patch(
    rbg: dict,
    targets: dict[str, int],
) -> tuple[list[dict], bool]:
    role_operations = []
    changed = False
    for index, role in enumerate(rbg[C.SPEC][C.ROLES]):
        role_name = role[C.NAME]
        if role_name not in targets:
            continue
        role_operations.append({"op": "test", "path": f"/spec/roles/{index}/name", "value": role_name})
        current = _instance_count(role.get(C.REPLICAS, 0), f"online role '{role_name}' replicas", allow_zero=True)
        if current != targets[role_name]:
            role_operations.append(
                {"op": "replace", "path": f"/spec/roles/{index}/replicas", "value": targets[role_name]}
            )
            changed = True
    if not changed:
        return [], False
    operations = _rbg_patch_tests(rbg) + role_operations
    return operations, True


def _updated_baseline(baseline: dict, user_config: dict, targets: dict[str, int]) -> dict:
    result = deepcopy(baseline)
    target_deploy = _deploy_config(result, "baseline")
    source_deploy = _deploy_config(user_config, "user_config")
    for role_name in targets:
        key = C.RBG_INSTANCE_KEYS_BY_ROLE[role_name]
        target_deploy[key] = deepcopy(source_deploy[key])
    return result


def update_rbg_instance_num(
    user_config: dict,
    baseline_config: dict,
) -> None:
    """Update complete RoleInstance counts without changing per-instance Pod topology."""
    settings = deployment_settings(user_config)
    namespace = settings["namespace"]
    rbg_name = settings["rbg_name"]
    rbg = _read_managed_resource(C.RBG_RESOURCE, rbg_name, settings)
    roles = _role_map(rbg)
    targets = _manual_targets(user_config, baseline_config, roles)
    if not targets:
        return
    _reject_automatic_scaling(namespace, rbg_name, targets, roles)
    for role_name, target in targets.items():
        _validate_port_capacity(user_config, role_name, roles[role_name], target)

    operations, changed = _replica_patch(rbg, targets)
    if changed:
        patched = _patch_rbg_object(C.RBG_RESOURCE, rbg, operations)
        actual_roles = _role_map(patched)
        if any(actual_roles.get(role, {}).get(C.REPLICAS) != target for role, target in targets.items()):
            raise RuntimeError(
                "RBG response does not retain requested replicas; configuration baseline was not updated"
            )

    updated_baseline = _updated_baseline(baseline_config, user_config, targets)
    try:
        update_rbg_runtime_config(updated_baseline, baseline_config)
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        action = "was updated" if changed else "already has the requested replicas"
        message = f"RBG '{namespace}/{rbg_name}' {action}, but its configuration baseline was not updated; reload the configuration and retry: {exc}"
        raise RuntimeError(message) from exc
