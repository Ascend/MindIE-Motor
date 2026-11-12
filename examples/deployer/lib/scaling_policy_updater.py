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
from copy import deepcopy
from pathlib import Path


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


def run_update(policy_path: Path, kubeconfig: Path | None = None) -> None:
    executable = shutil.which("kubectl")
    if executable is None:
        raise FileNotFoundError("kubectl executable is required")

    def kubectl(*args: str) -> str:
        command = [executable]
        if kubeconfig:
            command.append("--kubeconfig=" + str(kubeconfig))
        return subprocess.check_output([*command, "--request-timeout=30s", *args], text=True)

    namespace, desired = load_policy(policy_path)
    resource = "inferservicesets.mindcluster.huawei.com"
    items = json.loads(kubectl("get", resource, "-n", namespace, "-o", "json"))["items"]
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
    args = ["patch", resource, name, "-n", namespace, "--type=json", "-p", json.dumps(patch), "-o", "json"]
    kubectl(*args, "--dry-run=server")
    result = json.loads(kubectl(*args))
    actual = {r["name"]: r.get("scalingPolicy") for r in result["spec"]["template"]["roles"]}
    for role_name, policy in expected.items():
        if actual[role_name] != policy:
            raise RuntimeError("Policy verification failed: " + role_name)
    print(json.dumps({"updated": name, "namespace": namespace, "changes": desired}, indent=2))


def update_scaling_policy(policy_path: str) -> None:
    if "KUBERNETES_SERVICE_HOST" not in os.environ:
        run_update(Path(policy_path))
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
        run_update(Path(policy_path), Path(config_file.name))
