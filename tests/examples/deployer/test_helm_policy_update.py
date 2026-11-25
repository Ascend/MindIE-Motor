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


def deployed_configmap(mode):
    return {
        "metadata": {
            "name": "motor-config",
            "namespace": "mindie",
            "labels": {"app.kubernetes.io/managed-by": "motor-deployer", "rbg.workloads.x-k8s.io/group-name": "model"},
            "annotations": {policy_update.C.RBG_SERVICE_ID_ANNOTATION: "identity"},
        },
        "data": {
            "user_config.json": json.dumps(
                {
                    "motor_deploy_config": {
                        "deploy_mode": mode,
                        "job_id": "mindie",
                        "rbg_config": {"name": "model"},
                    }
                }
            ),
        },
    }


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
    command = Mock(
        side_effect=[
            json.dumps({"items": [current]}),
            "{}",
            json.dumps(result),
        ]
    )
    monkeypatch.setattr(policy_update.subprocess, "check_output", command)
    return current, config, command


def test_update_validates_before_writing_and_guards_old_policy(update_api, tmp_path):
    current, _, command = update_api
    policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    calls = [call.args[0] for call in command.call_args_list]
    assert len(calls) == 3
    assert calls[0][-6:] == ["get", policy_update._INFER_RESOURCE, "-n", "mindie", "-o", "json"]
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
    command.side_effect = [
        json.dumps({"items": [current] * count}),
        json.dumps({"items": []}),
    ]
    with pytest.raises(ValueError, match="exactly one InferServiceSet"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert command.call_count == (1 if count else 2)


def test_dry_run_failure_prevents_write(update_api, tmp_path):
    current, _, command = update_api
    command.side_effect = [
        json.dumps({"items": [current]}),
        subprocess.CalledProcessError(1, "kubectl"),
    ]
    with pytest.raises(subprocess.CalledProcessError):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert command.call_count == 2


def test_missing_policy_requires_complete_configuration(update_api, tmp_path):
    current, _, command = update_api
    del current["spec"]["template"]["roles"][0]["scalingPolicy"]
    command.side_effect = [
        json.dumps({"items": [current]}),
    ]
    with pytest.raises(ValueError, match="complete HPA"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert command.call_count == 1


def test_update_rejects_namespace_without_supported_resources(update_api, tmp_path):
    _, _, command = update_api
    command.side_effect = [json.dumps({"items": []}), json.dumps({"items": []})]
    with pytest.raises(ValueError, match="exactly one"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert command.call_count == 2


@pytest.fixture
def rbg_update_api(monkeypatch):
    rbg = {
        "metadata": {
            "name": "model",
            "namespace": "mindie",
            "labels": {policy_update.C.RBG_MANAGED_LABEL: policy_update.C.RBG_MANAGED_VALUE},
        },
        "spec": {"roles": [{"name": "prefill", "replicas": 1, "template": {"spec": {"containers": []}}}]},
    }
    role = rbg["spec"]["roles"][0]
    role["standalonePattern"] = {"template": role.pop("template")}
    hpa = {
        "metadata": {
            "name": "model-prefill-hpa",
            "namespace": "mindie",
            "uid": "hpa-uid",
            "resourceVersion": "12",
            "labels": {
                "app.kubernetes.io/managed-by": "motor-deployer",
                "rbg.workloads.x-k8s.io/group-name": "model",
            },
        },
        "spec": {
            "minReplicas": 1,
            "maxReplicas": 4,
            "metrics": [
                {
                    "type": "External",
                    "external": {
                        "metric": {
                            "name": "motor_prefill_utilization",
                            "selector": {
                                "matchLabels": {
                                    "kubernetes_namespace": "mindie",
                                    "infer_huawei_com_inferservice_name": "model-0",
                                }
                            },
                        },
                        "target": {"type": "Value", "value": "0.8"},
                    },
                }
            ],
            "scaleTargetRef": {
                "apiVersion": "workloads.x-k8s.io/v1alpha2",
                "kind": "RoleBasedGroupScalingAdapter",
                "name": "model-prefill",
            },
        },
    }
    config = {"namespace": "mindie", "roles": {"prefill": {"spec": {"maxReplicas": 8}}}}
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: json.dumps(config))
    monkeypatch.setattr(policy_update.shutil, "which", lambda _: "/usr/local/bin/kubectl")
    result = deepcopy(hpa)
    result["spec"]["maxReplicas"] = 8
    command = Mock(
        side_effect=[
            json.dumps({"items": []}),
            json.dumps({"items": [rbg]}),
            json.dumps(deployed_configmap("rbg")),
            json.dumps({"items": [hpa]}),
            json.dumps(result),
            json.dumps(result),
        ]
    )
    monkeypatch.setattr(policy_update.subprocess, "check_output", command)
    return rbg, hpa, config, command


def test_rbg_update_patches_existing_role_hpa(rbg_update_api, tmp_path):
    _, _, _, command = rbg_update_api

    policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")

    calls = [call.args[0] for call in command.call_args_list]
    assert len(calls) == 6
    assert calls[1][-6:] == [
        "get",
        "rolebasedgroups.workloads.x-k8s.io",
        "-n",
        "mindie",
        "-o",
        "json",
    ]
    assert "--dry-run=server" in calls[4]
    assert calls[5] == calls[4][:-1]
    patch = json.loads(calls[5][calls[5].index("-p") + 1])
    assert patch == [
        {"op": "test", "path": "/metadata/uid", "value": "hpa-uid"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "12"},
        {"op": "replace", "path": "/spec/maxReplicas", "value": 8},
    ]


@pytest.mark.parametrize("changed_after_preview", [False, True])
def test_rbg_hpa_update_verifies_against_server_normalization(rbg_update_api, tmp_path, changed_after_preview):
    rbg, hpa, config, command = rbg_update_api
    config["roles"]["prefill"]["spec"] = {"metrics": deepcopy(hpa["spec"]["metrics"])}
    preview = deepcopy(hpa)
    preview["spec"].update(deepcopy(config["roles"]["prefill"]["spec"]))
    preview["spec"]["metrics"][0]["external"]["target"]["value"] = "800m"
    result = deepcopy(preview)
    if changed_after_preview:
        result["spec"]["metrics"][0]["external"]["target"]["value"] = "900m"
    command.side_effect = [
        json.dumps({"items": []}),
        json.dumps({"items": [rbg]}),
        json.dumps(deployed_configmap("rbg")),
        json.dumps({"items": [hpa]}),
        json.dumps(preview),
        json.dumps(result),
    ]
    if changed_after_preview:
        with pytest.raises(RuntimeError, match="HPA verification failed"):
            policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    else:
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert command.call_count == 6


@pytest.mark.parametrize("count", [0, 2])
def test_rbg_update_rejects_missing_or_ambiguous_rbg(rbg_update_api, tmp_path, count):
    rbg, _, _, command = rbg_update_api
    command.side_effect = [
        json.dumps({"items": []}),
        json.dumps({"items": [rbg] * count}),
        json.dumps(deployed_configmap("rbg")),
    ]

    with pytest.raises(ValueError, match="exactly one managed RoleBasedGroup"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")

    assert command.call_count == 2


@pytest.mark.parametrize("count", [0, 2])
def test_rbg_update_rejects_missing_or_ambiguous_role_hpa(rbg_update_api, tmp_path, count):
    rbg, hpa, _, command = rbg_update_api
    command.side_effect = [
        json.dumps({"items": []}),
        json.dumps({"items": [rbg]}),
        json.dumps(deployed_configmap("rbg")),
        json.dumps({"items": [hpa] * count}),
    ]

    with pytest.raises(ValueError, match="exactly one managed HPA for RBG role prefill"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")

    assert command.call_count == 4


def test_rbg_dry_run_failure_prevents_write(rbg_update_api, tmp_path):
    rbg, hpa, _, command = rbg_update_api
    command.side_effect = [
        json.dumps({"items": []}),
        json.dumps({"items": [rbg]}),
        json.dumps(deployed_configmap("rbg")),
        json.dumps({"items": [hpa]}),
        subprocess.CalledProcessError(1, "kubectl"),
    ]

    with pytest.raises(subprocess.CalledProcessError):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")

    assert command.call_count == 5


def test_rbg_update_rejects_scale_target_change(rbg_update_api, tmp_path):
    rbg, hpa, config, command = rbg_update_api
    config["roles"]["prefill"]["spec"] = {"scaleTargetRef": {"name": "other"}}
    command.side_effect = [
        json.dumps({"items": []}),
        json.dumps({"items": [rbg]}),
        json.dumps(deployed_configmap("rbg")),
        json.dumps({"items": [hpa]}),
    ]

    with pytest.raises(ValueError, match="scaleTargetRef cannot be changed"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")

    assert command.call_count == 4


@pytest.mark.parametrize("fail", [False, True])
def test_kubeconfig_is_private_and_removed_after_execution(monkeypatch, fail):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    monkeypatch.setattr(Path, "is_file", lambda _: True)
    created = []

    def run(policy_path, kubeconfig=None, **options):
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
    run.assert_called_once_with(Path("policy.json"), dry_run=False, other_update=False)


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


def test_rbg_policy_update_rejects_port_overflow_before_any_patch(rbg_update_api, tmp_path):
    rbg, hpa, config, command = rbg_update_api
    config["roles"]["prefill"]["spec"] = {"maxReplicas": 16}
    env = {"MOTOR_INSTANCE_PORTS_ENABLE": "1", "ASCEND_BASE_PORT": "12000", "HIXL_LISTEN_PORT": "16000"}
    rbg["spec"] = {
        "roles": [
            {
                "name": "prefill",
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [{"name": "engine", "env": [{"name": k, "value": v} for k, v in env.items()]}]
                    }
                },
            }
        ]
    }
    role = rbg["spec"]["roles"][0]
    role["standalonePattern"] = {"template": role.pop("template")}
    runtime = deployed_configmap("rbg")
    cfg = json.loads(runtime["data"]["user_config.json"])
    cfg["motor_engine_prefill_config"] = {
        "motor_nodemanger_config": {
            "port_allocator_config": {"enable": False},
            "api_config": {"node_manager_port": 1026},
            "endpoint_config": {"base_port": 8000},
        },
        "engine_config": {"data_parallel_rpc_port": 9000},
    }
    runtime["data"]["user_config.json"] = json.dumps(cfg)
    result = deepcopy(hpa)
    result["spec"]["maxReplicas"] = 16
    command.side_effect = [
        json.dumps({"items": []}),
        json.dumps({"items": [rbg]}),
        json.dumps(runtime),
        json.dumps({"items": [hpa]}),
        json.dumps(result),
        json.dumps(result),
    ]
    with pytest.raises(ValueError, match="ASCEND_BASE_PORT exceeds 65535"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert not any("patch" in call.args[0] for call in command.call_args_list)


def test_rbg_hpa_update_rejects_lost_metric_scope(rbg_update_api, tmp_path):
    rbg, hpa, config, command = rbg_update_api
    metrics = deepcopy(hpa["spec"]["metrics"])
    del metrics[0]["external"]["metric"]["selector"]
    config["roles"]["prefill"]["spec"] = {"metrics": metrics}
    result = deepcopy(hpa)
    result["spec"]["metrics"] = metrics
    command.side_effect = [
        json.dumps({"items": []}),
        json.dumps({"items": [rbg]}),
        json.dumps(deployed_configmap("rbg")),
        json.dumps({"items": [hpa]}),
        json.dumps(result),
        json.dumps(result),
    ]
    with pytest.raises(ValueError, match="selector labels"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig")
    assert not any("patch" in call.args[0] for call in command.call_args_list)


@pytest.mark.parametrize("options", [{}, {"dry_run": True}, {"other_update": True}])
def test_policy_update_discovers_iss_without_fixed_configmap(monkeypatch, tmp_path, options):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"namespace": "demo", "roles": {"prefill": {"spec": {"maxReplicas": 3}}}}))
    monkeypatch.setattr(policy_update.shutil, "which", lambda _: "kubectl")
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if policy_update.C.RBG_RESOURCE in args:
            raise subprocess.CalledProcessError(1, args, stderr="Forbidden: no RBG read permission")
        if "configmap" in args:
            raise AssertionError("ISS service1 must not read motor-config")
        return json.dumps(
            {"items": [{"metadata": {"name": "service1-vllm"}}] if policy_update._INFER_RESOURCE in args else []}
        )

    updated = []
    monkeypatch.setattr(policy_update.subprocess, "check_output", command)
    monkeypatch.setattr(policy_update, "_update_infer", lambda *a, **kw: updated.append(True))
    policy_update.run_update(path, **options)
    assert updated == [True]
    assert len(calls) == 1


@pytest.mark.parametrize("error", ["Forbidden", "connection refused"])
def test_policy_discovery_preserves_api_errors(monkeypatch, tmp_path, error):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"namespace": "demo", "roles": {"prefill": {"spec": {"maxReplicas": 3}}}}))
    monkeypatch.setattr(policy_update.shutil, "which", lambda _: "kubectl")

    def command(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr=error)

    monkeypatch.setattr(policy_update.subprocess, "check_output", command)
    with pytest.raises(subprocess.CalledProcessError):
        policy_update.run_update(path)


@pytest.mark.parametrize("relative", ["k8s/clusterrole.yaml", "helm/templates/rbac.yaml.tpl"])
def test_deployer_rbac_can_grant_runtime_permissions_without_extra_serviceaccount_access(relative):
    text = (ROOT / relative).read_text(encoding="utf-8")
    rules = yaml.safe_load("rules:" + text.split("rules:", 1)[1].split("---", 1)[0])["rules"]
    for resource in ("pods", "nodes", "configmaps"):
        verbs = {verb for rule in rules if resource in rule["resources"] for verb in rule["verbs"]}
        assert {"get", "list", "watch"} <= verbs
    account = next(rule for rule in rules if "serviceaccounts" in rule["resources"])
    assert set(account["verbs"]) == {"get", "create", "patch", "delete"}
    assert not any({"escalate", "bind"} & set(rule["verbs"]) for rule in rules)


def test_basic_job_and_helm_rbg_examples_render_without_scaling_resources():
    from lib.rbg.render import generate_rbg_resources

    root = Path(__file__).resolve().parents[3] / "examples/cloud_native_deploy"
    helm = yaml.safe_load((root / "helm/values-rbg.yaml").read_text(encoding="utf-8"))
    job = yaml.safe_load((root / "k8s/configmap-user-config-rbg.yaml").read_text(encoding="utf-8"))
    config = json.loads(helm["userConfig"])
    assert config == json.loads(job["data"]["user_config.json"])
    assert helm["operation"] == "deploy"
    assert "scaling_policy" not in config
    assert "coordinated_policy" not in config["motor_deploy_config"]["rbg_config"]
    assert config["motor_deploy_config"]["job_id"] == job["metadata"]["namespace"]
    assert config["motor_deploy_config"]["deploy_mode"] == "rbg"
    config["motor_deploy_config"]["image_name"] = "motor:test"
    bundle = generate_rbg_resources(config, {}, facts={"accelerator_type": "module-910c-8"})
    assert bundle["rbg"]["metadata"]["namespace"] == job["metadata"]["namespace"]
    assert bundle["services"]
    runtime = next(obj for obj in bundle["prerequisites"] if obj["kind"] == "ConfigMap")
    assert json.loads(runtime["data"]["user_config.json"])["motor_deploy_config"]["deploy_mode"] == "rbg"


@pytest.mark.parametrize("options", [{"dry_run": True}, {"other_update": True}])
def test_rbg_policy_update_rejects_conflicting_flags_before_writing(rbg_update_api, tmp_path, options):
    _, _, _, command = rbg_update_api
    with pytest.raises(ValueError, match="RBG"):
        policy_update.run_update(Path("/policy/policy.json"), tmp_path / "kubeconfig", **options)
    assert not any("patch" in call.args[0] for call in command.call_args_list)


@pytest.mark.parametrize("mode", ["infer_service_set", "multi_deployment", "rbg"])
def test_helm_cleanup_selects_rbg_branch_only_for_rbg(helm_binary, mode):
    output = subprocess.check_output(
        [
            helm_binary,
            "template",
            "test",
            str(ROOT / "helm"),
            "--set",
            "operation=cleanup",
            "--set-json",
            "userConfig=" + json.dumps(json.dumps({"motor_deploy_config": {"job_id": "demo", "deploy_mode": mode}})),
        ],
        text=True,
    )
    job = next(doc for doc in yaml.safe_load_all(output) if doc and doc["kind"] == "Job")
    env = {entry["name"]: entry["value"] for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env.get("MOTOR_DEPLOY_MODE") == ("rbg" if mode == "rbg" else None)
