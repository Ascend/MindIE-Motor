# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Test runtime updates using rendered RBG resources and mocked kubectl process I/O."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import deploy
import lib.constant as C
import pytest
from lib.generator import k8s_utils
from lib.rbg import lifecycle as rbg_lifecycle
from lib.rbg import render
from lib.rbg.render import generate_rbg_resources


@pytest.fixture
def resources():
    config = {
        "motor_deploy_config": {
            "deploy_mode": "rbg",
            "job_id": "test-motor",
            "image_name": "motor:test",
            "p_instances_num": 1,
            "d_instances_num": 0,
            "single_p_instance_pod_num": 1,
            "single_d_instance_pod_num": 1,
            "p_pod_npu_num": 8,
            "d_pod_npu_num": 8,
        },
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "engine_config": {"model": "/weights"},
        },
        "motor_engine_decode_config": {
            "engine_type": "vllm",
            "engine_config": {"model": "/weights"},
        },
        "motor_controller_config": {"logging_config": {"log_level": "INFO"}},
        "motor_coordinator_config": {},
    }
    bundle = generate_rbg_resources(config, {}, facts={"accelerator_type": "module-910b-8"})
    runtime = next(doc for doc in bundle["prerequisites"] if doc["kind"] == "ConfigMap")
    runtime["metadata"].update(uid="cm-uid", resourceVersion="17")
    desired = deepcopy(bundle["user_config"])
    desired["motor_controller_config"]["logging_config"]["log_level"] = "DEBUG"
    bundle["rbg"]["metadata"].update(uid="rbg-uid", resourceVersion="10")
    return bundle["rbg"], runtime, desired


def recorded_command(command):
    recorded = list(command)
    if "--patch-file" in command:
        recorded += ["--patch-json", Path(command[command.index("--patch-file") + 1]).read_text(encoding="utf-8")]
    return recorded


def command_responses(monkeypatch, responses):
    """Provide explicit process results, without emulating Kubernetes operations."""
    outputs = iter(responses)
    commands = []

    def run(command, **kwargs):
        recorded = list(command)
        if "--patch-file" in command:
            recorded += ["--patch-json", Path(command[command.index("--patch-file") + 1]).read_text(encoding="utf-8")]
        commands.append(recorded)
        result = next(outputs)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, SimpleNamespace):
            return result
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(result) if result is not None else "",
            stderr="",
        )

    monkeypatch.setattr(k8s_utils.subprocess, "run", run)
    return commands


def updated_runtime(runtime, desired):
    result = deepcopy(runtime)
    result["data"]["user_config.json"] = json.dumps(desired, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return result


@pytest.mark.parametrize("intent", ["create", "enable", "preserve", "disable", "policy"])
def test_deployment_entry_generates_and_submits_scaling_intent(monkeypatch, tmp_path, resources, intent):
    """Exercise resource and scaling generation through the deployment entry point with mocked cluster operations."""
    from lib.rbg.scaling import generate_scaling_resources

    live, runtime, config = deepcopy(resources)
    config["motor_deploy_config"]["rbg_config"] = {}
    updating = intent in {"preserve", "disable", "policy"}
    if updating:
        next(r for r in live["spec"]["roles"] if r["name"] == "prefill")["replicas"] = 3
    if intent == "enable":
        config["scaling_policy"] = {"prefill": {"min_replicas": 1, "max_replicas": 4}}
    if intent == "disable":
        config["scaling_policy"] = {}
    if intent == "policy":
        config["motor_deploy_config"]["rbg_config"]["coordinated_policy"] = {
            "policies": [
                {
                    "name": "engines",
                    "roles": ["prefill", "decode"],
                    "strategy": {"scaling": {"maxSkew": "100%", "progression": "OrderReady"}},
                }
            ]
        }
    facts = {"accelerator_type": "module-910b-8"}
    if updating:
        facts.update(rbg=live, runtime_configmap=runtime, services={})
    monkeypatch.chdir(tmp_path)
    env = tmp_path / "env.json"
    env.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(rbg_lifecycle, "_read_rbg_facts", lambda _: deepcopy(facts))
    current = {"policy": None, "hpas": {}}
    monkeypatch.setattr(k8s_utils, "get_rbg_scaling_current", lambda _: current)
    monkeypatch.setattr(rbg_lifecycle, "_preflight_rbg_plan", lambda *a: None)
    monkeypatch.setattr(
        rbg_lifecycle,
        "_deletion_query",
        lambda resource, *a, **kw: [live] if resource.startswith("rolebasedgroups.") and updating else [],
    )
    monkeypatch.setattr(rbg_lifecycle, "_read_managed_resource", lambda *a: deepcopy(live))
    events = []

    def submit(obj, settings, identity, *, rbg=None):
        events.append(("submit", deepcopy(obj)))
        result = deepcopy(obj)
        result["metadata"].update(uid="real-uid", resourceVersion="1")
        return result

    def bind(obj, group, identity, settings):
        assert group["metadata"]["uid"] == "real-uid"
        assert settings == render.deployment_settings(config)
        events.append(("bind", obj["kind"]))

    monkeypatch.setattr(rbg_lifecycle, "_submit_rbg_object", submit)
    monkeypatch.setattr(rbg_lifecycle, "_bind_rbg_attachment", bind)
    monkeypatch.setattr(rbg_lifecycle, "_enable_rbg_adapter", lambda *a: events.append(("adapter", True)) or live)
    monkeypatch.setattr(rbg_lifecycle, "_wait_rbg_adapter", lambda *a, **kw: events.append(("wait", True)))
    monkeypatch.setattr(rbg_lifecycle, "_request_rbg_coordination", lambda *a: events.append(("coordinate", True)))
    rbg_lifecycle.deploy_services_rbg(config, str(env))
    submitted = [value for action, value in events if action == "submit"]
    kinds = [obj["kind"] for obj in submitted]
    assert "ConfigMap" in kinds and "RoleBasedGroup" in kinds
    expected_policy = not updating or intent == "policy"
    assert ("CoordinatedPolicy" in kinds) == expected_policy
    assert any(action == "coordinate" for action, _ in events) == expected_policy
    if expected_policy:
        assert kinds.index("Service") < kinds.index("CoordinatedPolicy") < kinds.index("RoleBasedGroup")
    group = next(obj for obj in submitted if obj["kind"] == "RoleBasedGroup")
    assert next(r for r in group["spec"]["roles"] if r["name"] == "prefill")["replicas"] == 1
    if intent == "enable":
        hpa = next(obj for obj in submitted if obj["kind"] == "HorizontalPodAutoscaler")
        assert hpa["spec"]["scaleTargetRef"]["name"].endswith("-prefill")
        assert [action for action, _ in events][-4:] == ["adapter", "wait", "submit", "coordinate"]
    else:
        assert "HorizontalPodAutoscaler" not in kinds
    # Preview and submission use the same real generators and validation contract.
    generated = generate_scaling_resources(config, group, is_update=updating, current=current)
    assert (generated["policy"] is not None) == expected_policy
    events.clear()
    rbg_lifecycle.deploy_services_rbg(config, str(env), dry_run=True)
    assert not events


def test_deployment_entry_propagates_submission_failure(monkeypatch, tmp_path, resources):
    _, _, config = deepcopy(resources)
    monkeypatch.chdir(tmp_path)
    env = tmp_path / "env.json"
    env.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(rbg_lifecycle, "_read_rbg_facts", lambda _: {"accelerator_type": "module-910b-8"})
    monkeypatch.setattr(rbg_lifecycle, "_preflight_rbg_plan", lambda *a: None)
    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", lambda *a, **kw: [])
    calls = []

    def fail(obj, *a, **kw):
        calls.append(obj["kind"])
        raise RuntimeError("Forbidden")

    monkeypatch.setattr(rbg_lifecycle, "_submit_rbg_object", fail)
    with pytest.raises(RuntimeError, match="Forbidden"):
        rbg_lifecycle.deploy_services_rbg(config, str(env))
    assert len(calls) == 1


@pytest.mark.parametrize("state", ["running", "missing_service", "missing_pod", "forbidden", "explicit"])
def test_kv_online_facts_determine_rendered_roles_services_and_addresses(monkeypatch, resources, state):
    """Verify that Service/Pod lookup results determine rendered KV resources and addresses."""
    _, _, config = deepcopy(resources)
    config["kv_cache_store_config"] = {
        "target_job_id": "other",
        "backend": "mooncake",
        "eviction_high_watermark_ratio": 0.95,
        "eviction_ratio": 0.05,
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "shared-kv", "namespace": "other", "uid": "kv-service"},
        "spec": {"selector": {C.RBG_ROLE_LABEL: "kv-store"}, "clusterIP": "10.0.0.2"},
    }
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "kv-0", "namespace": "other", "uid": "kv-pod", "labels": {C.RBG_ROLE_LABEL: "kv-store"}},
        "status": {"phase": "Running"},
    }
    calls = []

    def query(resource, namespace, **kwargs):
        calls.append(resource)
        if state in {"forbidden", "explicit"}:
            raise RuntimeError("Forbidden")
        if resource == "services":
            return [] if state == "missing_service" else [service]
        assert kwargs["selector"] == C.RBG_ROLE_LABEL + "=kv-store"
        return [] if state == "missing_pod" else [pod]

    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", query)
    if state == "explicit":
        config["kv_cache_store_config"]["master_server_address"] = "10.0.0.9:50051"
    if state == "forbidden":
        with pytest.raises(RuntimeError, match="Forbidden"):
            rbg_lifecycle._read_rbg_kv_facts(config)
        return
    facts = rbg_lifecycle._read_rbg_kv_facts(config)
    facts["accelerator_type"] = "module-910b-8"
    bundle = generate_rbg_resources(config, {}, facts=facts)
    local = state in {"missing_service", "missing_pod"}
    assert any(r["name"] == "kv-store" and r["replicas"] > 0 for r in bundle["rbg"]["spec"]["roles"]) == local
    assert any(s["metadata"].get("labels", {}).get(C.RBG_ROLE_LABEL) == "kv-store" for s in bundle["services"]) == local
    if not local:
        address = "10.0.0.9" if state == "explicit" else "shared-kv.other.svc.cluster.local"
        assert address in json.dumps(bundle["rbg"])
    if state == "explicit":
        assert not calls


@pytest.mark.parametrize("change", [None, "pattern", "size", "restart"])
def test_fresh_rbg_submission_rechecks_rendered_leader_worker_topology(monkeypatch, resources, change):
    _, _, config = resources
    original = render._load_templates

    def templates():
        rbg, catalog, services, rbac = original()
        for name in ("prefill", "decode"):
            custom = catalog[name].pop("customComponentsPattern")
            catalog[name]["leaderWorkerPattern"] = {
                "size": 1,
                "template": custom["components"][0]["template"],
                "restartPolicyConfig": custom["restartPolicyConfig"],
            }
        return rbg, catalog, services, rbac

    monkeypatch.setattr(render, "_load_templates", templates)
    bundle = generate_rbg_resources(config, {}, facts={"accelerator_type": "module-910b-8"})
    snapshot = deepcopy(bundle["rbg"])
    snapshot["metadata"].update(uid="live-rbg", resourceVersion="1")
    plan = rbg_lifecycle._prepare_rbg_plan(bundle, {"policy": None, "hpas": None}, snapshot)
    desired_role = next(r for r in plan["rbg"]["spec"]["roles"] if r["name"] == "prefill")
    desired_role["leaderWorkerPattern"]["template"]["metadata"].setdefault("annotations", {})["example.com/update"] = (
        "requested"
    )
    live = deepcopy(snapshot)
    role = next(role for role in live["spec"]["roles"] if role["name"] == "prefill")
    if change == "pattern":
        role["standalonePattern"] = {"template": role.pop("leaderWorkerPattern")["template"]}
    elif change == "size":
        role["leaderWorkerPattern"]["size"] += 1
    elif change == "restart":
        role["leaderWorkerPattern"]["restartPolicyConfig"]["type"] = "None"
    else:
        role["replicas"] = 7
    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", lambda *a, **kw: [live])
    writes = []

    def patch_object(*args, dry_run=False):
        if dry_run:
            return {**live, "spec": args[2][-1]["value"]}
        writes.append(args)
        return live

    monkeypatch.setattr(rbg_lifecycle, "_patch_rbg_object", patch_object)
    settings = render.deployment_settings(config)
    if change:
        with pytest.raises(ValueError, match="requires redeployment"):
            rbg_lifecycle._submit_rbg_object(plan["rbg"], settings, plan["identity"], rbg=snapshot)
        assert not writes
    else:
        submitted = rbg_lifecycle._submit_rbg_object(plan["rbg"], settings, plan["identity"], rbg=snapshot)
        assert next(r for r in submitted["spec"]["roles"] if r["name"] == "prefill")["replicas"] == 7
        spec = next(p["value"] for p in writes[0][2] if p["path"] == "/spec")
        patched_role = next(r for r in spec["roles"] if r["name"] == "prefill")
        assert patched_role["replicas"] == desired_role["replicas"]
        assert patched_role["leaderWorkerPattern"] == desired_role["leaderWorkerPattern"]


def test_real_entry_updates_only_effective_config_and_keeps_identity(monkeypatch, resources):
    rbg, runtime, desired = resources
    incoming = deepcopy(desired)
    incoming["motor_controller_config"]["fault_tolerance_config"].pop("enable_scale_p2d")
    incoming["motor_deploy_config"]["image_name"] = "ignored:non-whitelisted"
    result = updated_runtime(runtime, desired)
    before = deepcopy(runtime)
    commands = command_responses(monkeypatch, [runtime, rbg, runtime, result])

    deploy.handle_update_config(incoming)

    assert len(commands) == 4
    patch_command = commands[-1]
    assert patch_command[1:4] == ["patch", "configmap", "motor-config"]
    patch = json.loads(patch_command[patch_command.index("--patch-json") + 1])
    assert patch == [
        {"op": "test", "path": "/metadata/uid", "value": "cm-uid"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "17"},
        {
            "op": "replace",
            "path": "/data/user_config.json",
            "value": result["data"]["user_config.json"],
        },
    ]
    assert json.loads(patch[-1]["value"]) == desired
    assert runtime == before
    assert result["metadata"] == before["metadata"]
    assert {k: v for k, v in result["data"].items() if k != "user_config.json"} == {
        k: v for k, v in before["data"].items() if k != "user_config.json"
    }


@pytest.mark.parametrize("target", ["rbg", "configmap"])
@pytest.mark.parametrize("label", [C.RBG_MANAGED_LABEL, C.RBG_GROUP_LABEL])
def test_foreign_resources_are_rejected_before_patch(monkeypatch, resources, target, label):
    rbg, runtime, desired = resources
    (rbg if target == "rbg" else runtime)["metadata"]["labels"][label] = "another-deployment"
    commands = command_responses(monkeypatch, [rbg, runtime])
    with pytest.raises(ValueError, match="not managed"):
        rbg_lifecycle.update_rbg_runtime_config(desired, json.loads(resources[1]["data"]["user_config.json"]))
    assert all("patch" not in command for command in commands)


@pytest.mark.parametrize("problem", ["identity", "baseline", "json"])
def test_invalid_runtime_identity_or_data_is_rejected(monkeypatch, resources, problem):
    rbg, runtime, desired = resources
    if problem == "identity":
        runtime["metadata"]["annotations"].pop(C.RBG_SERVICE_ID_ANNOTATION)
    elif problem == "baseline":
        baseline = json.loads(runtime["data"]["user_config.json"])
        baseline["motor_deploy_config"]["job_id"] = "foreign"
        runtime["data"]["user_config.json"] = json.dumps(baseline)
    else:
        runtime["data"]["user_config.json"] = "invalid JSON"
    commands = command_responses(monkeypatch, [rbg, runtime])
    with pytest.raises(ValueError):
        rbg_lifecycle.update_rbg_runtime_config(desired, {})
    assert len(commands) == 2


@pytest.mark.parametrize("stage", [0, 1])
def test_not_found_is_distinct_from_api_failure(monkeypatch, resources, stage):
    rbg, _, desired = resources
    command_responses(monkeypatch, [rbg] * stage + [None])
    with pytest.raises(FileNotFoundError):
        rbg_lifecycle.update_rbg_runtime_config(desired, json.loads(resources[1]["data"]["user_config.json"]))


@pytest.mark.parametrize("stage", [0, 1, 2])
def test_forbidden_or_conflict_propagates_without_retry(monkeypatch, resources, stage):
    rbg, runtime, desired = resources
    commands = []

    def run(command, **kwargs):
        commands.append(recorded_command(command))
        if len(commands) == stage + 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="Forbidden or resourceVersion conflict")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(rbg if len(commands) == 1 else runtime),
            stderr="",
        )

    monkeypatch.setattr(k8s_utils.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="Forbidden or resourceVersion conflict"):
        rbg_lifecycle.update_rbg_runtime_config(desired, json.loads(resources[1]["data"]["user_config.json"]))
    assert len(commands) == stage + 1


def test_request_timeout_propagates(monkeypatch, resources):
    _, _, desired = resources
    command_responses(monkeypatch, [subprocess.TimeoutExpired("kubectl", 60)])
    with pytest.raises(RuntimeError, match="timed out"):
        rbg_lifecycle.update_rbg_runtime_config(desired, json.loads(resources[1]["data"]["user_config.json"]))


@pytest.mark.parametrize("problem", ["uid", "resourceVersion", "deletionTimestamp"])
def test_wait_adapter_rejects_invalid_live_metadata_before_scale_read(monkeypatch, resources, problem):
    """An incomplete or deleting Adapter must not satisfy the HPA dependency."""
    rbg, _, _ = resources
    metadata = {
        "name": f"{rbg['metadata']['name']}-prefill",
        "namespace": rbg["metadata"]["namespace"],
        "uid": "adapter-uid",
        "resourceVersion": "1",
    }
    if problem == "deletionTimestamp":
        metadata[problem] = "2026-11-23T00:00:00Z"
    else:
        metadata.pop(problem)
    adapter = {
        "apiVersion": "workloads.x-k8s.io/v1alpha2",
        "kind": "RoleBasedGroupScalingAdapter",
        "metadata": metadata,
        "spec": {"scaleTargetRef": {"name": rbg["metadata"]["name"], "role": "prefill"}},
    }
    commands = command_responses(monkeypatch, [adapter])
    with pytest.raises(ValueError, match="deleting or lacks API identity/version"):
        rbg_lifecycle._wait_rbg_adapter(rbg, "prefill")
    assert len(commands) == 1
    assert "--raw" not in commands[0]


def test_unchanged_config_needs_no_write(monkeypatch, resources):
    rbg, runtime, _ = resources
    commands = command_responses(monkeypatch, [rbg, runtime])
    rbg_lifecycle.update_rbg_runtime_config(
        json.loads(runtime["data"]["user_config.json"]), json.loads(runtime["data"]["user_config.json"])
    )
    assert len(commands) == 2


def test_unexpected_patch_response_is_not_reported_as_success(monkeypatch, resources):
    rbg, runtime, desired = resources
    replaced = deepcopy(runtime)
    replaced["metadata"]["uid"] = "replacement-uid"
    command_responses(monkeypatch, [rbg, runtime, replaced])
    with pytest.raises(RuntimeError, match="replaced"):
        rbg_lifecycle.update_rbg_runtime_config(desired, json.loads(resources[1]["data"]["user_config.json"]))


@pytest.mark.parametrize("operation", ["create", "patch"])
@pytest.mark.parametrize("outcome", ["normalized", "timeout_applied", "timeout_unconfirmed", "foreign"])
def test_hpa_submission_accepts_api_quantities_but_checks_uncertain_writes(monkeypatch, resources, operation, outcome):
    group, _, config = resources
    desired = {
        "apiVersion": C.RBG_HPA_API_VERSION,
        "kind": C.RBG_HPA_KIND,
        "metadata": deepcopy(group["metadata"]),
        "spec": {"metrics": [{"external": {"target": {"type": "Value", "value": "0.8"}}}]},
    }
    reply = deepcopy(desired)
    if outcome != "timeout_applied":
        reply["spec"]["metrics"][0]["external"]["target"]["value"] = "800m"
    if outcome == "foreign":
        reply["metadata"]["namespace"] = "another-deployment"
    queries = iter([[], [reply]] if operation == "create" else [[reply]])
    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", lambda *a, **kw: next(queries))
    writes = []

    def run(command):
        writes.append(recorded_command(command))
        if outcome.startswith("timeout"):
            raise RuntimeError("request timed out")
        return json.dumps(reply)

    monkeypatch.setattr(rbg_lifecycle, "run_cmd_get_output", run)
    patch = rbg_lifecycle._rbg_patch_tests(desired) + [{"op": "replace", "path": "/spec", "value": desired["spec"]}]

    def submit():
        if operation == "create":
            return rbg_lifecycle._submit_rbg_object(desired, render.deployment_settings(config), "identity")
        return rbg_lifecycle._patch_rbg_object(C.RBG_HPA_RESOURCE, desired, patch)

    if outcome == "timeout_unconfirmed":
        with pytest.raises(RuntimeError, match="outcome (?:not confirmed|unconfirmed)"):
            submit()
    elif outcome == "foreign":
        with pytest.raises((ValueError, RuntimeError), match="identity|replaced"):
            submit()
    else:
        assert submit() == reply
    assert len(writes) == 1
    if operation == "patch":
        assert json.loads(writes[0][writes[0].index("--patch-json") + 1])[:2] == rbg_lifecycle._rbg_patch_tests(desired)


@pytest.fixture
def service_resources(resources):
    rbg, runtime, config = resources
    rbg["metadata"]["uid"] = "rbg-uid"
    bundle = generate_rbg_resources(config, {}, facts={"accelerator_type": "module-910b-8"})
    desired = next(s for s in bundle["services"] if s["spec"].get("type") == "NodePort")
    live = deepcopy(desired)
    live["metadata"].update(
        uid="svc-uid",
        resourceVersion="29",
        ownerReferences=[
            {
                "apiVersion": C.RBG_API_VERSION,
                "kind": "RoleBasedGroup",
                "name": rbg["metadata"]["name"],
                "uid": "rbg-uid",
                "controller": True,
            }
        ],
        finalizers=["external.example/protection"],
    )
    live["metadata"]["annotations"] = {"external.example/key": "keep", "motor-deployer/coordination-request": "keep"}
    live["spec"].update(
        clusterIP="10.0.0.8",
        clusterIPs=["10.0.0.8"],
        ipFamilies=["IPv4"],
        ipFamilyPolicy="SingleStack",
        internalTrafficPolicy="Cluster",
        externalTrafficPolicy="Cluster",
    )
    live["status"] = {"loadBalancer": {}}
    return rbg, runtime, config, desired, live


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("spec", "externalTrafficPolicy", "Local"),
        ("spec", "internalTrafficPolicy", "Local"),
        ("spec", "publishNotReadyAddresses", True),
        ("spec", "sessionAffinityConfig", {"clientIP": {"timeoutSeconds": 300}}),
        ("labels", "business.example/tier", "new"),
        ("annotations", "business.example/revision", "new"),
        ("ports", "targetPort", 9999),
        ("ports", "port", 1028),
        ("ports", "protocol", "UDP"),
        ("selector", "app", "new-business"),
        ("spec", "externalIPs", ["192.0.2.1"]),
    ],
)
def test_service_explicit_fields_are_patched_from_rendered_resources(
    monkeypatch, service_resources, section, key, value
):
    rbg, runtime, config, original, live = service_resources
    load = render.load_documents

    def templates(filename):
        docs = load(filename)
        template = next(d for d in docs if d["kind"] == "Service" and d["spec"].get("type") == "NodePort")
        if section in ("labels", "annotations"):
            template["metadata"].setdefault(section, {})[key] = value
        elif section == "ports":
            template["spec"]["ports"][0][key] = value
        elif section == "selector":
            template["spec"]["selector"][key] = value
        else:
            template["spec"][key] = value
            if key == "sessionAffinityConfig":
                template["spec"]["sessionAffinity"] = "ClientIP"
        return docs

    monkeypatch.setattr(render, "load_documents", templates)
    bundle = generate_rbg_resources(config, {}, facts={"accelerator_type": "module-910b-8"})
    desired = next(s for s in bundle["services"] if s["metadata"]["name"] == original["metadata"]["name"])
    result = deepcopy(live)
    if section in ("labels", "annotations"):
        result["metadata"].setdefault(section, {})[key] = value
        path = f"/metadata/{section}/" + key.replace("/", "~1")
    elif section == "ports":
        result["spec"]["ports"][0][key] = value
        path = "/spec/ports/0/" + key
    elif section == "selector":
        result["spec"]["selector"][key] = value
        path = "/spec/selector/" + key
    else:
        result["spec"][key] = value
        path = "/spec/" + key
        if key == "sessionAffinityConfig":
            result["spec"]["sessionAffinity"] = "ClientIP"
    result["metadata"]["labels"].update(desired["metadata"]["labels"])
    before = deepcopy((desired, live))
    commands = command_responses(monkeypatch, [rbg, runtime, live, {"items": [live]}, result])
    rbg_lifecycle._update_rbg_service(config, desired)
    patch = json.loads(commands[-1][commands[-1].index("--patch-json") + 1])
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "svc-uid"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "29"},
    ]
    assert any(op["path"] == path and op["value"] == value for op in patch[2:])
    assert all(op["op"] in ("add", "replace") for op in patch[2:])
    assert all(op["path"] not in ("/spec", "/metadata", "/spec/ports") for op in patch[2:])
    assert all(
        not any(
            word in op["path"]
            for word in ("clusterIP", "ipFamil", "nodePort", "ownerReferences", "finalizers", "coordination-request")
        )
        for op in patch[2:]
    )
    assert (desired, live) == before
    assert len(commands) == 5


def test_service_noop_preserves_allocations_external_metadata_and_omitted_fields(monkeypatch, service_resources):
    rbg, runtime, config, desired, live = service_resources
    live["metadata"].update(managedFields=[{"manager": "external"}], creationTimestamp="2026-01-01T00:00:00Z")
    live["spec"]["ports"][0]["appProtocol"] = "http"
    live["spec"]["publishNotReadyAddresses"] = True
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    rbg_lifecycle._update_rbg_service(config, desired)
    assert len(commands) == 3


def test_attachment_binding_keeps_complete_deployment_identity(monkeypatch, service_resources):
    rbg, runtime, config, desired, live = service_resources
    identity = runtime["metadata"]["annotations"][C.RBG_SERVICE_ID_ANNOTATION]
    live["metadata"]["annotations"][C.RBG_SERVICE_ID_ANNOTATION] = identity
    objects = {
        "services": live,
        "service": live,
        "configmap": runtime,
        "rolebasedgroups.workloads.x-k8s.io": rbg,
    }
    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", lambda resource, *a, **kw: [deepcopy(objects[resource])])
    monkeypatch.setattr(rbg_lifecycle, "_patch_rbg_object", lambda resource, obj, patch: obj)
    # Keep the real Service validation: a missing logical_service_name must fail here.
    result = rbg_lifecycle._bind_rbg_attachment(desired, rbg, identity, render.deployment_settings(config))
    assert result["metadata"]["uid"] == live["metadata"]["uid"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("clusterIP", "10.0.0.9"),
        ("clusterIP", "None"),
        ("clusterIPs", ["10.0.0.9"]),
        ("ipFamilies", ["IPv6"]),
        ("ipFamilyPolicy", "RequireDualStack"),
        ("type", "ExternalName"),
    ],
)
def test_service_address_and_type_transitions_fail_without_write(monkeypatch, service_resources, field, value):
    rbg, runtime, config, desired, live = service_resources
    desired["spec"][field] = value
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    with pytest.raises(RuntimeError, match="redeploy required"):
        rbg_lifecycle._update_rbg_service(config, desired)
    assert len(commands) == 3


@pytest.mark.parametrize("problem", ["owner", "uid", "deleting", "role", "labels", "selector", "identity", "native"])
def test_service_identity_guards_block_writes(monkeypatch, service_resources, problem):
    rbg, runtime, config, desired, live = service_resources
    if problem == "owner":
        live["metadata"]["ownerReferences"] = []
    elif problem == "uid":
        live["metadata"]["ownerReferences"][0]["uid"] = "replaced-rbg"
    elif problem == "deleting":
        live["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    elif problem == "role":
        live["metadata"]["labels"][C.RBG_ROLE_LABEL] = "foreign"
    elif problem == "labels":
        live["metadata"]["labels"][C.RBG_MANAGED_LABEL] = "foreign"
    elif problem == "selector":
        desired["spec"]["selector"][C.RBG_GROUP_LABEL] = "foreign"
    elif problem == "identity":
        runtime["metadata"]["annotations"].pop(C.RBG_SERVICE_ID_ANNOTATION)
    else:
        desired["metadata"]["name"] = "s-" + rbg["metadata"]["name"] + "-controller"
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    with pytest.raises(RuntimeError):
        rbg_lifecycle._update_rbg_service(config, desired)
    assert not any("patch" in c for c in commands)


@pytest.mark.parametrize("problem", ["missing", "forbidden", "conflict", "timeout", "json", "wrong-result"])
def test_service_api_failures_are_not_success(monkeypatch, service_resources, problem):
    rbg, runtime, config, desired, live = service_resources
    desired["spec"]["externalTrafficPolicy"] = "Local"
    responses = [rbg, runtime, live]
    if problem == "missing":
        responses[-1] = None
    elif problem == "json":
        responses[-1] = SimpleNamespace(returncode=0, stdout="{invalid-json", stderr="")
    elif problem == "wrong-result":
        replaced = deepcopy(live)
        replaced["metadata"]["uid"] = "replacement-uid"
        responses.extend([{"items": [live]}, replaced])
    elif problem == "timeout":
        responses.extend([{"items": [live]}, subprocess.TimeoutExpired("kubectl", 60), live])
    else:
        responses.append({"items": [live]})
        responses.append(
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Forbidden" if problem == "forbidden" else "UID/resourceVersion conflict",
            )
        )
    commands = command_responses(monkeypatch, responses)
    with pytest.raises(RuntimeError, match="Update Service/"):
        rbg_lifecycle._update_rbg_service(config, desired)
    assert len(commands) <= 6


def test_service_nested_affinity_change_preserves_other_fields(monkeypatch, service_resources):
    rbg, runtime, config, desired, live = service_resources
    desired["spec"].update(sessionAffinity="ClientIP", sessionAffinityConfig={"clientIP": {"timeoutSeconds": 300}})
    live["spec"].update(sessionAffinity="ClientIP", sessionAffinityConfig={"clientIP": {"timeoutSeconds": 100}})
    result = deepcopy(live)
    result["spec"]["sessionAffinityConfig"]["clientIP"]["timeoutSeconds"] = 300
    commands = command_responses(monkeypatch, [rbg, runtime, live, {"items": [live]}, result])
    rbg_lifecycle._update_rbg_service(config, desired)
    patch = json.loads(commands[-1][commands[-1].index("--patch-json") + 1])
    assert patch[2:] == [{"op": "replace", "path": "/spec/sessionAffinityConfig/clientIP/timeoutSeconds", "value": 300}]


@pytest.mark.parametrize("problem", ["nodeport", "null"])
def test_service_ambiguous_release_or_removal_is_rejected(monkeypatch, service_resources, problem):
    rbg, runtime, config, desired, live = service_resources
    if problem == "nodeport":
        desired["spec"]["ports"][0]["nodePort"] = 0
    else:
        desired["metadata"]["annotations"] = {"business.example/delete": None}
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    with pytest.raises(RuntimeError):
        rbg_lifecycle._update_rbg_service(config, desired)
    assert len(commands) == 3


def test_rendered_headless_service_and_native_defaults_need_no_patch(monkeypatch, service_resources):
    rbg, runtime, config, _, live = service_resources
    _, catalog, templates, _ = render._load_templates()
    roles = {"mf-store": catalog["mf-store"]}
    rbg["spec"]["roles"].append(catalog["mf-store"])
    services, _ = render.build_services(render.deployment_settings(config), roles, config, {}, templates)
    desired = services[0]
    live["metadata"].update(name=desired["metadata"]["name"], labels=deepcopy(desired["metadata"]["labels"]))
    live["spec"] = deepcopy(desired["spec"])
    live["spec"].pop("sessionAffinity")
    live["spec"]["ports"][0].pop("protocol")
    live["spec"]["ports"][0].pop("targetPort")
    live["spec"].update(clusterIPs=["None"], ipFamilies=["IPv4"])
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    rbg_lifecycle._update_rbg_service(config, desired)
    assert desired["spec"]["clusterIP"] == "None"
    assert len(commands) == 3


def test_rendering_preserves_online_nodeport_before_service_update(monkeypatch, service_resources):
    rbg, runtime, config, desired, live = service_resources
    live["spec"]["ports"][0]["nodePort"] = 32001
    bundle = generate_rbg_resources(
        config, {}, facts={"accelerator_type": "module-910b-8", "services": {live["metadata"]["name"]: live}}
    )
    desired = next(s for s in bundle["services"] if s["metadata"]["name"] == desired["metadata"]["name"])
    assert desired["spec"]["ports"][0]["nodePort"] == 32001
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    rbg_lifecycle._update_rbg_service(config, desired)
    assert len(commands) == 3


def test_service_explicit_default_false_is_noop(monkeypatch, service_resources):
    rbg, runtime, config, desired, live = service_resources
    desired["spec"]["publishNotReadyAddresses"] = False
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    rbg_lifecycle._update_rbg_service(config, desired)
    assert len(commands) == 3


@pytest.fixture
def deletion_resources(service_resources):
    rbg, runtime, config, _, service = service_resources
    rbg["metadata"]["resourceVersion"] = "10"
    bundle = generate_rbg_resources(config, {}, facts={"accelerator_type": "module-910b-8"})
    permissions = [
        obj for obj in bundle["prerequisites"] if obj["kind"] in {"ServiceAccount", "ClusterRole", "ClusterRoleBinding"}
    ]
    for index, obj in enumerate(permissions):
        obj["metadata"].update(uid=f"permission-{index}", resourceVersion="1")
    binding = next(obj for obj in permissions if obj["kind"] == "ClusterRoleBinding")
    preflight = [
        {"items": []},
        rbg,
        runtime,
        {"items": [service]},
        None,
        {"items": [binding]},
        {"items": []},
        {"items": []},
        {"items": []},
        *permissions,
    ]
    return config, rbg, runtime, service, permissions, preflight


def test_delete_without_hpa_uses_rendered_resources_and_atomic_uid_body(monkeypatch, deletion_resources):
    config, rbg, runtime, service, permissions, preflight = deletion_resources
    ordered = [rbg, service, *sorted(permissions, key=lambda obj: obj["kind"] != "ClusterRoleBinding"), runtime]
    responses = list(preflight)
    for index, obj in enumerate(ordered):
        responses.extend([obj, {"kind": "Status", "status": "Success"}, None])
        if index == 0:
            responses.append({"items": []})
    commands = command_responses(monkeypatch, responses)
    run = k8s_utils.subprocess.run
    bodies = []

    def inspect_delete(command, **kwargs):
        if "delete" in command:
            bodies.append(json.loads(Path(command[command.index("-f") + 1]).read_text(encoding="utf-8")))
        return run(command, check=kwargs.pop("check", False), **kwargs)

    monkeypatch.setattr(k8s_utils.subprocess, "run", inspect_delete)
    rbg_lifecycle.delete_rbg(config)
    assert [body["preconditions"] for body in bodies] == [
        {"uid": obj["metadata"]["uid"], "resourceVersion": obj["metadata"]["resourceVersion"]} for obj in ordered
    ]
    deletes = [c for c in commands if "delete" in c]
    assert len(deletes) == len(ordered)
    assert all("--raw" in c and "--all" not in c for c in deletes)
    assert deletes[0][deletes[0].index("--raw") + 1].endswith("/rolebasedgroups/" + rbg["metadata"]["name"])
    assert deletes[-1][deletes[-1].index("--raw") + 1].endswith("/configmaps/motor-config")
    assert all("persistentvolume" not in " ".join(c) for c in deletes)
    assert not any("apply" in c or "patch" in c for c in commands)


def test_delete_owned_hpa_precedes_rbg_without_scaling_generation(monkeypatch, deletion_resources):
    config, rbg, runtime, service, permissions, preflight = deletion_resources
    hpa = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {
            "name": "prefill-hpa",
            "namespace": rbg["metadata"]["namespace"],
            "uid": "hpa",
            "resourceVersion": "1",
            "labels": deepcopy(rbg["metadata"]["labels"]),
            "ownerReferences": [
                {
                    "apiVersion": rbg["apiVersion"],
                    "kind": "RoleBasedGroup",
                    "name": rbg["metadata"]["name"],
                    "uid": rbg["metadata"]["uid"],
                }
            ],
        },
        "spec": {
            "scaleTargetRef": {
                "apiVersion": rbg["apiVersion"],
                "kind": "RoleBasedGroupScalingAdapter",
                "name": rbg["metadata"]["name"] + "-prefill",
            }
        },
    }
    # Adapter already gone: the HPA's real owner and fixed target still prove deletion ownership.
    responses = [{"items": [hpa]}, rbg, None, *preflight[2:]]
    ordered = [hpa, rbg, service, *sorted(permissions, key=lambda obj: obj["kind"] != "ClusterRoleBinding"), runtime]
    for index, obj in enumerate(ordered):
        responses.extend([obj, {"kind": "Status"}, None])
        if index == 1:
            responses.append({"items": []})
    commands = command_responses(monkeypatch, responses)
    rbg_lifecycle.delete_rbg(config)
    deletes = [command for command in commands if "delete" in command]
    assert "/horizontalpodautoscalers/prefill-hpa" in deletes[0][deletes[0].index("--raw") + 1]
    assert "/rolebasedgroups/" in deletes[1][deletes[1].index("--raw") + 1]


@pytest.mark.parametrize("problem", ["hpa", "identity", "foreign", "owner", "shared-role", "shared-account", "pvc-gc"])
def test_delete_preflight_blocks_unsafe_candidates_without_writes(monkeypatch, deletion_resources, problem):
    config, _, runtime, service, permissions, responses = deletion_resources
    if problem == "hpa":
        responses[0] = {"items": [{"kind": "HorizontalPodAutoscaler", "metadata": {"name": "vllm-prefill-hpa"}}]}
    elif problem == "identity":
        runtime["metadata"]["annotations"].pop(C.RBG_SERVICE_ID_ANNOTATION)
    elif problem == "foreign":
        service["metadata"]["labels"][C.RBG_MANAGED_LABEL] = "external"
    elif problem == "owner":
        service["metadata"]["ownerReferences"][0]["uid"] = "foreign-uid"
    elif problem == "shared-role":
        role = next(obj for obj in permissions if obj["kind"] == "ClusterRole")
        responses[6] = {
            "items": [
                {
                    "metadata": {
                        "name": next(obj for obj in permissions if obj["kind"] == "ClusterRoleBinding")["metadata"][
                            "name"
                        ]
                    },
                    "roleRef": {
                        "apiGroup": "rbac.authorization.k8s.io",
                        "kind": "ClusterRole",
                        "name": role["metadata"]["name"],
                    },
                }
            ]
        }
    elif problem == "pvc-gc":
        responses[8] = {
            "items": [
                {
                    "metadata": {
                        "name": "data",
                        "ownerReferences": [
                            {"apiVersion": C.RBG_API_VERSION, "kind": "RoleBasedGroup", "name": "vllm"}
                        ],
                    }
                }
            ]
        }
    else:
        sa = next(obj for obj in permissions if obj["kind"] == "ServiceAccount")
        responses[7] = {"items": [{"metadata": {"labels": {}}, "spec": {"serviceAccountName": sa["metadata"]["name"]}}]}
    commands = command_responses(monkeypatch, responses)
    with pytest.raises((RuntimeError, ValueError)):
        rbg_lifecycle.delete_rbg(config)
    assert not any("delete" in c for c in commands)


@pytest.mark.parametrize("problem", ["forbidden", "json", "timeout"])
def test_delete_query_failure_never_becomes_absence(monkeypatch, resources, problem):
    _, _, config = resources
    failure = {
        "forbidden": SimpleNamespace(returncode=1, stdout="", stderr="Forbidden"),
        "json": SimpleNamespace(returncode=0, stdout="invalid", stderr=""),
        "timeout": subprocess.TimeoutExpired("kubectl", 60),
    }[problem]
    commands = command_responses(monkeypatch, [failure])
    with pytest.raises((RuntimeError, ValueError)):
        rbg_lifecycle.delete_rbg(config)
    assert len(commands) == 1


@pytest.mark.parametrize("problem", ["absent", "replacement", "conflict", "timeout", "terminating"])
def test_uid_delete_wait_and_failure_semantics(monkeypatch, service_resources, problem):
    rbg, _, _, _, _ = service_resources
    rbg["metadata"]["resourceVersion"] = "10"
    responses = [rbg]
    if problem == "absent":
        responses = [None]
    elif problem == "replacement":
        replacement = deepcopy(rbg)
        replacement["metadata"]["uid"] = "new-uid"
        responses = [replacement]
    elif problem == "conflict":
        responses.append(SimpleNamespace(returncode=1, stdout="", stderr="Conflict: UID precondition failed"))
    elif problem == "timeout":
        responses.extend([subprocess.TimeoutExpired("kubectl", 60), rbg])
    else:
        responses.extend([{"kind": "Status"}, rbg])
        ticks = iter([0, 121])
        monkeypatch.setattr(rbg_lifecycle.time, "monotonic", lambda: next(ticks))
    commands = command_responses(monkeypatch, responses)
    if problem == "absent":
        rbg_lifecycle._delete_with_uid("rolebasedgroups.workloads.x-k8s.io", "test-motor", "/unused", rbg)
        assert len(commands) == 1
    else:
        with pytest.raises(RuntimeError):
            rbg_lifecycle._delete_with_uid("rolebasedgroups.workloads.x-k8s.io", "test-motor", "/unused", rbg)


def test_delete_remaining_pods_preserve_config_and_permissions(monkeypatch, deletion_resources):
    config, rbg, _, _, _, responses = deletion_resources
    responses.extend([rbg, {"kind": "Status"}, None, {"items": [{"kind": "Pod"}]}])
    ticks = iter([0, 0, 0, 0, 121])
    monkeypatch.setattr(rbg_lifecycle.time, "monotonic", lambda: next(ticks))
    commands = command_responses(monkeypatch, responses)
    with pytest.raises(RuntimeError, match="preserving runtime"):
        rbg_lifecycle.delete_rbg(config)
    assert len([c for c in commands if "delete" in c]) == 1


def test_delete_after_rbg_gone_cleans_verified_residuals(monkeypatch, deletion_resources):
    config, _, runtime, service, _, responses = deletion_resources
    responses[1] = None
    responses[-3:] = [None, None, None]
    responses.extend([{"items": []}, service, {"kind": "Status"}, None, runtime, {"kind": "Status"}, None])
    commands = command_responses(monkeypatch, responses)
    rbg_lifecycle.delete_rbg(config)
    deletes = [c for c in commands if "delete" in c]
    assert len(deletes) == 2
    assert not any("/rolebasedgroups/" in " ".join(c) for c in deletes)
    assert "/configmaps/motor-config" in " ".join(deletes[-1])


def test_delete_already_empty_deployment_is_idempotent(monkeypatch, deletion_resources):
    config, _, _, _, _, _ = deletion_resources
    responses = [
        {"items": []},
        None,
        None,
        {"items": []},
        None,
        {"items": []},
        {"items": []},
        {"items": []},
        {"items": []},
        None,
        None,
        None,
        {"items": []},
    ]
    commands = command_responses(monkeypatch, responses)
    rbg_lifecycle.delete_rbg(config)
    assert not any("delete" in c for c in commands)


def test_runtime_format_only_change_does_not_write(monkeypatch, resources):
    rbg, runtime, _ = resources
    config = json.loads(runtime["data"]["user_config.json"])
    runtime["data"]["user_config.json"] = json.dumps(config)
    commands = command_responses(monkeypatch, [rbg, runtime])
    rbg_lifecycle.update_rbg_runtime_config(config, json.loads(resources[1]["data"]["user_config.json"]))
    assert len(commands) == 2


@pytest.mark.parametrize("problem", ["kind", "apiVersion", "deletionTimestamp", "uid", "resourceVersion"])
def test_runtime_rejects_invalid_object_before_write(monkeypatch, resources, problem):
    rbg, runtime, config = resources
    if problem in {"kind", "apiVersion"}:
        runtime[problem] = "wrong"
    elif problem == "deletionTimestamp":
        runtime["metadata"][problem] = "2026-01-01T00:00:00Z"
    else:
        runtime["metadata"].pop(problem)
    commands = command_responses(monkeypatch, [rbg, runtime])
    with pytest.raises(ValueError):
        rbg_lifecycle.update_rbg_runtime_config(config, json.loads(resources[1]["data"]["user_config.json"]))
    assert len(commands) == 2


@pytest.mark.parametrize("entry", [None, "config", "instances"])
@pytest.mark.parametrize("failure", [None, "json", "missing-data", "forbidden", "timeout"])
def test_baseline_and_real_update_entries_distinguish_absence_from_failure(monkeypatch, resources, failure, entry):
    _, runtime, _ = resources
    response = runtime
    if failure is None:
        response = None
    elif failure == "json":
        response = SimpleNamespace(returncode=0, stdout="invalid", stderr="")
    elif failure == "missing-data":
        response = {**runtime, "data": {}}
    elif failure == "forbidden":
        response = SimpleNamespace(returncode=1, stdout="", stderr="Forbidden")
    elif failure == "timeout":
        response = RuntimeError("Command timed out")
    commands = command_responses(monkeypatch, [response])

    def read():
        if entry == "config":
            return deploy.handle_update_config(resources[2])
        if entry == "instances":
            return deploy.handle_update_instance_num(resources[2])
        return rbg_lifecycle.get_rbg_baseline_config(resources[2])

    if failure is None and entry is None:
        assert read() is None
    elif failure is None:
        with pytest.raises(FileNotFoundError):
            read()
    else:
        with pytest.raises((RuntimeError, ValueError)):
            read()
    assert len(commands) == 1
    assert "--ignore-not-found=true" in commands[0]


@pytest.mark.parametrize("omitted", ["one-port", "all-ports", "ports-field", "nodeport", "port-name", "selector"])
def test_service_omissions_preserve_ports_allocations_and_fields(monkeypatch, service_resources, omitted):
    rbg, runtime, config, desired, live = service_resources
    first = live["spec"]["ports"][0]
    first["name"] = "management"
    first["appProtocol"] = "http"
    live["spec"]["ports"].append({"name": "metrics", "port": 9090, "targetPort": 9090, "nodePort": 32090})
    live["spec"]["selector"]["example.com/zone"] = "a"
    desired["spec"]["ports"] = [deepcopy(first)]
    desired["spec"]["ports"][0].pop("appProtocol")
    if omitted == "all-ports":
        desired["spec"]["ports"] = []
    elif omitted == "ports-field":
        desired["spec"].pop("ports")
    elif omitted == "nodeport":
        desired["spec"]["ports"][0].pop("nodePort")
    elif omitted == "port-name":
        desired["spec"]["ports"][0].pop("name")
    elif omitted == "selector":
        desired["spec"].pop("selector")
    desired["metadata"]["annotations"] = {"example.com/update": "new"}
    result = deepcopy(live)
    result["metadata"].setdefault("annotations", {}).update(desired["metadata"]["annotations"])
    commands = command_responses(monkeypatch, [rbg, runtime, live, {"items": [live]}, result])
    rbg_lifecycle._update_rbg_service(config, desired)
    patch = json.loads(commands[-1][commands[-1].index("--patch-json") + 1])
    assert not any(p["path"].startswith(("/spec/ports", "/spec/selector")) for p in patch)


def test_service_named_port_reordering_keeps_allocations(monkeypatch, service_resources):
    rbg, runtime, config, _, live = service_resources
    load = render.load_documents

    def templates(filename):
        docs = load(filename)
        svc = next(d for d in docs if d["kind"] == "Service" and d["spec"].get("type") == "NodePort")
        svc["spec"]["ports"] = [
            {"name": "one", "port": 1027, "targetPort": 1027, "nodePort": 31027},
            {"name": "two", "port": 1028, "targetPort": 1028, "nodePort": 31028},
        ]
        return docs

    monkeypatch.setattr(render, "load_documents", templates)
    bundle = generate_rbg_resources(config, {}, facts={"accelerator_type": "module-910b-8"})
    desired = next(s for s in bundle["services"] if s["metadata"]["name"] == live["metadata"]["name"])
    live["spec"]["ports"] = deepcopy(desired["spec"]["ports"])
    desired["spec"]["ports"].reverse()
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    rbg_lifecycle._update_rbg_service(config, desired)
    assert len(commands) == 3


@pytest.mark.parametrize("lb_class", [None, "example.com/balancer"])
def test_service_nodeport_to_loadbalancer_retains_ports(monkeypatch, service_resources, lb_class):
    rbg, runtime, config, original, live = service_resources
    load = render.load_documents

    def templates(filename):
        docs = load(filename)
        next(d for d in docs if d["kind"] == "Service" and d["spec"].get("type") == "NodePort")["spec"]["type"] = (
            "LoadBalancer"
        )
        if lb_class:
            next(d for d in docs if d["kind"] == "Service" and d["spec"].get("type") == "LoadBalancer")["spec"][
                "loadBalancerClass"
            ] = lb_class
        return docs

    monkeypatch.setattr(render, "load_documents", templates)
    desired = next(
        s
        for s in generate_rbg_resources(config, {}, facts={"accelerator_type": "module-910b-8"})["services"]
        if s["metadata"]["name"] == original["metadata"]["name"]
    )
    result = deepcopy(live)
    result["spec"]["type"] = "LoadBalancer"
    if lb_class:
        result["spec"]["loadBalancerClass"] = lb_class
    commands = command_responses(monkeypatch, [rbg, runtime, live, {"items": [live]}, result])
    rbg_lifecycle._update_rbg_service(config, desired)
    patch = json.loads(commands[-1][commands[-1].index("--patch-json") + 1])
    assert {"op": "replace", "path": "/spec/type", "value": "LoadBalancer"} in patch[2:]
    if lb_class:
        assert {"op": "add", "path": "/spec/loadBalancerClass", "value": lb_class} in patch[2:]
    assert all("nodePort" not in op["path"] for op in patch[2:])


@pytest.mark.parametrize(
    "problem",
    [
        "ok",
        "collision",
        "duplicate",
        "invalid",
        "boolean-plan",
        "boolean-live",
        "query",
        "json",
        "replacement",
        "version",
        "timeout",
        "executable",
    ],
)
def test_rbg_nodeport_checks_actual_plan_without_rewrite(monkeypatch, service_resources, problem):
    from lib.nodeport_allocator import check_rbg_nodeports

    _, _, _, desired, live = service_resources
    plan = [desired]
    online = deepcopy(live)
    response = {"items": [online]}
    if problem == "collision":
        online["metadata"]["name"] = "external"
    elif problem == "replacement":
        online["metadata"]["uid"] = "replacement"
    elif problem == "version":
        online["metadata"]["resourceVersion"] = "changed"
    elif problem == "timeout":
        response = subprocess.TimeoutExpired("kubectl", 60)
    elif problem == "executable":
        response = FileNotFoundError("kubectl missing")
    elif problem == "duplicate":
        second = deepcopy(desired)
        second["metadata"]["name"] = "second"
        plan.append(second)
    elif problem == "invalid":
        desired["spec"]["ports"][0]["nodePort"] = "31027"
    elif problem == "boolean-plan":
        desired["spec"]["ports"][0]["nodePort"] = True
    elif problem == "boolean-live":
        online["spec"]["ports"][0]["nodePort"] = True
    elif problem == "query":
        response = SimpleNamespace(returncode=1, stdout="", stderr="Forbidden")
    elif problem == "json":
        response = {"items": {}}
    before = deepcopy(plan)
    commands = command_responses(monkeypatch, [response])
    if problem == "ok":
        check_rbg_nodeports(plan, current={live["metadata"]["name"]: live})
    else:
        with pytest.raises((RuntimeError, ValueError)):
            check_rbg_nodeports(plan, current={live["metadata"]["name"]: live})
    assert plan == before
    assert all("get" in c and "patch" not in c for c in commands)


def test_rbg_nodeport_swap_is_rejected_before_api_writes(monkeypatch, service_resources):
    from lib.nodeport_allocator import check_rbg_nodeports

    _, _, _, desired, live = service_resources
    second = deepcopy(live)
    second["metadata"].update(name="second", uid="second-uid")
    second["spec"]["ports"][0]["nodePort"] = 31028
    plan = [deepcopy(desired), deepcopy(second)]
    plan[0]["spec"]["ports"][0]["nodePort"] = 31028
    plan[1]["spec"]["ports"][0]["nodePort"] = live["spec"]["ports"][0]["nodePort"]
    commands = command_responses(monkeypatch, [{"items": [live, second]}])
    with pytest.raises(RuntimeError, match="occupied"):
        check_rbg_nodeports(plan, current={s["metadata"]["name"]: s for s in (live, second)})
    assert len(commands) == 1 and "get" in commands[0]


@pytest.mark.parametrize("error", ["not-found", "timeout", "forbidden"])
def test_delete_confirms_uncertain_result_without_retry(monkeypatch, service_resources, error):
    rbg, _, _, _, _ = service_resources
    failure = (
        subprocess.TimeoutExpired("kubectl", 60)
        if error == "timeout"
        else SimpleNamespace(
            returncode=1, stdout="", stderr="Error from server (NotFound)" if error == "not-found" else "Forbidden"
        )
    )
    commands = command_responses(monkeypatch, [rbg, failure, None])
    if error == "forbidden":
        with pytest.raises(RuntimeError, match="Forbidden"):
            rbg_lifecycle._delete_with_uid("rolebasedgroups.workloads.x-k8s.io", "test-motor", "/unused", rbg)
        assert len(commands) == 2
    else:
        rbg_lifecycle._delete_with_uid("rolebasedgroups.workloads.x-k8s.io", "test-motor", "/unused", rbg)
        assert len(commands) == 3
    assert len([c for c in commands if "delete" in c]) == 1


@pytest.mark.parametrize("target_kind", ["Deployment", "StatefulSet", C.RBG_SCALING_ADAPTER_KIND])
def test_delete_ignores_unrelated_hpa_without_querying_its_target(monkeypatch, deletion_resources, target_kind):
    config, rbg, runtime, service, permissions, responses = deletion_resources
    responses[0] = {
        "items": [
            {
                "metadata": {"name": "external"},
                "spec": {
                    "scaleTargetRef": {
                        "apiVersion": C.RBG_API_VERSION if target_kind == C.RBG_SCALING_ADAPTER_KIND else "apps/v1",
                        "kind": target_kind,
                        "name": "external",
                    }
                },
            }
        ]
    }
    responses[8] = {
        "items": [
            {
                "metadata": {
                    "name": "other-data",
                    "ownerReferences": [
                        {"apiVersion": C.RBG_API_VERSION, "kind": "RoleBasedGroup", "name": "other", "uid": "other-uid"}
                    ],
                }
            }
        ]
    }
    ordered = [rbg, service, *sorted(permissions, key=lambda o: o["kind"] != "ClusterRoleBinding"), runtime]
    for index, obj in enumerate(ordered):
        responses.extend([obj, {"kind": "Status"}, None])
        if index == 0:
            responses.append({"items": []})
    commands = command_responses(monkeypatch, responses)
    rbg_lifecycle.delete_rbg(config)
    assert not any("external" in command for command in commands)
    assert all(
        "/horizontalpodautoscalers/" not in " ".join(c) and "/persistentvolumeclaims/" not in " ".join(c)
        for c in commands
        if "delete" in c
    )


@pytest.mark.parametrize("association", ["label", "owner", "target", "name"])
def test_delete_rejects_related_hpa_with_invalid_ownership(monkeypatch, deletion_resources, association):
    config, rbg, _, _, _, responses = deletion_resources
    name = rbg["metadata"]["name"]
    hpa = {
        "metadata": {"name": "external"},
        "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "missing"}},
    }
    if association == "label":
        hpa["metadata"]["labels"] = {C.RBG_GROUP_LABEL: name}
    elif association == "owner":
        hpa["metadata"]["ownerReferences"] = [{"kind": C.RBG_KIND, "name": name, "uid": "wrong"}]
    elif association == "target":
        hpa["spec"]["scaleTargetRef"]["name"] = f"{name}-prefill"
    else:
        hpa["metadata"]["name"] = f"{name}-prefill-hpa"
    responses[0] = {"items": [hpa]}
    commands = command_responses(monkeypatch, responses)
    with pytest.raises(RuntimeError, match="ownership is ambiguous"):
        rbg_lifecycle.delete_rbg(config)
    assert not any("delete" in command for command in commands)


@pytest.mark.parametrize("problem", ["success", "relative-workdir", "hpa", "missing", "invalid", "namespace"])
def test_real_cleanup_reaches_python_and_propagates_failure(tmp_path, deletion_resources, problem):
    import os
    import shutil

    _config, rbg, runtime, service, permissions, responses = deletion_resources
    root = Path(__file__).resolve().parents[4]
    bash = shutil.which("bash") or "C:/Program Files/Git/bin/bash.exe"
    if not Path(bash).is_file():
        pytest.skip("bash unavailable")
    first = deepcopy(runtime)
    if problem == "missing":
        first = None
    elif problem == "invalid":
        first["data"]["user_config.json"] = "invalid"
    elif problem == "namespace":
        cfg = json.loads(first["data"]["user_config.json"])
        cfg["motor_deploy_config"]["job_id"] = "foreign"
        first["data"]["user_config.json"] = json.dumps(cfg)
    elif problem == "hpa":
        responses[0] = {"items": [{"metadata": {"name": "vllm-prefill-hpa"}}]}
    ordered = [rbg, service, *sorted(permissions, key=lambda o: o["kind"] != "ClusterRoleBinding"), runtime]
    for index, obj in enumerate(ordered):
        responses.extend([obj, {"kind": "Status"}, None])
        if index == 0:
            responses.append({"items": []})
    (tmp_path / "responses.json").write_text(json.dumps([first, *responses]), encoding="utf-8")
    # Child Python executes the real imports and deletion function. Only kubectl I/O is substituted.
    (tmp_path / "sitecustomize.py").write_text(
        "import subprocess,json,os\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
        "folder=Path(os.environ['CLEANUP_TEST_IO'])\nresponses=iter(json.loads((folder/'responses.json').read_text()))\n"
        "original=subprocess.run\n"
        "def run(command, **kwargs):\n"
        " if command[0]!='kubectl': return original(command, **kwargs)\n"
        " with (folder/'commands.jsonl').open('a') as f: f.write(json.dumps(command)+'\\n')\n"
        " value=next(responses)\n return SimpleNamespace(returncode=0,stdout=json.dumps(value) if value is not None else '',stderr='')\n"
        "subprocess.run=run\n",
        encoding="utf-8",
    )
    fake = tmp_path / "kubectl"
    fake.write_text('#!/bin/bash\necho "$*" >> "$CLEANUP_TEST_IO/legacy.log"\nexit 0\n', encoding="utf-8")
    fake.chmod(0o700)
    env = {
        **os.environ,
        "WORKDIR": "examples/deployer" if problem == "relative-workdir" else str(root / "examples/deployer"),
        "NAMESPACE": "test-motor",
        "MOTOR_DEPLOY_MODE": "rbg",
        "PYTHONPATH": str(tmp_path),
        "CLEANUP_TEST_IO": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    script = (root / "examples/cloud_native_deploy/cleanup.sh").as_posix()
    result = subprocess.run(
        [
            bash,
            "-c",
            'python3() { python "$@"; }; export -f python3; export PATH="$(cygpath -u "$CLEANUP_TEST_IO" 2>/dev/null || printf %s "$CLEANUP_TEST_IO"):$PATH"; bash "$1" "$NAMESPACE"',
            "cleanup-test",
            script,
        ],
        check=False,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if problem in {"success", "relative-workdir", "missing"}:
        assert result.returncode == 0, result.stderr
        if problem in {"success", "relative-workdir"}:
            commands = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text().splitlines()]
            assert any("--raw" in c for c in commands)
            assert not (tmp_path / "legacy.log").exists()
        else:
            assert not (tmp_path / "legacy.log").exists()
    else:
        assert result.returncode != 0
        assert "cleanup completed" not in result.stdout.lower()
        assert not (tmp_path / "legacy.log").exists()


def test_cleanup_rbac_covers_actual_reads_and_deletes():
    import yaml

    root = Path(__file__).resolve().parents[4] / "examples/cloud_native_deploy"
    native = yaml.safe_load((root / "k8s/clusterrole.yaml").read_text(encoding="utf-8"))["rules"]
    helm = (root / "helm/templates/rbac.yaml.tpl").read_text(encoding="utf-8")
    block = helm.split("rules:\n", 1)[1].split("\n---", 1)[0]
    assert yaml.safe_load(block) == native
    needed = {
        ("workloads.x-k8s.io", "rolebasedgroups"): {"get", "list", "patch", "delete"},
        ("workloads.x-k8s.io", "coordinatedpolicies"): {"get", "delete"},
        ("workloads.x-k8s.io", "roleinstances"): {"get", "list"},
        ("workloads.x-k8s.io", "roleinstancesets"): {"get", "list"},
        ("workloads.x-k8s.io", "rolebasedgroupscalingadapters"): {"list"},
        ("autoscaling", "horizontalpodautoscalers"): {"list"},
        ("", "pods"): {"list"},
        ("", "persistentvolumeclaims"): {"list"},
        ("", "services"): {"get", "list", "patch", "delete"},
        ("", "configmaps"): {"get", "patch", "delete"},
        ("rbac.authorization.k8s.io", "rolebindings"): {"list"},
        ("rbac.authorization.k8s.io", "clusterrolebindings"): {"get", "list", "delete"},
        ("apps", "deployments"): {"get"},
        ("apps", "statefulsets"): {"get"},
    }
    for (group, resource), verbs in needed.items():
        actual = {
            v for rule in native if group in rule["apiGroups"] and resource in rule["resources"] for v in rule["verbs"]
        }
        assert verbs <= actual, (group, resource, verbs - actual)
    assert all("*" not in rule["resources"] and "*" not in rule["verbs"] for rule in native)
    assert not any("persistentvolumeclaims" in r["resources"] and "delete" in r["verbs"] for r in native)


def test_delete_pvc_owner_chain_proves_foreign_rbg(monkeypatch, deletion_resources):
    config, rbg, runtime, service, permissions, responses = deletion_resources
    responses[8] = {
        "items": [
            {
                "metadata": {
                    "name": "other-data",
                    "ownerReferences": [
                        {"apiVersion": C.RBG_API_VERSION, "kind": "RoleInstance", "name": "other-ri", "uid": "ri-uid"}
                    ],
                }
            }
        ]
    }
    responses[9:9] = [
        {
            "metadata": {
                "name": "other-ri",
                "namespace": "test-motor",
                "uid": "ri-uid",
                "ownerReferences": [
                    {"apiVersion": C.RBG_API_VERSION, "kind": "RoleInstanceSet", "name": "other-ris", "uid": "ris-uid"}
                ],
            }
        },
        {
            "metadata": {
                "name": "other-ris",
                "namespace": "test-motor",
                "uid": "ris-uid",
                "ownerReferences": [
                    {"apiVersion": C.RBG_API_VERSION, "kind": "RoleBasedGroup", "name": "other", "uid": "other-uid"}
                ],
            }
        },
    ]
    ordered = [rbg, service, *sorted(permissions, key=lambda o: o["kind"] != "ClusterRoleBinding"), runtime]
    for index, obj in enumerate(ordered):
        responses.extend([obj, {"kind": "Status"}, None])
        if index == 0:
            responses.append({"items": []})
    commands = command_responses(monkeypatch, responses)
    rbg_lifecycle.delete_rbg(config)
    assert all("/persistentvolumeclaims/" not in " ".join(c) for c in commands if "delete" in c)


def test_affinity_disable_requires_removal_alignment_before_write(monkeypatch, service_resources):
    rbg, runtime, config, desired, live = service_resources
    live["spec"].update(sessionAffinity="ClientIP", sessionAffinityConfig={"clientIP": {"timeoutSeconds": 300}})
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    with pytest.raises(RuntimeError, match="align explicit removal intent"):
        rbg_lifecycle._update_rbg_service(config, desired)
    assert len(commands) == 3


@pytest.mark.parametrize("new_class", ["example.com/new", "example.com/first"])
def test_existing_loadbalancer_class_is_immutable(monkeypatch, service_resources, new_class):
    rbg, runtime, config, desired, live = service_resources
    live["spec"]["type"] = desired["spec"]["type"] = "LoadBalancer"
    if new_class.endswith("new"):
        live["spec"]["loadBalancerClass"] = "example.com/old"
    desired["spec"]["loadBalancerClass"] = new_class
    commands = command_responses(monkeypatch, [rbg, runtime, live])
    with pytest.raises(RuntimeError, match="loadBalancerClass"):
        rbg_lifecycle._update_rbg_service(config, desired)
    assert not any("patch" in c for c in commands)


@pytest.mark.parametrize("role_kind", ["Role", "ClusterRole"])
def test_delete_distinguishes_same_name_role_reference(monkeypatch, deletion_resources, role_kind):
    config, rbg, runtime, service, permissions, responses = deletion_resources
    role = next(p for p in permissions if p["kind"] == "ClusterRole")
    responses[6] = {
        "items": [
            {
                "metadata": {"name": "other-binding"},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": role_kind,
                    "name": role["metadata"]["name"],
                },
            }
        ]
    }
    if role_kind == "ClusterRole":
        commands = command_responses(monkeypatch, responses)
        with pytest.raises(ValueError, match="shared"):
            rbg_lifecycle.delete_rbg(config)
        assert not any("delete" in c for c in commands)
    else:
        ordered = [rbg, service, *sorted(permissions, key=lambda p: p["kind"] != "ClusterRoleBinding"), runtime]
        for index, obj in enumerate(ordered):
            responses.extend([obj, {"kind": "Status"}, None])
            if index == 0:
                responses.append({"items": []})
        commands = command_responses(monkeypatch, responses)
        rbg_lifecycle.delete_rbg(config)
        assert any("--raw" in c for c in commands)


@pytest.mark.parametrize("success", [True, False])
def test_tui_delete_calls_lifecycle_and_preserves_state_on_failure(monkeypatch, deletion_resources, success):
    from lib.tui.actions import _DeployActionsMixin

    config, rbg, runtime, service, permissions, responses = deletion_resources
    if success:
        ordered = [rbg, service, *sorted(permissions, key=lambda p: p["kind"] != "ClusterRoleBinding"), runtime]
        for index, obj in enumerate(ordered):
            responses.extend([obj, {"kind": "Status"}, None])
            if index == 0:
                responses.append({"items": []})
    else:
        responses = [SimpleNamespace(returncode=1, stdout="", stderr="Forbidden")]
    commands = command_responses(monkeypatch, responses)
    statuses, stopped = [], []
    session = SimpleNamespace(
        user_config=config,
        namespace="test-motor",
        pod_cnt=1,
        _deployed=True,
        _progress_active=True,
        _log_running=True,
        _menu_selected=2,
        _set_status=lambda *args: statuses.append(args),
        _stop_progress_monitor=lambda: stopped.append("progress"),
        _stop_pod_watcher=lambda: stopped.append("watcher"),
    )
    session._reset_after_delete = lambda: _DeployActionsMixin._reset_after_delete(session)
    _DeployActionsMixin._exec_delete(session, "test-motor")
    assert session._deployed is not success
    if success:
        assert stopped == ["progress", "watcher"]
        assert session.namespace == "" and session.user_config == {} and session.pod_cnt == 0
        assert not session._progress_active and not session._log_running
        assert any("--raw" in c for c in commands)
    else:
        assert not stopped
        assert session.namespace == "test-motor" and session.user_config == config and session.pod_cnt == 1
        assert "Forbidden" in statuses[0][0]
        assert not any("delete" in c for c in commands)


@pytest.mark.parametrize("source", ["job", "helm"])
def test_job_and_helm_examples_generate_deployment_and_scaling_resources(source):
    import yaml
    from lib.rbg.scaling import generate_scaling_resources

    root = Path(__file__).resolve().parents[4] / "examples/cloud_native_deploy"
    if source == "job":
        document = yaml.safe_load((root / "k8s/configmap-user-config-rbg.yaml").read_text(encoding="utf-8"))
        config = json.loads(document["data"]["user_config.json"])
    else:
        config = json.loads(yaml.safe_load((root / "helm/values-rbg.yaml").read_text(encoding="utf-8"))["userConfig"])
    config["motor_deploy_config"]["image_name"] = "motor:test"
    scenarios = {
        'enable': {
            'scaling_policy': {
                'prefill': {
                    'min_replicas': 1,
                    'max_replicas': 4,
                    'metric': 'motor_prefill_utilization',
                    'target': 0.8,
                    'target_type': 'Value',
                }
            }
        },
        'update': {
            'scaling_policy': {
                'prefill': {
                    'min_replicas': 1,
                    'max_replicas': 6,
                    'metric': 'motor_prefill_utilization',
                    'target': 0.7,
                    'target_type': 'Value',
                }
            }
        },
        'disable': {'scaling_policy': {}},
        'preserve': {},
    }
    bundle = generate_rbg_resources(config, {}, facts={"accelerator_type": "module-910c-8"})
    current = generate_scaling_resources({**config, **scenarios["enable"]}, bundle["rbg"])
    for intent, fragment in scenarios.items():
        scaling = generate_scaling_resources(
            {**config, **fragment},
            bundle["rbg"],
            is_update=True,
            current=current,
        )
        assert scaling["policy"] is None
        if intent == "preserve":
            assert scaling["hpas"] is None
        elif intent == "disable":
            assert scaling["hpas"] == {}
        else:
            hpa = scaling["hpas"]["prefill"]
            assert hpa["spec"]["maxReplicas"] == fragment["scaling_policy"]["prefill"]["max_replicas"]
            assert hpa["spec"]["scaleTargetRef"]["kind"] == "RoleBasedGroupScalingAdapter"


@pytest.mark.parametrize("kind", ["policy", "hpa"])
def test_scaling_resource_updates_patch_native_spec_with_uid_guard(monkeypatch, resources, kind):
    from lib.rbg.scaling import generate_scaling_resources

    group, _, config = deepcopy(resources)
    config["scaling_policy"] = {"prefill": {"max_replicas": 2}}
    first = generate_scaling_resources(config, group)
    config["scaling_policy"]["prefill"]["max_replicas"] = 4
    config["motor_deploy_config"]["rbg_config"] = {
        "coordinated_policy": {
            "policies": [
                {
                    "name": "new-policy",
                    "roles": ["prefill", "decode"],
                    "strategy": {"scaling": {"maxSkew": "100%", "progression": "OrderReady"}},
                }
            ]
        }
    }
    second = generate_scaling_resources(config, group, is_update=True, current=first)
    old = deepcopy(first["policy"] if kind == "policy" else first["hpas"]["prefill"])
    new = deepcopy(second["policy"] if kind == "policy" else second["hpas"]["prefill"])
    identity = "test-deployment-identity"
    for obj in (old, new):
        obj["metadata"].setdefault("annotations", {})[C.RBG_SERVICE_ID_ANNOTATION] = identity
    old["metadata"].update(
        uid="attachment-uid",
        resourceVersion="5",
        ownerReferences=[
            {
                "apiVersion": C.RBG_API_VERSION,
                "kind": "RoleBasedGroup",
                "name": group["metadata"]["name"],
                "uid": group["metadata"]["uid"],
            }
        ],
    )
    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", lambda *a, **kw: [old])
    patches = []
    monkeypatch.setattr(rbg_lifecycle, "_patch_rbg_object", lambda resource, live, patch: patches.append(patch) or live)
    rbg_lifecycle._submit_rbg_object(new, render.deployment_settings(config), identity, rbg=group)
    patch = patches[0]
    assert patch[:2] == rbg_lifecycle._rbg_patch_tests(old)
    assert next(p["value"] for p in patch if p["path"] == "/spec") == new["spec"]


def test_full_update_keeps_requested_counts_and_selected_mode(resources):
    live, _, config = resources
    config["motor_deploy_config"]["p_instances_num"] = 4
    bundle = generate_rbg_resources(
        config, {}, facts={"rbg": live, "runtime_configmap": resources[1], "accelerator_type": "module-910b-8"}
    )
    plan = rbg_lifecycle._prepare_rbg_plan(bundle, {"policy": None, "hpas": None}, live)
    assert next(r for r in plan["rbg"]["spec"]["roles"] if r["name"] == "prefill")["replicas"] == 4
    runtime = next(o for o in plan["prerequisites"] if o["kind"] == "ConfigMap")
    deploy = json.loads(runtime["data"]["user_config.json"])["motor_deploy_config"]
    assert deploy["p_instances_num"] == 4
    assert "hybrid_instances_num" not in deploy and "e_instances_num" not in deploy


def test_full_update_preserves_only_hpa_owned_counts(resources):
    live, runtime, config = deepcopy(resources)
    prefill = next(role for role in live["spec"]["roles"] if role["name"] == "prefill")
    prefill.update(replicas=3, scalingAdapter={"enable": True})
    bundle = generate_rbg_resources(
        config, {}, facts={"rbg": live, "runtime_configmap": runtime, "accelerator_type": "module-910b-8"}
    )
    preserved = rbg_lifecycle._prepare_rbg_plan(bundle, {"policy": None, "hpas": None}, live)
    assert next(role for role in preserved["rbg"]["spec"]["roles"] if role["name"] == "prefill")["replicas"] == 3
    disabled = rbg_lifecycle._prepare_rbg_plan(bundle, {"policy": None, "hpas": {}}, live)
    assert next(role for role in disabled["rbg"]["spec"]["roles"] if role["name"] == "prefill")["replicas"] == 1


@pytest.mark.parametrize("removed", [True, False])
def test_submit_removes_template_dict_fields_but_accepts_api_defaults(monkeypatch, resources, removed):
    live, runtime, config = deepcopy(resources)
    desired = deepcopy(live)
    live["spec"]["minReadySeconds"] = 0
    if removed:
        render.pod_template(live["spec"]["roles"][0])["spec"]["nodeSelector"] = {"pool": "old"}
        render.pod_template(desired["spec"]["roles"][0])["spec"].pop("nodeSelector", None)
    normalized = deepcopy(desired)
    normalized["spec"]["minReadySeconds"] = 0
    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", lambda *a, **kw: [live])
    commands = []

    def server(command):
        commands.append(recorded_command(command))
        return json.dumps(normalized)

    monkeypatch.setattr(rbg_lifecycle, "run_cmd_get_output", server)
    identity = runtime["metadata"]["annotations"][C.RBG_SERVICE_ID_ANNOTATION]
    rbg_lifecycle._submit_rbg_object(desired, render.deployment_settings(config), identity, rbg=live)
    writes = [cmd for cmd in commands if "--dry-run=server" not in cmd]
    assert len(writes) == int(removed)
    if removed:
        patch = json.loads(writes[0][writes[0].index("--patch-json") + 1])
        assert patch[:2] == rbg_lifecycle._rbg_patch_tests(live)
        spec = next(op["value"] for op in patch if op["path"] == "/spec")
        assert "nodeSelector" not in render.pod_template(spec["roles"][0])["spec"]


@pytest.mark.parametrize("error", ["timeout", "forbidden"])
def test_rbg_defaulting_failure_never_becomes_a_successful_update(monkeypatch, resources, error):
    live, runtime, config = deepcopy(resources)
    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", lambda *a, **kw: [live])
    commands = []

    def fail(command):
        commands.append(recorded_command(command))
        raise RuntimeError(error)

    monkeypatch.setattr(rbg_lifecycle, "run_cmd_get_output", fail)
    with pytest.raises(RuntimeError, match=error):
        rbg_lifecycle._submit_rbg_object(
            live,
            render.deployment_settings(config),
            runtime["metadata"]["annotations"][C.RBG_SERVICE_ID_ANNOTATION],
            rbg=live,
        )
    assert len(commands) == 1 and "--dry-run=server" in commands[0]


def test_submit_can_activate_zero_role_with_hpa(monkeypatch, resources):
    live, runtime, config = deepcopy(resources)
    next(role for role in live["spec"]["roles"] if role["name"] == "prefill")["replicas"] = 0
    bundle = generate_rbg_resources(
        config, {}, facts={"rbg": live, "runtime_configmap": runtime, "accelerator_type": "module-910b-8"}
    )
    desired = next(role for role in bundle["rbg"]["spec"]["roles"] if role["name"] == "prefill")
    desired["scalingAdapter"] = {"enable": True}
    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", lambda *args, **kwargs: [live])
    writes = []

    def patch_object(*args, dry_run=False):
        if dry_run:
            return {**live, "spec": args[2][-1]["value"]}
        writes.append(args)
        return live

    monkeypatch.setattr(rbg_lifecycle, "_patch_rbg_object", patch_object)
    identity = runtime["metadata"]["annotations"][C.RBG_SERVICE_ID_ANNOTATION]
    rbg_lifecycle._submit_rbg_object(bundle["rbg"], render.deployment_settings(config), identity, rbg=live)
    spec = next(op["value"] for op in writes[0][2] if op["path"] == "/spec")
    assert next(role for role in spec["roles"] if role["name"] == "prefill")["replicas"] == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        SimpleNamespace(returncode=0, stdout="invalid", stderr=""),
        {"data": {}},
        SimpleNamespace(returncode=1, stdout="", stderr="Forbidden"),
        RuntimeError("Command timed out"),
    ],
)
@pytest.mark.parametrize("entry", [None, "handle_update_config", "handle_update_instance_num"])
def test_iss_baseline_query_preserves_legacy_failure_behavior(monkeypatch, response, entry):
    commands = command_responses(monkeypatch, [response])
    if entry is None:
        assert k8s_utils.get_baseline_config_from_configmap("test-motor") is None
    else:
        config = {"motor_deploy_config": {"job_id": "test-motor", "deploy_mode": "infer_service_set"}}
        with pytest.raises(FileNotFoundError):
            getattr(deploy, entry)(config)
    assert len(commands) == 1
    assert "--ignore-not-found=true" not in commands[0]
    assert not any(arg.startswith("--request-timeout") for arg in commands[0])


def test_rbg_baseline_reads_config_and_rejects_non_object(monkeypatch, resources):
    _, runtime, config = resources
    command_responses(monkeypatch, [runtime])
    assert rbg_lifecycle.get_rbg_baseline_config(config) == json.loads(runtime["data"]["user_config.json"])
    runtime["data"]["user_config.json"] = "[]"
    command_responses(monkeypatch, [runtime])
    with pytest.raises(ValueError, match="must be an object"):
        rbg_lifecycle.get_rbg_baseline_config(config)


@pytest.mark.parametrize("enabled", [False, True])
def test_new_adapter_is_disabled_until_ris_has_selector(resources, enabled):
    from lib.rbg.scaling import generate_scaling_resources

    live, runtime, config = deepcopy(resources)
    role = next(r for r in live["spec"]["roles"] if r["name"] == "prefill")
    role["scalingAdapter"] = {"enable": enabled}
    config["scaling_policy"] = {"prefill": {"min_replicas": 1, "max_replicas": 2}}
    bundle = generate_rbg_resources(
        config, {}, facts={"rbg": live, "runtime_configmap": runtime, "accelerator_type": "module-910b-8"}
    )
    scaling = generate_scaling_resources(config, bundle["rbg"], is_update=True, current={"policy": None, "hpas": {}})
    plan = rbg_lifecycle._prepare_rbg_plan(bundle, scaling, live)
    assert (
        next(r for r in plan["rbg"]["spec"]["roles"] if r["name"] == "prefill")["scalingAdapter"]["enable"] is enabled
    )


@pytest.mark.parametrize("state", ["new", "healthy", "empty", "referenced"])
def test_adapter_enable_waits_for_selector_and_repairs_only_unused_target(monkeypatch, resources, state):
    live, _, config = deepcopy(resources)
    role = next(r for r in live["spec"]["roles"] if r["name"] == "prefill")
    role["scalingAdapter"] = {"enable": state != "new"}
    owner = {
        "apiVersion": C.RBG_API_VERSION,
        "kind": C.RBG_KIND,
        "name": live["metadata"]["name"],
        "uid": live["metadata"]["uid"],
        "controller": True,
    }
    name = live["metadata"]["name"] + "-prefill"
    meta = {
        "name": name,
        "namespace": live["metadata"]["namespace"],
        "uid": "native-uid",
        "resourceVersion": "1",
        "ownerReferences": [owner],
    }
    ris = {"apiVersion": C.RBG_API_VERSION, "kind": "RoleInstanceSet", "metadata": meta, "status": {}}
    ready = deepcopy(ris)
    ready["status"]["labelSelector"] = "app=prefill"
    responses = iter([json.dumps(ris), json.dumps(ready)])
    reads = []
    monkeypatch.setattr(rbg_lifecycle, "run_cmd_get_output", lambda *a, **kw: reads.append(a) or next(responses))
    monkeypatch.setattr(rbg_lifecycle.time, "sleep", lambda _: None)
    monkeypatch.setattr(rbg_lifecycle, "_read_managed_resource", lambda *a: deepcopy(live))
    adapter = {
        "apiVersion": C.RBG_API_VERSION,
        "kind": C.RBG_SCALING_ADAPTER_KIND,
        "metadata": meta,
        "spec": {"scaleTargetRef": {"name": live["metadata"]["name"], "role": "prefill"}},
        "status": {"phase": "Bound", "selector": "app=prefill" if state == "healthy" else ""},
    }

    def query(resource, *args, **kwargs):
        if resource == C.RBG_ADAPTER_RESOURCE:
            return [] if state == "new" else [adapter]
        return [{"spec": {"scaleTargetRef": {"name": name}}}] if state == "referenced" else []

    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", query)
    actions = []

    def patch(resource, original, changes):
        assert len(reads) == 2
        enabled = changes[-1]["value"]
        actions.append(enabled)
        role["scalingAdapter"]["enable"] = enabled
        return deepcopy(live)

    monkeypatch.setattr(rbg_lifecycle, "_patch_rbg_object", patch)
    monkeypatch.setattr(rbg_lifecycle, "_wait_rbg_adapter", lambda *a, **kw: actions.append("absent"))
    if state == "referenced":
        with pytest.raises(ValueError, match="Delete and confirm the HPA"):
            rbg_lifecycle._enable_rbg_adapter(config, live, "prefill")
        assert not actions
    else:
        rbg_lifecycle._enable_rbg_adapter(config, live, "prefill")
        assert actions == {"new": [True], "healthy": [], "empty": [False, "absent", True]}[state]


def test_adapter_selector_api_failure_never_enables_adapter(monkeypatch, resources):
    live, _, config = resources
    monkeypatch.setattr(
        rbg_lifecycle, "run_cmd_get_output", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("forbidden"))
    )
    with pytest.raises(PermissionError, match="forbidden"):
        rbg_lifecycle._enable_rbg_adapter(config, live, "prefill")
    with pytest.raises(TimeoutError, match="status.labelSelector"):
        rbg_lifecycle._enable_rbg_adapter(config, live, "prefill", timeout_seconds=0)


def test_runtime_update_rejects_change_since_caller_baseline(monkeypatch, resources):
    group, runtime, desired = deepcopy(resources)
    baseline = json.loads(runtime["data"]["user_config.json"])
    concurrent = deepcopy(baseline)
    concurrent["motor_controller_config"]["logging_config"]["log_level"] = "WARNING"
    runtime["data"]["user_config.json"] = json.dumps(concurrent)
    commands = command_responses(monkeypatch, [group, runtime])
    with pytest.raises(RuntimeError, match="conflict"):
        rbg_lifecycle.update_rbg_runtime_config(desired, baseline)
    assert not any("patch" in command for command in commands)


def test_large_patch_uses_closed_temporary_file_and_cleans_up(monkeypatch, resources):
    group, _, _ = deepcopy(resources)
    patch = [{"op": "add", "path": "/metadata/annotations/large", "value": "x" * 140000}]
    paths = []

    def run(command, **kwargs):
        assert "-p" not in command
        path = Path(command[command.index("--patch-file") + 1])
        assert json.loads(path.read_text(encoding="utf-8")) == patch
        paths.append(path)
        return json.dumps(group)

    monkeypatch.setattr(rbg_lifecycle, "run_cmd_get_output", run)
    rbg_lifecycle._patch_rbg_object(C.RBG_RESOURCE, group, patch)
    assert paths and not paths[0].exists()


def test_preflight_rejects_served_schema_missing_generated_fields(monkeypatch, resources):
    group, _, desired = deepcopy(resources)
    role = group["spec"]["roles"][0]
    role["leaderWorkerPattern"] = {"restartPolicyConfig": {"type": "RecreateRoleInstanceOnPodRestart"}}
    role.pop("standalonePattern", None)
    writes = []

    def query(resource, namespace, **kwargs):
        if resource == "namespace":
            return [{"kind": "Namespace", "metadata": {"name": namespace}}]
        if resource.startswith("customresourcedefinitions"):
            return [
                {
                    "spec": {
                        "group": C.RBG_API_GROUP,
                        "versions": [
                            {
                                "name": "v1alpha2",
                                "served": True,
                                "schema": {
                                    "openAPIV3Schema": {
                                        "properties": {
                                            "spec": {
                                                "properties": {
                                                    "roles": {
                                                        "items": {
                                                            "properties": {"leaderWorkerPattern": {"properties": {}}}
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                },
                            }
                        ],
                    }
                }
            ]
        return []

    monkeypatch.setattr(rbg_lifecycle, "_deletion_query", query)
    monkeypatch.setattr(rbg_lifecycle, "run_cmd_get_output", lambda *a, **kw: writes.append(a))
    plan = {"rbg": group, "prerequisites": [], "services": [], "policy": None, "hpas": None}
    with pytest.raises(ValueError, match="restartPolicyConfig"):
        rbg_lifecycle._preflight_rbg_plan(plan, render.deployment_settings(desired))
    assert not writes


def test_second_rbg_reports_fixed_configmap_owner_before_any_write(monkeypatch, resources):
    _, runtime, desired = deepcopy(resources)
    requested = desired["motor_deploy_config"].setdefault("rbg_config", {})
    requested["name"] = "second"
    monkeypatch.setattr(rbg_lifecycle, "_read_rbg_kv_facts", lambda _: {})
    commands = command_responses(monkeypatch, [None, runtime])
    with pytest.raises(ValueError, match="one RBG per namespace") as error:
        rbg_lifecycle._read_rbg_facts(desired)
    assert "second" in str(error.value) and runtime["metadata"]["labels"][C.RBG_GROUP_LABEL] in str(error.value)
    assert not any("patch" in command or "create" in command for command in commands)


def test_crd_preflight_accepts_native_ris_passthrough_template():
    version = {
        "schema": {
            "openAPIV3Schema": {
                "properties": {
                    "spec": {"properties": {"roleInstanceTemplate": {"x-kubernetes-preserve-unknown-fields": True}}}
                }
            }
        }
    }
    roles = [{"customComponentsPattern": {"restartPolicyConfig": {"type": "RecreateRoleInstanceOnPodRestart"}}}]
    rbg_lifecycle._check_rbg_schema(version, "roleinstancesets", roles)


def test_rbg_queries_keep_process_deadline_without_overriding_incluster_config(monkeypatch):
    calls = []

    def command(args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps({"items": []})

    monkeypatch.setattr(rbg_lifecycle, "run_cmd_get_output", command)
    assert rbg_lifecycle._deletion_query("services", "demo", timeout_seconds=7) == []
    assert calls[0][1]["timeout"] == 7
    assert not any(arg.startswith("--request-timeout") for arg in calls[0][0])
