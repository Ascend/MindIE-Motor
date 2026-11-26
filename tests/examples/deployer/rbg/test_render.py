# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Test RBG resource rendering independently of lifecycle submission and scaling generation."""

from copy import deepcopy
from types import SimpleNamespace

import lib.constant as C
import pytest
import yaml
from lib.generator import k8s_utils
from lib.rbg import render
from lib.rbg.pods import pod_template


@pytest.fixture
def config():
    return {
        "motor_deploy_config": {
            "deploy_mode": "rbg",
            "job_id": "test-motor",
            "image_name": "motor:test",
            "hardware_type": C.HARDWARE_TYPE_800I_A2,
            "p_instances_num": 2,
            "d_instances_num": 2,
            "single_p_instance_pod_num": 2,
            "single_d_instance_pod_num": 2,
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
        "motor_controller_config": {},
        "motor_coordinator_config": {},
    }


@pytest.fixture
def facts():
    return {
        "rbg": None,
        "runtime_configmap": None,
        "policy": None,
        "hpas": {},
        "services": {},
        "accelerator_type": "module-910b-8",
        "external_service_addresses": {},
        "warnings": [],
    }


def generate(config, facts, tmp_path, env=None):
    context = render.deployment_settings(config)
    return context, render.generate_rbg_resources(config, env or {}, facts=facts)


def test_official_env_version_is_preserved_as_metadata(config, facts):
    import json

    env = json.loads((C.RBG_DEPLOYER_ROOT.parent.parent / "model_configs/vllm/env.json").read_text(encoding="utf-8"))
    bundle = render.generate_rbg_resources(config, env, facts=facts)
    data = next(obj["data"] for obj in bundle["prerequisites"] if obj["kind"] == "ConfigMap")
    assert json.loads(data["env.json"])["version"] == env["version"]
    assert "export version=" not in data["common.sh"]
    env["motor_common_env"] = "invalid"
    with pytest.raises(TypeError, match="motor_common_env must be an object"):
        render.generate_rbg_resources(config, env, facts=facts)


def test_runtime_identity_matches_startup_environment(config, facts):
    import json

    config["north_config"] = {"name": "north-test"}
    bundle = render.generate_rbg_resources(config, {}, facts=facts)
    cm = next(obj for obj in bundle["prerequisites"] if obj["kind"] == "ConfigMap")
    common = json.loads(cm["data"]["env.json"])[C.MOTOR_COMMON_ENV]
    assert common[C.ENV_ENGINE_TYPE] == "vllm"
    assert common[C.ENV_MODEL_NAME] == "/weights"
    assert common[C.ENV_NORTH_PLATFORM] == "north-test"
    assert common[C.ENV_SERVICE_ID] == cm["metadata"]["annotations"][C.RBG_SERVICE_ID_ANNOTATION]
    for key in (C.ENV_ENGINE_TYPE, C.ENV_MODEL_NAME, C.ENV_NORTH_PLATFORM, C.ENV_SERVICE_ID):
        assert f"export {key}=" in cm["data"]["common.sh"]
        with pytest.raises(ValueError, match="overrides managed fields"):
            render.generate_rbg_resources(config, {"motor_engine_decode_env": {key: "wrong"}}, facts=facts)


@pytest.mark.parametrize("identity_config", [{}, {"identity_mode": "legacy"}, {"identity_mode": "v1"}])
def test_conductor_service_and_startup_use_configured_port_and_identity(config, facts, identity_config):
    import json

    config[C.KV_CACHE_STORE_CONFIG] = {
        "backend": "mooncake",
        "eviction_high_watermark_ratio": 0.9,
        "eviction_ratio": 0.1,
    }
    config[C.KV_CONDUCTOR_CONFIG] = {C.KV_CONDUCTOR_PORT: 14444, **identity_config}
    bundle = render.generate_rbg_resources(config, {}, facts=facts)
    role = next(r for r in bundle["rbg"]["spec"]["roles"] if r["name"] == C.ROLE_KV_CONDUCTOR)
    env = {e["name"]: e.get("value") for e in pod_template(role)["spec"]["containers"][0]["env"]}
    service = next(s for s in bundle["services"] if s["metadata"]["labels"][C.RBG_ROLE_LABEL] == C.ROLE_KV_CONDUCTOR)
    cm = next(obj for obj in bundle["prerequisites"] if obj["kind"] == "ConfigMap")
    assert service["spec"]["ports"][0]["targetPort"] == int(env["KV_CONDUCTOR_PORT"]) == 14444
    assert "export KV_CONDUCTOR_PORT=14444" in cm["data"]["kv_conductor.sh"]
    expected_mode = identity_config.get("identity_mode", "legacy")
    assert f"export KV_CONDUCTOR_IDENTITY_MODE={expected_mode}" in cm["data"]["kv_conductor.sh"]
    conductor_env = json.loads(cm["data"]["env.json"])["motor_kv_conductor_env"]
    assert conductor_env["KV_CONDUCTOR_PORT"] == 14444
    assert conductor_env["KV_CONDUCTOR_IDENTITY_MODE"] == expected_mode


def test_conductor_startup_rejects_invalid_or_overridden_identity_mode(config):
    config[C.KV_CONDUCTOR_CONFIG] = {C.KV_CONDUCTOR_PORT: 14444, "identity_mode": "invalid"}
    context = render.deployment_settings(config)
    with pytest.raises(ValueError, match="identity_mode must be"):
        render.render_configmap(context, config, {}, "service-id")

    config[C.KV_CONDUCTOR_CONFIG]["identity_mode"] = "v1"
    env = {"motor_kv_conductor_env": {"KV_CONDUCTOR_IDENTITY_MODE": "legacy"}}
    with pytest.raises(ValueError, match="overrides managed fields.*KV_CONDUCTOR_IDENTITY_MODE"):
        render.render_configmap(context, config, env, "service-id")


@pytest.mark.parametrize("value,expected", [(True, "1"), (False, "0"), ("True", "1"), ("FALSE", "0")])
def test_instance_port_switch_has_same_shell_and_pod_value(config, facts, value, expected):
    config[C.MOTOR_ENGINE_DECODE_CONFIG][C.MOTOR_NODEMANAGER_CONFIG] = {
        "port_allocator_config": {"enable": False},
        "api_config": {"node_manager_port": 1026},
        "endpoint_config": {"base_port": 8000},
    }
    config[C.MOTOR_ENGINE_DECODE_CONFIG][C.ENGINE_CONFIG]["data_parallel_rpc_port"] = 9000
    values = {
        "MOTOR_INSTANCE_PORTS_ENABLE": value,
        "MOTOR_INSTANCE_PORT_STRIDE": 100,
        "ASCEND_BASE_PORT": "12000",
        "HIXL_LISTEN_PORT": "16000",
    }
    bundle = render.generate_rbg_resources(
        config,
        {
            "motor_common_env": {"MOTOR_INSTANCE_PORTS_ENABLE": False, "MOTOR_INSTANCE_PORT_STRIDE": 200},
            "motor_engine_decode_env": values,
        },
        facts=facts,
    )
    role = next(r for r in bundle["rbg"]["spec"]["roles"] if r["name"] == "decode")
    env = {e["name"]: e.get("value") for e in pod_template(role)["spec"]["containers"][0]["env"]}
    cm = next(obj for obj in bundle["prerequisites"] if obj["kind"] == "ConfigMap")
    assert env["MOTOR_INSTANCE_PORTS_ENABLE"] == expected
    assert f"export MOTOR_INSTANCE_PORTS_ENABLE={expected}" in cm["data"]["engine.sh"]
    assert env["MOTOR_INSTANCE_PORT_STRIDE"] == "100"
    assert "export MOTOR_INSTANCE_PORTS_ENABLE=" not in cm["data"]["common.sh"]
    assert "export MOTOR_INSTANCE_PORT_STRIDE=" not in cm["data"]["common.sh"]
    if expected == "1":
        for missing in ("ASCEND_BASE_PORT", "HIXL_LISTEN_PORT"):
            incomplete = {key: item for key, item in values.items() if key != missing}
            with pytest.raises(ValueError, match=f"instance ports require {missing}"):
                render.generate_rbg_resources(config, {"motor_engine_decode_env": incomplete}, facts=facts)


@pytest.mark.parametrize(
    "section,key,value,restarts_engine",
    [
        (C.MOTOR_COORDINATOR_CONFIG, "scheduler_config", {"enable_pd_separation_fallback_to_hybrid": False}, False),
        (C.MOTOR_CONTROLLER_CONFIG, "logging_config", {"log_level": "DEBUG"}, False),
        (C.MOTOR_CONTROLLER_CONFIG, C.FAULT_TOLERANCE_CONFIG, {"enable_dp_scale_down": True}, True),
        (C.MOTOR_CONTROLLER_CONFIG, "instance_config", {"instance_assemble_timeout": 120}, True),
        (C.MOTOR_CONTROLLER_CONFIG, "api_config", {"controller_api_port": 1126}, True),
    ],
)
def test_engine_digest_only_tracks_consumed_control_configuration(config, facts, section, key, value, restarts_engine):
    before = render.generate_rbg_resources(config, {}, facts=facts)
    facts.update(
        rbg=before["rbg"], runtime_configmap=next(obj for obj in before["prerequisites"] if obj["kind"] == "ConfigMap")
    )
    config[section][key] = value
    after = render.generate_rbg_resources(config, {}, facts=facts)
    for old, new in zip(before["rbg"]["spec"]["roles"], after["rbg"]["spec"]["roles"]):
        if old["name"] in ("prefill", "decode"):
            assert (pod_template(old) != pod_template(new)) == restarts_engine
        elif C.RBG_ROLE_CONFIGS[old["name"]] == section:
            assert pod_template(old) != pod_template(new)


def test_unknown_coordinator_service_port_reports_template_context(config, facts, monkeypatch):
    original = render._load_templates

    def load():
        rbg, catalog, services, rbac = original()
        service = next(s for s in services if s["metadata"]["labels"][C.RBG_TEMPLATE_ROLE_LABEL] == C.COORDINATOR)
        service["metadata"]["name"] = "custom-coordinator"
        service["spec"]["ports"][0]["port"] = 12345
        return rbg, catalog, services, rbac

    monkeypatch.setattr(render, "_load_templates", load)
    with pytest.raises(ValueError, match=r"custom-coordinator.*12345.*1025.*1026.*1027"):
        render.generate_rbg_resources(config, {}, facts=facts)


def test_complete_bundle_has_native_roles_identity_and_services(config, facts, tmp_path, monkeypatch):
    monkeypatch.setattr(
        k8s_utils,
        "get_accelerator_type_from_cluster",
        lambda *_: pytest.fail("render queried cluster"),
    )
    before = deepcopy(config)
    _, bundle = generate(config, facts, tmp_path)
    assert config == before
    assert bundle["rbg"]["apiVersion"] == "workloads.x-k8s.io/v1alpha2"
    assert bundle["rbg"]["metadata"]["annotations"][C.RBG_GANG_ANNOTATION] == "true"
    assert len(bundle["services"]) == 5
    roles = {r["name"]: r for r in bundle["rbg"]["spec"]["roles"]}
    prefill = roles["prefill"]
    assert prefill["replicas"] == 2
    assert prefill["customComponentsPattern"]["components"][0]["size"] == 2
    template = pod_template(prefill)
    assert template["spec"]["schedulerName"] == "volcano"
    env = {e["name"]: e for e in template["spec"]["containers"][0]["env"]}
    assert env["INSTANCE_INDEX"]["valueFrom"]["fieldRef"]["fieldPath"] == f"metadata.labels['{C.RBG_INDEX_LABEL}']"
    assert "fault-scheduling" not in template["metadata"]["labels"]
    assert "lifecycle" in template["spec"]["containers"][0]
    cm = next(d for d in bundle["prerequisites"] if d["kind"] == "ConfigMap")
    assert {
        "boot.sh",
        "prestop.py",
        "instance_config.py",
        "probe.sh",
        "engine.sh",
        "user_config.json",
    } <= cm["data"].keys()
    assert cm["metadata"]["annotations"][C.RBG_SERVICE_ID_ANNOTATION]
    assert set(bundle) == {"rbg", "services", "prerequisites", "user_config"}
    assert all(
        d["kind"] not in ("InferServiceSet", "Deployment", "StatefulSet", "PodGroup") for d in bundle["prerequisites"]
    )


def test_generation_does_not_read_infer_template_or_modify_startup(config, facts, tmp_path, monkeypatch):
    from lib.generator import infer_service

    monkeypatch.setattr(
        infer_service,
        "generate_yaml_infer_service_set",
        lambda *_: pytest.fail("legacy generator"),
    )
    source = C.RBG_DEPLOYER_ROOT / "startup/roles/engine.sh"
    before = source.read_bytes()
    _, bundle = generate(
        config,
        facts,
        tmp_path,
        {"motor_engine_prefill_env": {"LITERAL": "$(touch /tmp/no); 'quote'"}},
    )
    assert source.read_bytes() == before
    assert "LITERAL=" in next(d for d in bundle["prerequisites"] if d["kind"] == "ConfigMap")["data"]["engine.sh"]


def test_updates_keep_identity_and_do_not_invent_policy_or_hpa(config, facts, tmp_path):
    _, first = generate(config, facts, tmp_path)
    facts.update(
        rbg=first["rbg"],
        runtime_configmap=next(d for d in first["prerequisites"] if d["kind"] == "ConfigMap"),
    )
    _, second = generate(config, facts, tmp_path)
    assert "policy" not in second and "hpas" not in second
    assert second["rbg"] == first["rbg"]
    assert second["prerequisites"] == first["prerequisites"]


@pytest.mark.parametrize("name,value", [("gang_scheduling", "false"), ("scheduler_name", "")])
def test_invalid_scheduling_is_rejected(config, facts, tmp_path, name, value):
    config["motor_deploy_config"]["rbg_config"] = {name: value}
    with pytest.raises(ValueError):
        generate(config, facts, tmp_path)


def test_disabling_gang_preserves_volcano(config, facts, tmp_path):
    config["motor_deploy_config"]["rbg_config"] = {"gang_scheduling": False}
    _, bundle = generate(config, facts, tmp_path)
    assert bundle["rbg"]["metadata"]["annotations"][C.RBG_GANG_ANNOTATION] == "false"
    assert {pod_template(r)["spec"]["schedulerName"] for r in bundle["rbg"]["spec"]["roles"]} == {"volcano"}


@pytest.mark.parametrize("gang", [True, False])
@pytest.mark.parametrize("policy", ["None", "RecreateRoleInstanceOnPodRestart"])
def test_explicit_multi_pod_restart_policy_is_independent_of_gang(config, facts, gang, policy):
    config["motor_deploy_config"]["rbg_config"] = {
        "gang_scheduling": gang,
        "restart_policy_by_role": {"prefill": policy, "decode": policy},
    }
    bundle = render.generate_rbg_resources(config, {}, facts=facts)
    assert bundle["rbg"]["metadata"]["annotations"][C.RBG_GANG_ANNOTATION] == str(gang).lower()
    for role in bundle["rbg"]["spec"]["roles"]:
        if role["name"] in {"prefill", "decode"}:
            pattern = role["customComponentsPattern"]
            assert pattern["components"][0]["size"] == 2
            assert pattern["restartPolicyConfig"]["type"] == policy


def test_zero_decode_and_dp_do_not_remove_healthy_members(config, facts, tmp_path):
    config["motor_deploy_config"]["d_instances_num"] = 0
    config["motor_controller_config"]["fault_tolerance_config"] = {"enable_dp_scale_down": True}
    config["motor_engine_decode_config"]["engine_config"]["enable_fault_tolerance"] = True
    _, bundle = generate(config, facts, tmp_path)
    decode = next(r for r in bundle["rbg"]["spec"]["roles"] if r["name"] == "decode")
    assert decode["replicas"] == 0
    assert decode["customComponentsPattern"]["restartPolicyConfig"]["type"] == "None"
    config["motor_deploy_config"]["rbg_config"] = {
        "restart_policy_by_role": {"decode": "RecreateRoleInstanceOnPodRestart"}
    }
    with pytest.raises(ValueError, match="DP"):
        generate(config, facts, tmp_path)


def test_plain_and_ha_external_kv_never_deploy_local_master(config, facts, tmp_path):
    facts["kv_store_reusable"] = False
    for address in ("kv.example:50088", "etcd://kv:2379"):
        config["kv_cache_store_config"] = {
            "backend": "mooncake",
            "master_server_address": address,
            "target_job_id": "other-motor",
        }
        _, bundle = generate(config, facts, tmp_path)
        assert next(role for role in bundle["rbg"]["spec"]["roles"] if role["name"] == "kv-store")["replicas"] == 0
        env = pod_template(next(r for r in bundle["rbg"]["spec"]["roles"] if r["name"] == "prefill"))["spec"][
            "containers"
        ][0]["env"]
        assert {e["name"]: e.get("value") for e in env}["KV_STORE_EXTERNAL"] == "1"


@pytest.mark.parametrize("backend", ["mooncake", "memcache"])
@pytest.mark.parametrize("reusable", [False, True])
def test_target_kv_lookup_controls_local_resources_and_engine_address(config, facts, backend, reusable):
    config["kv_cache_store_config"] = {"backend": backend, "target_job_id": "other-motor"}
    if backend == "mooncake":
        config["kv_cache_store_config"].update(eviction_high_watermark_ratio=0.95, eviction_ratio=0.05)
    external = "shared-kv.other-motor.svc.cluster.local"
    facts["kv_store_reusable"] = reusable
    facts["external_service_addresses"] = {"kv_store": external} if reusable else {}
    before = deepcopy((config, facts))
    bundle = render.generate_rbg_resources(config, {}, facts=facts)
    roles = {role["name"]: role for role in bundle["rbg"]["spec"]["roles"]}
    services = [
        service
        for service in bundle["services"]
        if service["metadata"]["labels"].get("rbg.workloads.x-k8s.io/role-name") == "kv-store"
    ]
    assert (roles["kv-store"]["replicas"] > 0) is not reusable
    assert len(services) == (0 if reusable else 1)
    address = external if reusable else (f"{services[0]['metadata']['name']}.test-motor.svc.cluster.local")
    for name in ("prefill", "decode"):
        env = {e["name"]: e.get("value") for e in pod_template(roles[name])["spec"]["containers"][0]["env"]}
        assert env[C.ENV_KVS_MASTER_SERVICE] == address
    assert (config, facts) == before


@pytest.mark.parametrize("reusable", [None, True])
def test_target_kv_without_address_or_negative_lookup_does_not_fall_back(config, facts, reusable):
    config["kv_cache_store_config"] = {"backend": "mooncake", "target_job_id": "other-motor"}
    if reusable is not None:
        facts["kv_store_reusable"] = reusable
    with pytest.raises(ValueError, match="External KV target requires"):
        render.generate_rbg_resources(config, {}, facts=facts)


def test_mmc_ports_and_repeated_generation_do_not_leak_globals(config, facts, tmp_path):
    config["kv_cache_store_config"] = {"backend": "memcache", "port": 50088}
    _, bundle = generate(config, facts, tmp_path)
    assert "kv-store" in {role["name"] for role in bundle["rbg"]["spec"]["roles"]}
    service = next(
        s for s in bundle["services"] if s["metadata"]["labels"].get("rbg.workloads.x-k8s.io/role-name") == "kv-store"
    )
    assert {p["name"] for p in service["spec"]["ports"]} == {"business", "config-store", "metrics", "kv-events"}
    role = next(r for r in bundle["rbg"]["spec"]["roles"] if r["name"] == "kv-store")
    assert (
        next(e["value"] for e in pod_template(role)["spec"]["containers"][0]["env"] if e["name"] == "ROLE")
        == "kv_store"
    )
    del config["kv_cache_store_config"]
    _, without = generate(config, facts, tmp_path)
    assert next(role for role in without["rbg"]["spec"]["roles"] if role["name"] == "kv-store")["replicas"] == 0


@pytest.mark.parametrize(
    "env",
    [
        {"motor_common_env": {"INSTANCE_INDEX": "99"}},
        {"motor_engine_prefill_env": {"JOB_NAME": "old"}},
    ],
)
def test_managed_environment_cannot_be_overridden(config, facts, tmp_path, env):
    with pytest.raises(ValueError, match="managed"):
        generate(config, facts, tmp_path, env)


def test_resource_files_roundtrip(config, facts, tmp_path):
    _, bundle = generate(config, facts, tmp_path)
    paths = render.write_rbg_resources(bundle, tmp_path)
    assert len(paths) == 3
    assert yaml.safe_load((tmp_path / "rbg.yaml").read_text(encoding="utf-8")) == bundle["rbg"]


def test_p2d_is_disabled_in_effective_runtime_config(config, facts, tmp_path):
    import json

    _, bundle = generate(config, facts, tmp_path)
    cm = next(d for d in bundle["prerequisites"] if d["kind"] == "ConfigMap")
    runtime = json.loads(cm["data"]["user_config.json"])
    assert runtime["motor_controller_config"]["fault_tolerance_config"]["enable_scale_p2d"] is False
    config["motor_controller_config"]["fault_tolerance_config"] = {"enable_scale_p2d": True}
    with pytest.raises(ValueError, match="P2D"):
        generate(config, facts, tmp_path)


def test_scale_only_generation_keeps_startup_digest(config, facts, tmp_path):
    _, first = generate(config, facts, tmp_path)
    facts.update(
        rbg=first["rbg"],
        runtime_configmap=next(d for d in first["prerequisites"] if d["kind"] == "ConfigMap"),
    )
    config["motor_deploy_config"]["p_instances_num"] = 3
    _, second = generate(config, facts, tmp_path)
    for before, after in zip(first["rbg"]["spec"]["roles"], second["rbg"]["spec"]["roles"]):
        assert pod_template(before) == pod_template(after)


@pytest.mark.parametrize(
    "section,changed_roles",
    [
        ("motor_controller_env", {"controller"}),
        ("motor_engine_prefill_env", {"prefill"}),
        ("motor_common_env", {"controller", "coordinator", "prefill", "decode"}),
    ],
)
def test_env_update_only_changes_consuming_role_templates(config, facts, section, changed_roles):
    before = render.generate_rbg_resources(config, {}, facts=facts)
    facts.update(
        rbg=before["rbg"], runtime_configmap=next(obj for obj in before["prerequisites"] if obj["kind"] == "ConfigMap")
    )
    after = render.generate_rbg_resources(config, {section: {"EXAMPLE_SETTING": "new"}}, facts=facts)
    for old, new in zip(before["rbg"]["spec"]["roles"], after["rbg"]["spec"]["roles"]):
        if old["name"] in {"controller", "coordinator", "prefill", "decode"}:
            assert (pod_template(old) != pod_template(new)) == (old["name"] in changed_roles)


def test_explicit_ports_reject_instance_count_overflow(config, facts, tmp_path):
    from lib.rbg.config import validate_instance_ports

    config["motor_engine_decode_config"]["motor_nodemanger_config"] = {
        "port_allocator_config": {"enable": False},
        "api_config": {"node_manager_port": 65000},
        "endpoint_config": {"base_port": 64000},
    }
    config["motor_engine_decode_config"]["engine_config"]["data_parallel_rpc_port"] = 63000
    env = {
        "motor_engine_decode_env": {
            "MOTOR_INSTANCE_PORTS_ENABLE": "1",
            "ASCEND_BASE_PORT": "12000",
            "HIXL_LISTEN_PORT": "16000",
        }
    }
    plans = render.role_settings(config)
    validate_instance_ports(config, env, plans)
    with pytest.raises(ValueError, match="65535"):
        plans["decode"]["replicas"] = 8
        validate_instance_ports(config, env, plans)


def test_heterogeneous_gang_strategy_is_rejected(config, facts, tmp_path):
    config["motor_deploy_config"]["hardware_type"] = C.HARDWARE_TYPE_800I_A3
    config["motor_deploy_config"]["single_d_instance_pod_num"] = 4
    with pytest.raises(ValueError, match="different per-instance"):
        generate(config, facts, tmp_path)


def test_discovery_uses_labels_and_uids_not_name_prefix(monkeypatch):
    import json
    import shutil

    from lib.tui import step as discovery

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/kubectl")

    pods = [
        {
            "metadata": {
                "name": "any-name",
                "uid": "new-uid",
                "labels": {"rbg.workloads.x-k8s.io/role-name": "prefill"},
            },
            "status": {"phase": "Running"},
        },
        {
            "metadata": {
                "name": "vllm-controller",
                "uid": "control",
                "labels": {"rbg.workloads.x-k8s.io/role-name": "controller"},
            },
            "status": {"phase": "Running"},
        },
    ]
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps({"items": pods}))

    monkeypatch.setattr(discovery.subprocess, "run", run)
    assert discovery.engine_pods("ns", "model") == {"any-name": "new-uid"}
    assert "rbg.workloads.x-k8s.io/group-name=model" in calls[0]


def test_native_resources_match_local_rbg_schema(config, facts, tmp_path):
    import os
    from pathlib import Path

    source = os.environ.get("RBG_SOURCE")
    if not source:
        pytest.skip("Set RBG_SOURCE to validate against the installed RBG source CRD")
    jsonschema = pytest.importorskip("jsonschema")
    _, bundle = generate(config, facts, tmp_path)
    crd = yaml.safe_load(
        (Path(source) / "config/crd/bases/workloads.x-k8s.io_rolebasedgroups.yaml").read_text(encoding="utf-8")
    )
    version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1alpha2")
    jsonschema.Draft7Validator(version["schema"]["openAPIV3Schema"]).validate(bundle["rbg"])


def test_update_scheduling_queue_cannot_silently_change(config, facts, tmp_path):
    config["motor_deploy_config"][C.SCHEDULING_QUEUE] = "batch"
    _, bundle = generate(config, facts, tmp_path)
    assert (
        bundle["rbg"]["metadata"]["annotations"]["rbg.workloads.x-k8s.io/group-gang-scheduling-volcano-queue"]
        == "batch"
    )
    facts.update(
        rbg=bundle["rbg"],
        runtime_configmap=next(d for d in bundle["prerequisites"] if d["kind"] == "ConfigMap"),
    )
    config["motor_deploy_config"][C.SCHEDULING_QUEUE] = "other"
    with pytest.raises(ValueError, match="queue"):
        generate(config, facts, tmp_path)


def test_runtime_timeout_and_sidecar_are_rendered_without_mutating_input(config, facts, tmp_path):
    config["motor_coordinator_config"]["render_config"] = {
        "enable": True,
        "endpoint": {"host": "127.0.0.1", "port": 8110},
        "image_name": "render:test",
    }
    before = deepcopy(config)
    _, bundle = generate(
        config, facts, tmp_path, {"motor_engine_prefill_env": {"VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT": "45"}}
    )
    assert config == before
    assert bundle["user_config"]["motor_coordinator_config"]["exception_config"]["decode_first_token_timeout"] == 45
    coordinator = next(r for r in bundle["rbg"]["spec"]["roles"] if r["name"] == "coordinator")
    assert any(c["image"] == "render:test" for c in pod_template(coordinator)["spec"]["containers"])


def test_storage_mounts_preserve_external_claim_ownership(config, facts, tmp_path):
    config["motor_deploy_config"]["storage"] = [
        {"type": "pvc", "claim_name": "external-cache", "mount_path": "/cache"},
        {"type": "pvc", "storage_class_name": "shared", "mount_path": "/managed"},
    ]
    _, bundle = generate(config, facts, tmp_path)
    claims = [d["metadata"]["name"] for d in bundle["prerequisites"] if d["kind"] == "PersistentVolumeClaim"]
    assert claims == ["mindie-motor-store-1"]
    engine = next(r for r in bundle["rbg"]["spec"]["roles"] if r["name"] == "prefill")
    volumes = pod_template(engine)["spec"]["volumes"]
    assert any(v.get("persistentVolumeClaim", {}).get("claimName") == "external-cache" for v in volumes)


def test_business_service_selectors_match_role_pods(config, facts, tmp_path):
    config["motor_engine_prefill_config"]["engine_type"] = "sglang"
    config["motor_engine_decode_config"]["engine_type"] = "sglang"
    _, bundle = generate(config, facts, tmp_path)
    roles = {r["name"]: r for r in bundle["rbg"]["spec"]["roles"]}
    assert "mf-store" in roles
    for service in bundle["services"]:
        role = roles[service["metadata"]["labels"]["rbg.workloads.x-k8s.io/role-name"]]
        labels = pod_template(role)["metadata"]["labels"]
        assert service["spec"]["selector"].items() <= labels.items()
    store = next(s for s in bundle["services"] if s["metadata"]["labels"][C.RBG_ROLE_LABEL] == "mf-store")
    port = store["spec"]["ports"][0]["port"]
    for name in ("prefill", "decode", "coordinator", "mf-store"):
        container = pod_template(roles[name])["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        if name == "mf-store":
            assert env[C.ENV_ASCEND_MF_STORE_PORT] == str(port)
        else:
            assert env[C.ENV_ASCEND_MF_STORE_URL].endswith(f":{port}")


def test_union_and_control_standby_have_correct_replica_units(config, facts, tmp_path):
    deploy = config["motor_deploy_config"]
    for key in ("p_instances_num", "d_instances_num", "single_p_instance_pod_num", "single_d_instance_pod_num"):
        del deploy[key]
    deploy.update(hybrid_instances_num=3, single_hybrid_instance_pod_num=4, hybrid_pod_npu_num=8)
    config["motor_engine_union_config"] = config.pop("motor_engine_prefill_config")
    config.pop("motor_engine_decode_config")
    config["motor_controller_config"]["standby_config"] = {"enable_master_standby": True}
    _, bundle = generate(config, facts, tmp_path, env={"motor_engine_prefill_env": {"ASCEND_BASE_PORT": "20000"}})
    roles = {r["name"]: r for r in bundle["rbg"]["spec"]["roles"]}
    assert roles["prefill"]["replicas"] == roles["decode"]["replicas"] == 0
    assert roles["union"]["replicas"] == 3
    assert roles["union"]["customComponentsPattern"]["components"][0]["size"] == 4
    assert roles["controller"]["replicas"] == 2
    assert roles["controller"]["rolloutStrategy"]["rollingUpdate"]["maxUnavailable"] == 2

    env = {item["name"]: item for item in pod_template(roles["union"])["spec"]["containers"][0]["env"]}
    assert env["ASCEND_BASE_PORT"]["value"] == "20000"


def test_policy_trigger_and_metrics_select_only_intended_services(config, facts, tmp_path):
    context, bundle = generate(config, facts, tmp_path)
    assert f"mindie-motor-service-{context['logical_service_name']}-0-controller" in {
        s["metadata"]["name"] for s in bundle["services"]
    }
    metrics = [s for s in bundle["services"] if s["metadata"]["labels"].get("app") == "mindie-motor-coordinator"]
    assert len(metrics) == 1
    assert metrics[0]["spec"]["ports"][0]["port"] == 1027


def test_long_rbg_name_is_rejected_before_native_service_creation(config, facts, tmp_path):
    config["motor_deploy_config"]["rbg_config"] = {"name": "a" * 63}
    with pytest.raises(ValueError, match="native role Service name"):
        generate(config, facts, tmp_path)


def test_rbg_template_is_independent_and_user_config_overrides_defaults(config, facts, tmp_path, monkeypatch):
    original_load = render.load_documents

    def load(filename):
        assert filename == "rbg_template.yaml", "RBG must not load the InferServiceSet template"
        documents = original_load(filename)
        rbg = next(doc for doc in documents if doc["kind"] == "RoleBasedGroup")
        controller = next(role for role in rbg["spec"]["roles"] if role["name"] == "controller")
        template = pod_template(controller)
        template["spec"]["terminationGracePeriodSeconds"] = 37
        template["spec"]["containers"][0]["image"] = "template-default"
        service = next(doc for doc in documents if doc["kind"] == "Service")
        service["spec"]["sessionAffinity"] = "ClientIP"
        return documents

    monkeypatch.setattr(render, "load_documents", load)
    _, resources = generate(config, facts, tmp_path)
    controller = next(role for role in resources["rbg"]["spec"]["roles"] if role["name"] == "controller")
    assert pod_template(controller)["spec"]["terminationGracePeriodSeconds"] == 37
    assert pod_template(controller)["spec"]["containers"][0]["image"] == "motor:test"
    assert resources["services"][0]["spec"]["sessionAffinity"] == "ClientIP"


@pytest.mark.parametrize("role", ["controller", "prefill"])
def test_missing_required_template_role_has_explicit_error(config, facts, tmp_path, monkeypatch, role):
    original = render._load_templates

    def load():
        rbg, catalog, services, rbac = original()
        del catalog[role]
        return rbg, catalog, services, rbac

    monkeypatch.setattr(render, "_load_templates", load)
    with pytest.raises(ValueError, match=f"missing required roles:.*{role}"):
        generate(config, facts, tmp_path)


@pytest.fixture
def leader_worker_template(monkeypatch):
    original = render._load_templates

    def load():
        rbg, catalog, services, rbac = original()
        for name in ("prefill", "decode"):
            role = catalog[name]
            custom = role.pop("customComponentsPattern")
            role["leaderWorkerPattern"] = {
                "size": 99,
                "template": custom["components"][0]["template"],
                "restartPolicyConfig": {"type": "None", "baseDelaySeconds": 5},
                "leaderTemplatePatch": {"metadata": {"annotations": {"example.com/member": "leader"}}},
                "workerTemplatePatch": {"metadata": {"annotations": {"example.com/member": "worker"}}},
                "sharedServiceSelection": "All",
            }
        return rbg, catalog, services, rbac

    monkeypatch.setattr(render, "_load_templates", load)


@pytest.mark.parametrize("size", [1, 3])
def test_leader_worker_renders_total_size_shared_identity_and_native_patches(
    config, facts, tmp_path, leader_worker_template, size
):
    config["motor_deploy_config"]["single_p_instance_pod_num"] = size
    config["motor_controller_config"]["fault_tolerance_config"] = {"enable_dp_scale_down": True}
    config["motor_engine_decode_config"]["engine_config"]["enable_fault_tolerance"] = True
    _, bundle = generate(config, facts, tmp_path)
    roles = {role["name"]: role for role in bundle["rbg"]["spec"]["roles"]}
    pattern = roles["prefill"]["leaderWorkerPattern"]
    assert pattern["size"] == size  # Includes the leader; workers are size - 1.
    assert roles["prefill"]["replicas"] == 2
    assert pattern["restartPolicyConfig"] == {
        "type": "RecreateRoleInstanceOnPodRestart",
        "baseDelaySeconds": 5,
        "maxDelaySeconds": 600,
    }
    assert roles["decode"]["leaderWorkerPattern"]["restartPolicyConfig"]["type"] == "None"
    assert pattern["sharedServiceSelection"] == "All"
    assert pattern["leaderTemplatePatch"]["metadata"]["annotations"]["example.com/member"] == "leader"
    assert pattern["workerTemplatePatch"]["metadata"]["annotations"]["example.com/member"] == "worker"
    template = pattern["template"]
    container = template["spec"]["containers"][0]
    assert container["image"] == "motor:test"
    assert template["spec"]["schedulerName"] == "volcano"
    assert "motor-deployer/startup-digest" in template["metadata"]["annotations"]
    env = {item["name"]: item for item in container["env"]}
    assert env["ROLE"]["value"] == env["INSTANCE_ROLE"]["value"] == "prefill"
    assert env["INSTANCE_INDEX"]["valueFrom"]["fieldRef"]["fieldPath"] == f"metadata.labels['{C.RBG_INDEX_LABEL}']"

    facts.update(
        rbg=bundle["rbg"],
        runtime_configmap=next(doc for doc in bundle["prerequisites"] if doc["kind"] == "ConfigMap"),
    )
    _, repeated = generate(config, facts, tmp_path)
    assert repeated["rbg"] == bundle["rbg"]
    config["motor_deploy_config"]["p_instances_num"] = 3
    _, scaled = generate(config, facts, tmp_path)
    scaled_p = next(role for role in scaled["rbg"]["spec"]["roles"] if role["name"] == "prefill")
    assert scaled_p["replicas"] == 3
    assert scaled_p["leaderWorkerPattern"] == pattern


@pytest.mark.parametrize("change", ["pattern", "size", "restart"])
def test_leader_worker_topology_changes_require_redeployment(config, facts, tmp_path, leader_worker_template, change):
    _, first = generate(config, facts, tmp_path)
    facts.update(
        rbg=first["rbg"],
        runtime_configmap=next(doc for doc in first["prerequisites"] if doc["kind"] == "ConfigMap"),
    )
    old = next(role for role in facts["rbg"]["spec"]["roles"] if role["name"] == "prefill")
    if change == "pattern":
        pattern = old.pop("leaderWorkerPattern")
        old["standalonePattern"] = {"template": pattern["template"]}
    elif change == "size":
        config["motor_deploy_config"]["single_p_instance_pod_num"] = 4
    else:
        old["leaderWorkerPattern"]["restartPolicyConfig"]["type"] = "None"
    with pytest.raises(ValueError, match="requires redeployment"):
        generate(config, facts, tmp_path)


def test_full_deployment_updates_roles_without_replacing_group(config, facts):
    initial = render.generate_rbg_resources(config, {}, facts=facts)
    roles = {role["name"]: role for role in initial["rbg"]["spec"]["roles"]}
    assert roles["encode"]["replicas"] == roles["union"]["replicas"] == 0
    facts["rbg"] = initial["rbg"]
    facts["runtime_configmap"] = next(obj for obj in initial["prerequisites"] if obj["kind"] == "ConfigMap")
    config["motor_deploy_config"]["e_instances_num"] = 1
    config["motor_engine_encode_config"] = deepcopy(config["motor_engine_prefill_config"])
    updated = render.generate_rbg_resources(config, {}, facts=facts)
    assert next(r for r in updated["rbg"]["spec"]["roles"] if r["name"] == "encode")["replicas"] == 1
    facts["rbg"] = updated["rbg"]
    config["motor_deploy_config"].pop("p_instances_num")
    config["motor_deploy_config"].pop("d_instances_num")
    config["motor_deploy_config"].pop("e_instances_num")
    config["motor_deploy_config"]["hybrid_instances_num"] = 1
    config["motor_engine_union_config"] = deepcopy(config["motor_engine_prefill_config"])
    updated = render.generate_rbg_resources(config, {}, facts=facts)
    roles = {role["name"]: role for role in updated["rbg"]["spec"]["roles"]}
    assert roles["union"]["replicas"] == 1
    assert all(roles[name]["replicas"] == 0 for name in ("prefill", "decode", "encode"))
    assert set(roles) == {r["name"] for r in initial["rbg"]["spec"]["roles"]}


def test_mode_switch_restarts_control_plane_but_scaling_does_not(config):
    data = render.render_configmap(render.deployment_settings(config), config, {}, "test-identity")["data"]
    before = render.startup_digest(data, config, "coordinator")
    config["motor_deploy_config"]["p_instances_num"] = 3
    assert render.startup_digest(data, config, "coordinator") == before
    config["motor_deploy_config"]["hybrid_instances_num"] = 1
    hybrid = render.startup_digest(data, config, "coordinator")
    assert hybrid != before
    config["motor_deploy_config"]["hybrid_instances_num"] = 2
    assert render.startup_digest(data, config, "coordinator") == hybrid


def test_control_standby_can_be_enabled_by_full_update(config, facts):
    initial = render.generate_rbg_resources(config, {}, facts=facts)
    facts.update(
        rbg=initial["rbg"],
        runtime_configmap=next(obj for obj in initial["prerequisites"] if obj["kind"] == "ConfigMap"),
    )
    config["motor_controller_config"]["standby_config"] = {"enable_master_standby": True}
    updated = render.generate_rbg_resources(config, {}, facts=facts)
    controller = next(role for role in updated["rbg"]["spec"]["roles"] if role["name"] == "controller")
    assert controller["replicas"] == 2
    assert controller["rolloutStrategy"]["rollingUpdate"]["maxUnavailable"] == 2


@pytest.mark.parametrize(
    "key",
    [
        "MOTOR_INSTANCE_PORT_STRIDE",
        "MOTOR_INSTANCE_ASCEND_PORT_STRIDE",
        "MOTOR_INSTANCE_STORE_PORT_STRIDE",
        "MOTOR_INSTANCE_KV_PORT_STRIDE",
    ],
)
def test_instance_port_stride_overflow_rejected_even_for_first_instance(key):
    from lib.rbg.config import validate_instance_ports

    config = {
        "motor_engine_prefill_config": {
            "motor_nodemanger_config": {
                "port_allocator_config": {"enable": False},
                "api_config": {"node_manager_port": 1026},
                "endpoint_config": {"base_port": 8000},
            },
            "engine_config": {"data_parallel_rpc_port": 9000},
        }
    }
    values = {
        "MOTOR_INSTANCE_PORTS_ENABLE": "1",
        "ASCEND_BASE_PORT": "12000",
        "HIXL_LISTEN_PORT": "16000",
        key: "70000",
    }
    with pytest.raises(ValueError, match=key):
        validate_instance_ports(
            config, {"motor_engine_prefill_env": values}, {"prefill": {"name": "prefill", "replicas": 1}}
        )


def test_standby_roles_are_separate_single_pod_instances_with_isolated_services(config, facts):
    for component in ("controller", "coordinator"):
        config[f"motor_{component}_config"]["standby_config"] = {"enable_master_standby": True}
    bundle = render.generate_rbg_resources(config, {}, facts=facts)
    roles = {role["name"]: role for role in bundle["rbg"]["spec"]["roles"]}
    for component in ("controller", "coordinator"):
        role = roles[component]
        assert role["replicas"] == 2
        assert "standalonePattern" in role and "customComponentsPattern" not in role
        template = pod_template(role)
        container = template["spec"]["containers"][0]
        assert "readinessProbe" in container and "livenessProbe" in container
        for service in bundle["services"]:
            if service["metadata"]["labels"][C.RBG_ROLE_LABEL] == component:
                selector = service["spec"]["selector"]
                assert selector[C.RBG_GROUP_LABEL] == bundle["rbg"]["metadata"]["name"]
                assert selector[C.RBG_ROLE_LABEL] == component
                assert not service["spec"].get("publishNotReadyAddresses", False)
