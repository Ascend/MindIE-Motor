# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import importlib.util
import json
import os
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3] / "examples" / "cloud_native_deploy"
DEPLOYER_ROOT = ROOT.parent / "deployer"
SPEC = importlib.util.spec_from_file_location("helm_policy_update", DEPLOYER_ROOT / "lib" / "scaling_policy_updater.py")
policy_update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(policy_update)


@pytest.fixture
def helm_binary():
    executable = shutil.which("helm")
    if executable is None:
        pytest.skip("Helm is required for chart rendering tests")
    return executable


@pytest.mark.parametrize("operation", ["deploy", "scale"])
@pytest.mark.parametrize("args", [[], ["--skip-check", "value with spaces"]])
def test_helm_renders_argument_arrays(helm_binary, operation, args):
    output = subprocess.check_output(
        [
            helm_binary,
            "template",
            "test",
            str(ROOT / "helm"),
            "--set",
            f"operation={operation}",
            "--set-json",
            f"jobs.{operation}.args={json.dumps(args)}",
        ],
        text=True,
    )
    jobs = [doc for doc in yaml.safe_load_all(output) if doc and doc["kind"] == "Job"]
    assert len(jobs) == 1
    assert jobs[0]["spec"]["template"]["spec"]["containers"][0].get("args", []) == args


@pytest.mark.parametrize("operation", ["deploy", "scale"])
@pytest.mark.parametrize("args", ["--skip-check", {"argument": "--skip-check"}, [True]])
def test_helm_rejects_invalid_argument_types(helm_binary, operation, args):
    result = subprocess.run(
        [
            helm_binary,
            "template",
            "test",
            str(ROOT / "helm"),
            "--set",
            f"operation={operation}",
            "--set-json",
            f"jobs.{operation}.args={json.dumps(args)}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "args" in result.stderr
    assert "Invalid type" in result.stderr


def test_policy_update_job_runs_image_script_and_allows_slow_image_pulls(helm_binary):
    output = subprocess.check_output(
        [
            helm_binary,
            "template",
            "test",
            str(ROOT / "helm"),
            "--is-upgrade",
            "--set",
            "operation=update-scaling-policy",
            "--set",
            "policyUpdate.roles.prefill.spec.maxReplicas=8",
        ],
        text=True,
    )
    documents = [doc for doc in yaml.safe_load_all(output) if doc]
    jobs = [doc for doc in documents if doc["kind"] == "Job"]
    assert len(jobs) == 1
    assert "activeDeadlineSeconds" not in jobs[0]["spec"]
    assert jobs[0]["spec"]["backoffLimit"] == 0
    container = jobs[0]["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["python3", "/app/examples/deployer/deploy.py"]
    assert container["args"] == ["--update_scaling_policy", "/policy/policy.json"]
    policy_volume = next(
        volume for volume in jobs[0]["spec"]["template"]["spec"]["volumes"] if volume["name"] == "policy"
    )
    policy_config = next(
        doc
        for doc in documents
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == policy_volume["configMap"]["name"]
    )
    assert set(policy_config["data"]) == {"policy.json"}
    assert json.loads(policy_config["data"]["policy.json"]) == {
        "namespace": "mindie",
        "roles": {"prefill": {"spec": {"maxReplicas": 8}}},
    }


def test_merge_preserves_omitted_fields_replaces_arrays_and_escapes_keys():
    current = {"spec": {"minReplicas": 1, "metrics": ["old", "keep"]}}
    patch = []
    policy_update.merge_fields(current, {"spec": {"metrics": ["new"]}, "a~/b": 2}, "/policy", patch)
    assert current == {"spec": {"minReplicas": 1, "metrics": ["new"]}, "a~/b": 2}
    assert patch == [
        {"op": "replace", "path": "/policy/spec/metrics", "value": ["new"]},
        {"op": "add", "path": "/policy/a~0~1b", "value": 2},
    ]


@pytest.mark.parametrize("updates", [{}, {"spec": None}, {"spec": {"metrics": None}}])
def test_merge_rejects_empty_updates_and_deletions(updates):
    with pytest.raises(ValueError):
        policy_update.merge_fields({"spec": {"metrics": []}}, updates, "/policy", [])


def test_load_policy_accepts_partial_update(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps({"namespace": "mindie", "roles": {"decode": {"spec": {"maxReplicas": 8}}}}),
        encoding="utf-8",
    )

    assert policy_update.load_policy(path) == ("mindie", {"decode": {"spec": {"maxReplicas": 8}}})


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"namespace": "mindie"},
        {"namespace": "", "roles": {"decode": {"spec": {"maxReplicas": 8}}}},
        {"namespace": "mindie", "roles": {"union": {"spec": {"maxReplicas": 8}}}},
        {"namespace": "mindie", "roles": {"decode": {}}},
        {"namespace": "mindie", "roles": {"decode": {"type": "Custom"}}},
        {"namespace": "mindie", "roles": {"decode": {"spec": {}}}},
    ],
)
def test_load_policy_rejects_invalid_input(tmp_path, config):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError):
        policy_update.load_policy(path)


@pytest.fixture
def update_api(monkeypatch):
    current = {
        "metadata": {"uid": "uid-1", "resourceVersion": "42", "name": "model"},
        "spec": {
            "template": {
                "roles": [
                    {"name": "prefill", "scalingPolicy": {"type": "HPA", "spec": {"minReplicas": 1, "maxReplicas": 4}}}
                ]
            }
        },
    }
    config = {"namespace": "mindie", "roles": {"prefill": {"spec": {"maxReplicas": 8}}}}
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: json.dumps(config))
    monkeypatch.setattr(policy_update.shutil, "which", lambda _: "/usr/local/bin/kubectl")
    result = deepcopy(current)
    result["spec"]["template"]["roles"][0]["scalingPolicy"]["spec"]["maxReplicas"] = 8
    command = Mock(side_effect=[json.dumps({"items": [current]}), "{}", json.dumps(result)])
    monkeypatch.setattr(policy_update.subprocess, "check_output", command)
    return current, config, command


def test_update_validates_before_writing_and_guards_old_policy(update_api, tmp_path):
    current, _, command = update_api
    policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    calls = [call.args[0] for call in command.call_args_list]
    assert len(calls) == 3
    assert "--dry-run=server" in calls[1]
    assert calls[2] == calls[1][:-1]
    patch = json.loads(calls[2][calls[2].index("-p") + 1])
    assert patch == [
        {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
        {"op": "test", "path": "/spec/template/roles/0/name", "value": "prefill"},
        {
            "op": "test",
            "path": "/spec/template/roles/0/scalingPolicy",
            "value": current["spec"]["template"]["roles"][0]["scalingPolicy"],
        },
        {"op": "replace", "path": "/spec/template/roles/0/scalingPolicy/spec/maxReplicas", "value": 8},
    ]


@pytest.mark.parametrize("count", [0, 2])
def test_ambiguous_target_never_writes(update_api, tmp_path, count):
    current, _, command = update_api
    command.side_effect = [json.dumps({"items": [current] * count})]
    with pytest.raises(ValueError, match="exactly one InferServiceSet"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert command.call_count == 1


def test_dry_run_failure_prevents_write(update_api, tmp_path):
    current, _, command = update_api
    command.side_effect = [json.dumps({"items": [current]}), subprocess.CalledProcessError(1, "kubectl")]
    with pytest.raises(subprocess.CalledProcessError):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert command.call_count == 2


def test_missing_policy_requires_complete_configuration(update_api, tmp_path):
    current, _, command = update_api
    del current["spec"]["template"]["roles"][0]["scalingPolicy"]
    command.side_effect = [json.dumps({"items": [current]})]
    with pytest.raises(ValueError, match="complete HPA"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert command.call_count == 1


@pytest.mark.parametrize("fail", [False, True])
def test_kubeconfig_is_private_and_removed_after_execution(monkeypatch, fail):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    created = []

    def run(policy_path, kubeconfig=None):
        assert policy_path == Path("/policy/policy.json")
        created.append(kubeconfig)
        assert kubeconfig.stat().st_mode & 0o777 == 0o600
        config = json.loads(kubeconfig.read_text(encoding="utf-8"))
        assert config["clusters"][0]["cluster"]["server"] == "https://[fd00::1]:443"
        assert config["users"][0]["user"] == {"tokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token"}
        if fail:
            raise RuntimeError("update failed")

    monkeypatch.setattr(policy_update, "run_update", run)
    if fail:
        with pytest.raises(RuntimeError, match="update failed"):
            policy_update.update_scaling_policy("/policy/policy.json")
    else:
        policy_update.update_scaling_policy("/policy/policy.json")
    assert not created[0].exists()


def test_manual_update_uses_current_kubectl_context(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    run = Mock()
    monkeypatch.setattr(policy_update, "run_update", run)
    policy_update.update_scaling_policy("policy.json")
    run.assert_called_once_with(Path("policy.json"))


@pytest.mark.parametrize(
    "script, mode", [("entrypoint.sh", "--nostep"), ("entrypoint-scale.sh", "--update_instance_num")]
)
@pytest.mark.parametrize("extra", [[], ["--skip-check", "--example", "value with spaces"]])
def test_entrypoints_forward_skip_check_and_preserve_argument_boundaries(tmp_path, script, mode, extra):
    (tmp_path / "deploy.py").write_text("import json,sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
    command = [shutil.which("bash"), str(ROOT / script), *extra]
    output = subprocess.check_output(
        command, text=True, env={**os.environ, "WORKDIR": str(tmp_path), "CONFIG_DIR": "/config with spaces"}
    )
    assert json.loads(output) == ["--config_dir", "/config with spaces", mode, *extra]
