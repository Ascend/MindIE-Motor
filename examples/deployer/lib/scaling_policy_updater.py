# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import lib.constant as C
from lib.rbg.config import deployment_settings

_INFER_RESOURCE = "inferservicesets.mindcluster.huawei.com"


Kubectl = Callable[..., str]


def merge_fields(current: dict, updates: dict, path: str, patch: list) -> None:
    if not isinstance(updates, dict) or not updates:
        raise ValueError("Policy updates must be non-empty objects")
    for key, value in updates.items():
        target = path + "/" + key.replace("~", "~0").replace("/", "~1")
        if value is None:
            raise ValueError("Null/deletion is not supported: " + target)
        if isinstance(value, dict) and key in current:
            if not isinstance(current[key], dict):
                raise ValueError("Object type mismatch: " + target)
            merge_fields(current[key], value, target, patch)
        else:
            patch.append({"op": "replace" if key in current else "add", "path": target, "value": value})
            current[key] = deepcopy(value)


def load_policy(path: Path) -> tuple[str, dict]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != {"namespace", "roles"}:
        raise ValueError("Policy JSON must contain only namespace and roles")
    namespace = config["namespace"]
    roles = config["roles"]
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("Policy namespace must be a non-empty string")
    if not isinstance(roles, dict) or not roles or not set(roles).issubset({"prefill", "decode"}):
        raise ValueError("Policy roles must contain prefill and/or decode updates")
    for role, policy in roles.items():
        if not isinstance(policy, dict) or not policy or not set(policy).issubset({"type", "spec"}):
            raise ValueError("Expected a partial scalingPolicy for " + role)
        if "type" in policy and policy["type"] != "HPA":
            raise ValueError("Only HPA policies are supported")
        if "spec" in policy and (not isinstance(policy["spec"], dict) or not policy["spec"]):
            raise ValueError("scalingPolicy.spec must be a non-empty object: " + role)
    return namespace, roles


def _json_object(output: str, action: str) -> dict:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError(action + " returned invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(action + " did not return a JSON object")
    return value


def _resource_items(output: str, resource: str) -> list[dict]:
    value = _json_object(output, "Listing " + resource)
    items = value.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("Listing " + resource + " did not return an object list")
    return items


def _deployment_config(kubectl: Kubectl, namespace: str) -> dict:
    current = _json_object(
        kubectl("get", "configmap", C.MOTOR_CONFIG_CONFIGMAP_NAME, "-n", namespace, "-o", "json"),
        "Reading the deployed configuration",
    )
    data = current.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("user_config.json"), str):
        raise ValueError("ConfigMap motor-config must contain data.user_config.json")
    try:
        user_config = json.loads(data["user_config.json"])
    except json.JSONDecodeError as error:
        raise ValueError("ConfigMap motor-config contains invalid user_config.json") from error
    if not isinstance(user_config, dict):
        raise ValueError("ConfigMap motor-config user_config.json must contain an object")
    deploy_config = user_config.get("motor_deploy_config")
    if not isinstance(deploy_config, dict):
        raise ValueError("Deployed user_config must contain motor_deploy_config")
    if deploy_config.get("deploy_mode") != C.DEPLOY_MODE_RBG or deploy_config.get("job_id") != namespace:
        raise ValueError("Runtime configuration does not identify this RBG namespace")
    metadata = current.get("metadata", {})
    labels = metadata.get("labels", {})
    if (
        metadata.get("name") != C.MOTOR_CONFIG_CONFIGMAP_NAME
        or metadata.get("namespace") != namespace
        or labels.get(C.RBG_MANAGED_LABEL) != C.RBG_MANAGED_VALUE
        or not metadata.get("annotations", {}).get(C.RBG_SERVICE_ID_ANNOTATION)
        or labels.get(C.RBG_GROUP_LABEL) != deployment_settings(user_config)["rbg_name"]
    ):
        raise ValueError("Runtime ConfigMap has a conflicting RBG identity")
    return user_config


def _discover_resources(kubectl: Kubectl, resource: str, namespace: str) -> list[dict]:
    """Only an absent resource API is an empty backend; propagate transport/auth errors."""
    try:
        output = kubectl("get", resource, "-n", namespace, "-o", "json")
    except subprocess.CalledProcessError as error:
        detail = str(error.stderr or error.output or "")
        if "(NotFound)" in detail or "the server doesn't have a resource type" in detail:
            return []
        raise
    return _resource_items(output, resource)


def _update_infer(kubectl: Kubectl, namespace: str, desired: dict, items: list[dict]) -> None:
    if len(items) != 1:
        raise ValueError("Expected exactly one InferServiceSet in deployment namespace; found " + str(len(items)))
    current = items[0]
    name = current["metadata"]["name"]
    roles = current["spec"]["template"]["roles"]
    patch = [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]}]
    expected = {}
    for role_name, updates in desired.items():
        matches = [(i, r) for i, r in enumerate(roles) if r["name"] == role_name]
        if len(matches) != 1:
            raise ValueError("Role must exist exactly once: " + role_name)
        index, role = matches[0]
        path = "/spec/template/roles/" + str(index)
        if "scalingPolicy" not in role:
            spec = updates.get("spec")
            if updates.get("type") != "HPA" or not isinstance(spec, dict):
                raise ValueError("A complete HPA scalingPolicy is required for new policy: " + role_name)
            required = {"minReplicas", "maxReplicas", "metrics", "behavior"}
            if not required.issubset(spec) or not isinstance(spec["metrics"], list) or not spec["metrics"]:
                raise ValueError(
                    "New scalingPolicy requires minReplicas, maxReplicas, non-empty metrics and behavior: " + role_name
                )
            behavior = spec["behavior"]
            for direction in ("scaleUp", "scaleDown"):
                rule = behavior.get(direction) if isinstance(behavior, dict) else None
                if not isinstance(rule, dict) or not {
                    "selectPolicy",
                    "stabilizationWindowSeconds",
                    "policies",
                }.issubset(rule):
                    raise ValueError("New scalingPolicy requires complete " + direction + ": " + role_name)
            # Protect absence against concurrent creation; never overwrite a newly added policy.
            patch += [
                {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                {"op": "test", "path": path + "/name", "value": role_name},
            ]
            expected[role_name] = {}
            merge_fields(expected[role_name], updates, path + "/scalingPolicy", [])
            patch.append({"op": "add", "path": path + "/scalingPolicy", "value": expected[role_name]})
            continue
        policy = role["scalingPolicy"]
        if policy["type"] != "HPA":
            raise ValueError("Existing scalingPolicy must be HPA: " + role_name)
        patch += [
            {"op": "test", "path": path + "/name", "value": role_name},
            {"op": "test", "path": path + "/scalingPolicy", "value": policy},
        ]
        expected[role_name] = deepcopy(policy)
        merge_fields(expected[role_name], updates, path + "/scalingPolicy", patch)
    args = [
        "patch",
        _INFER_RESOURCE,
        name,
        "-n",
        namespace,
        "--type=json",
        "-p",
        json.dumps(patch),
        "-o",
        "json",
    ]
    kubectl(*args, "--dry-run=server")
    result = _json_object(kubectl(*args), "Updating InferServiceSet scaling policy")
    actual = {r["name"]: r.get("scalingPolicy") for r in result["spec"]["template"]["roles"]}
    for role_name, policy in expected.items():
        if actual[role_name] != policy:
            raise RuntimeError("Policy verification failed: " + role_name)
    print(json.dumps({"updated": name, "namespace": namespace, "changes": desired}, indent=2))


def _rbg_identity(resource: dict, namespace: str) -> str:
    metadata = resource.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("RoleBasedGroup has no metadata")
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("RoleBasedGroup metadata.name must be a non-empty string")
    if metadata.get("namespace") != namespace:
        raise ValueError("RoleBasedGroup metadata.namespace does not match the policy namespace")
    return name


def _managed_hpa_for_role(hpas: list[dict], rbg_name: str, role_name: str) -> dict:
    expected_target = {
        "apiVersion": C.RBG_API_VERSION,
        "kind": C.RBG_SCALING_ADAPTER_KIND,
        "name": rbg_name + "-" + role_name,
    }
    matches = []
    for hpa in hpas:
        metadata = hpa.get("metadata")
        spec = hpa.get("spec")
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            continue
        labels = metadata.get("labels")
        if not isinstance(labels, dict):
            continue
        if labels.get(C.RBG_MANAGED_LABEL) != C.RBG_MANAGED_VALUE or labels.get(C.RBG_GROUP_LABEL) != rbg_name:
            continue
        if spec.get("scaleTargetRef") == expected_target:
            matches.append(hpa)
    if len(matches) != 1:
        raise ValueError("Expected exactly one managed HPA for RBG role " + role_name + "; found " + str(len(matches)))
    return matches[0]


def _rbg_hpa_patch(hpa: dict, role_name: str, updates: dict) -> tuple[str, list[dict], dict]:
    metadata = hpa.get("metadata")
    spec = hpa.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ValueError("HPA for role " + role_name + " must contain metadata and spec")
    name = metadata.get("name")
    uid = metadata.get("uid")
    resource_version = metadata.get("resourceVersion")
    if not isinstance(name, str) or not name:
        raise ValueError("HPA for role " + role_name + " has no name")
    if not isinstance(uid, str) or not uid:
        raise ValueError("HPA for role " + role_name + " has no UID")
    if not isinstance(resource_version, str) or not resource_version:
        raise ValueError("HPA for role " + role_name + " has no resourceVersion")
    spec_updates = updates.get("spec")
    if not isinstance(spec_updates, dict) or not spec_updates:
        raise ValueError("RBG HPA update requires a non-empty spec: " + role_name)
    if "scaleTargetRef" in spec_updates:
        raise ValueError("RBG HPA scaleTargetRef cannot be changed: " + role_name)
    expected = deepcopy(spec)
    patch = [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
    ]
    merge_fields(expected, spec_updates, "/spec", patch)
    return name, patch, expected


def _update_rbg_hpas(kubectl: Kubectl, namespace: str, desired: dict, user_config: dict, rbgs: list[dict]) -> None:
    from lib.rbg.scaling import validate_role_hpa_spec

    if len(rbgs) != 1:
        raise ValueError("Expected exactly one managed RoleBasedGroup in deployment namespace; found " + str(len(rbgs)))
    rbg_name = _rbg_identity(rbgs[0], namespace)
    if rbg_name != deployment_settings(user_config)["rbg_name"]:
        raise ValueError("Runtime configuration belongs to a different RoleBasedGroup")
    if rbgs[0]["metadata"].get("labels", {}).get(C.RBG_MANAGED_LABEL) != C.RBG_MANAGED_VALUE:
        raise ValueError("RoleBasedGroup is not managed by Motor")
    hpas = _resource_items(
        kubectl("get", C.RBG_HPA_RESOURCE, "-n", namespace, "-o", "json"),
        C.RBG_HPA_KIND,
    )
    plans = []
    expected_by_name = {}
    for role_name, updates in desired.items():
        hpa = _managed_hpa_for_role(hpas, rbg_name, role_name)
        name, patch, expected = _rbg_hpa_patch(hpa, role_name, updates)
        roles = [role for role in rbgs[0].get("spec", {}).get("roles", []) if role.get("name") == role_name]
        if len(roles) != 1:
            raise ValueError("Expected exactly one RBG role: " + role_name)
        validate_role_hpa_spec(user_config, role_name, roles[0], expected, rbg_name, namespace)
        args = [
            "patch",
            C.RBG_HPA_RESOURCE,
            name,
            "-n",
            namespace,
            "--type=json",
            "-p",
            json.dumps(patch),
            "-o",
            "json",
        ]
        plans.append((name, args))

    for name, args in plans:
        preview = _json_object(kubectl(*args, "--dry-run=server"), "Validating RBG HPA " + name)
        expected_by_name[name] = preview["spec"]
    for name, args in plans:
        result = _json_object(kubectl(*args), "Updating RBG HPA " + name)
        if result.get("spec") != expected_by_name[name]:
            raise RuntimeError("HPA verification failed: " + name)
    print(json.dumps({"updated": [name for name, _ in plans], "namespace": namespace, "changes": desired}, indent=2))


def run_update(
    policy_path: Path, kubeconfig: Path | None = None, *, dry_run: bool = False, other_update: bool = False
) -> None:
    executable = shutil.which("kubectl")
    if executable is None:
        raise FileNotFoundError("kubectl executable is required")

    def kubectl(*args: str) -> str:
        command = [executable]
        if kubeconfig:
            command.append("--kubeconfig=" + str(kubeconfig))
        return subprocess.check_output([*command, "--request-timeout=30s", *args], text=True, stderr=subprocess.PIPE)

    namespace, desired = load_policy(policy_path)
    infer = _discover_resources(kubectl, _INFER_RESOURCE, namespace)
    if infer:
        # Preserve ISS discovery and operation precedence without requiring RBG permissions.
        _update_infer(kubectl, namespace, desired, infer)
        return
    rbgs = _discover_resources(kubectl, C.RBG_RESOURCE, namespace)
    if len(rbgs) != 1:
        raise ValueError(
            "Expected exactly one InferServiceSet or exactly one managed RoleBasedGroup; found " + str(len(rbgs))
        )
    if dry_run or other_update:
        raise ValueError("RBG scaling-policy update cannot be combined with --dry-run or another update operation")
    user_config = _deployment_config(kubectl, namespace)
    _update_rbg_hpas(kubectl, namespace, desired, user_config, rbgs)


def update_scaling_policy(policy_path: str, *, dry_run: bool = False, other_update: bool = False) -> None:
    if "KUBERNETES_SERVICE_HOST" not in os.environ:
        run_update(Path(policy_path), dry_run=dry_run, other_update=other_update)
        return

    # Explicit in-cluster auth; kubeconfig references the projected token, never copies it.
    sa = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ["KUBERNETES_SERVICE_PORT"]
    if ":" in host:
        host = "[" + host + "]"
    for required in (sa / "token", sa / "ca.crt"):
        if not required.is_file():
            raise RuntimeError("Missing ServiceAccount credential file: " + str(required))
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix="-kubeconfig.json") as config_file:
        json.dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [
                    {
                        "name": "in-cluster",
                        "cluster": {
                            "server": "https://" + host + ":" + port,
                            "certificate-authority": str(sa / "ca.crt"),
                        },
                    }
                ],
                "users": [{"name": "job", "user": {"tokenFile": str(sa / "token")}}],
                "contexts": [{"name": "job", "context": {"cluster": "in-cluster", "user": "job"}}],
                "current-context": "job",
            },
            config_file,
        )
        config_file.flush()
        run_update(Path(policy_path), Path(config_file.name), dry_run=dry_run, other_update=other_update)
