# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import os
import shlex
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import deploy as deploy_module
import pytest
from lib.generator.infer_service import (
    _find_infer_service_set_doc,
    generate_yaml_infer_service_set,
)
from lib.utils import load_yaml
from startup.log_paths import prepare_paths, resolve_paths, validate_root

DEPLOYER = Path(__file__).resolve().parents[3] / "examples/deployer"


def _environment(tmp_path):
    return {
        "MOTOR_LOG_ROOT_PATH": str(tmp_path / "logs"),
        "MODEL_NAME": "qwen",
        "SERVICE_ID": "run",
        "COMPUTE_UNIT_LOG_SUBPATH": "vllm-0-prefill-0-0/vllm",
        "engine_type": "vllm",
        "HOME": str(tmp_path / "home"),
    }


def _user_config():
    return {
        "motor_deploy_config": {
            "job_id": "test-logs",
            "deploy_mode": "infer_service_set",
            "image_name": "test:image",
            "hardware_type": "800I_A2",
            "p_instances_num": 1,
            "d_instances_num": 1,
            "single_p_instance_pod_num": 1,
            "single_d_instance_pod_num": 1,
            "p_pod_npu_num": 1,
            "d_pod_npu_num": 1,
        },
        "motor_controller_config": {},
        "motor_coordinator_config": {},
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "engine_config": {"served_model_name": "qwen"},
        },
        "motor_engine_decode_config": {"engine_type": "vllm"},
    }


@pytest.mark.parametrize("engine_type", ["vllm", "sglang"])
@pytest.mark.parametrize("role", ["encode", "prefill", "decode", "union"])
def test_compute_unit_paths_are_unique_and_reused_for_the_same_identity(tmp_path, engine_type, role):
    environment = {**_environment(tmp_path), "engine_type": engine_type}
    paths = resolve_paths(environment, role=role)
    expected = tmp_path / "logs/qwen/run/vllm-0-prefill-0-0/vllm"
    assert paths["SERVICE_ENGINE_LOG_PATH"] == str(expected / engine_type)
    assert paths["MOTOR_LOG_PATH"] == str(expected / "motor")
    assert paths["ASCEND_WORK_PATH"] == str(expected / "cann/work")
    assert paths["ASCEND_CACHE_PATH"] == str(expected / "cann/cache")
    assert paths["TORCHINDUCTOR_CACHE_DIR"] == str(expected / "cann/torchinductor")
    if engine_type == "vllm":
        assert paths["XDG_CACHE_HOME"] == str(expected / "vllm/xdg/cache")
        assert paths["XDG_CONFIG_HOME"] == str(expected / "vllm/xdg/config")
    assert paths["TRITON_HOME"] == str(expected / "triton")
    assert resolve_paths({**environment, "HOSTNAME": "restarted-container"}, role=role) == paths
    for subpath in (
        "vllm-0-prefill-1-0/vllm",
        "vllm-0-prefill-0-0/other-engine",
    ):
        changed = resolve_paths({**environment, "COMPUTE_UNIT_LOG_SUBPATH": subpath}, role=role)
        assert changed["MOTOR_LOG_PATH"] != paths["MOTOR_LOG_PATH"]
        assert changed["SERVICE_ENGINE_LOG_PATH"] != paths["SERVICE_ENGINE_LOG_PATH"]
        assert changed["TRITON_HOME"] != paths["TRITON_HOME"]
        assert changed["TORCHINDUCTOR_CACHE_DIR"] != paths["TORCHINDUCTOR_CACHE_DIR"]
        assert changed["TRITON_HOME"] == str(Path(changed["SERVICE_ENGINE_LOG_PATH"]).parent / "triton")


@pytest.mark.parametrize("missing", ["MODEL_NAME", "SERVICE_ID"])
def test_engine_without_complete_identity_preserves_existing_paths(tmp_path, missing):
    environment = _environment(tmp_path)
    del environment[missing]
    environment["MOTOR_LOG_PATH"] = str(tmp_path / "existing")
    original = environment.copy()
    paths = resolve_paths(environment, role="prefill")
    prepare_paths(paths, environment)
    assert paths == {}
    assert environment == original
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("role", ["controller", "coordinator"])
@pytest.mark.parametrize("subpath", [None, "motor-test", "other-container"])
def test_control_motor_paths_are_isolated_by_role_regardless_of_compute_identity(tmp_path, role, subpath):
    environment = _environment(tmp_path)
    if subpath is None:
        environment.pop("COMPUTE_UNIT_LOG_SUBPATH")
    else:
        environment["COMPUTE_UNIT_LOG_SUBPATH"] = subpath
    paths = resolve_paths(environment, role=role)
    assert paths["MOTOR_LOG_PATH"] == str(tmp_path / "logs/qwen/run/motor-control" / role)
    assert "SERVICE_ENGINE_LOG_PATH" not in paths
    assert "TORCHINDUCTOR_CACHE_DIR" not in paths


@pytest.mark.parametrize("subpath", ["/", "/outside", ".", "..", "worker/../escape", "unit\n0", "unit\x000"])
def test_compute_unit_subpath_cannot_escape_service_root(tmp_path, subpath):
    with pytest.raises(ValueError, match="COMPUTE_UNIT_LOG_SUBPATH"):
        resolve_paths({**_environment(tmp_path), "COMPUTE_UNIT_LOG_SUBPATH": subpath}, role="prefill")


@pytest.mark.parametrize("value", ["/", "relative/path", "/data/../etc", "/data\nlogs"])
def test_root_rejects_unsafe_or_relative_paths(value):
    with pytest.raises(ValueError, match="MOTOR_LOG_ROOT_PATH"):
        validate_root(value)


@pytest.mark.parametrize(
    "name",
    [
        "MOTOR_LOG_PATH",
        "ASCEND_WORK_PATH",
        "ASCEND_CACHE_PATH",
        "TORCHINDUCTOR_CACHE_DIR",
        "TRITON_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    ],
)
def test_explicit_role_paths_are_preserved_and_still_validated(tmp_path, capsys, name):
    environment = _environment(tmp_path)
    custom = str(tmp_path / "explicit" / name.lower())
    assert resolve_paths({**environment, name: custom}, role="prefill")[name] == custom
    assert name in capsys.readouterr().err
    default = resolve_paths(environment, role="prefill")[name]
    for empty in (None, ""):
        assert resolve_paths({**environment, name: empty}, role="prefill")[name] == default
    with pytest.raises(ValueError, match=name):
        resolve_paths({**environment, name: "/logs/../outside"}, role="prefill")


@pytest.mark.parametrize("hybrid", [False, True])
def test_single_container_script_isolates_paths_and_preserves_role_environment(tmp_path, hybrid):
    deployment, has_subpath = ("docker", False) if hybrid else ("k8s", True)
    environment = _environment(tmp_path)
    if deployment == "docker":
        environment["model_name"] = environment.pop("MODEL_NAME")
        environment["service_id"] = environment.pop("SERVICE_ID")
        environment["COMPUTE_UNIT_LOG_SUBPATH"] = "single-container"
    if not has_subpath:
        del environment["COMPUTE_UNIT_LOG_SUBPATH"]
    subpath = environment.get("COMPUTE_UNIT_LOG_SUBPATH", "")
    container_root = tmp_path / "logs/qwen/run" / subpath
    captured = tmp_path / "captured"
    captured.mkdir()
    capture = tmp_path / "capture.py"
    # Run the real helper with Linux stdout line endings on Windows too.
    capture.write_text(
        "import json, os, runpy, sys\nfrom pathlib import Path\n"
        "if sys.argv[1] != '-m':\n"
        "    sys.stdout.reconfigure(newline='\\n')\n"
        "    sys.argv = sys.argv[1:]\n"
        "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
        "    sys.exit(0)\n"
        "if sys.argv[2] == 'motor.common.utils.single_container_ports':\n"
        "    Path(os.environ['USER_CONFIG_PATH']).with_name('single_container_ports.env').touch()\n"
        "    sys.exit(0)\n"
        "role = os.environ['ROLE']\nindex = os.environ.get('INDEX', '0')\n"
        "if sys.argv[2] == 'ccae_reporter.run':\n    role = 'reporter-' + sys.argv[3].lower()\n"
        "elif sys.argv[2] == 'parent':\n    role = 'parent'\n"
        "target = Path(os.environ['CAPTURE_DIR']) / (role + '-' + index + '.json')\n"
        "target.write_text(json.dumps(dict(os.environ)))\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python = fake_bin / "python3"
    python.write_text(
        f"#!/bin/bash\nexec {shlex.quote(sys.executable)} {shlex.quote(str(capture))} \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    python.chmod(0o750)
    kv_store = fake_bin / "mooncake_master"
    kv_store.write_text(
        f"#!/bin/bash\nexec {shlex.quote(sys.executable)} {shlex.quote(str(capture))} -m kv_store \"$@\"\n",
        encoding="utf-8",
        newline="\n",
    )
    kv_store.chmod(0o750)
    config = tmp_path / "user_config.json"
    config.write_text(
        json.dumps(
            {"motor_engine_union_config": {}, "hybrid_instances_num": 2}
            if hybrid
            else {"p_instances_num": 2, "d_instances_num": 1},
            indent=2,
        )
    )
    common = (DEPLOYER / "startup/common.sh").read_text()
    functions = common[common.index("setup_log_paths()") : common.index("apply_patches()")]
    stubs = "\n".join(
        f"{name}() {{ :; }}"
        for name in (
            "set_cann_env",
            "apply_patches",
            "set_prefill_env",
            "set_decode_env",
            "set_union_env",
            "gen_kv_store_config",
            "set_kv_store_env",
            "motor_track_child",
            "motor_track_helper",
        )
    )
    stubs += '\nis_a5_hardware() { return 1; }\n'
    stubs += 'set_coordinator_env() { export COORDINATOR_ENV_MARKER=coordinator; }\n'
    stubs += 'set_controller_env() { export CONTROLLER_ENV_MARKER=controller; }\n'
    stubs += 'motor_supervise_children() { wait; python3 -m parent; }\n'
    env = {
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        **environment,
        "ROLE": "SINGLE_CONTAINER",
        "CONFIGMAP_PATH": str(DEPLOYER / "startup"),
        "SCRIPT_DIR": str(DEPLOYER / "startup"),
        "CONFIG_PATH": str(tmp_path),
        "USER_CONFIG_PATH": config.as_posix(),
        "CAPTURE_DIR": str(captured),
        "KV_STORE_BACKEND": "mooncake",
    }
    subprocess.run(
        ["bash"],
        input=functions + stubs + (DEPLOYER / "startup/roles/all_combine_in_single_container.sh").read_text(),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    observed = {file.stem: json.loads(file.read_text()) for file in captured.glob("*.json")}
    engines = {"union-0", "union-1"} if hybrid else {"prefill-0", "prefill-1", "decode-0"}
    assert (
        set(observed)
        == {
            "controller-0",
            "coordinator-0",
            "kv_store-0",
            "reporter-controller-0",
            "reporter-coordinator-0",
            "parent-0",
        }
        | engines
    )
    for name in ("kv_store-0", "reporter-controller-0", "reporter-coordinator-0", "parent-0"):
        assert "MOTOR_LOG_PATH" not in observed[name]
        assert "SERVICE_ENGINE_LOG_PATH" not in observed[name]
        assert "TORCHINDUCTOR_CACHE_DIR" not in observed[name]
    for role in ("controller", "coordinator"):
        assert observed[f"{role}-0"]["MOTOR_LOG_PATH"] == str(tmp_path / "logs/qwen/run/motor-control" / role)
        assert "SERVICE_ENGINE_LOG_PATH" not in observed[f"{role}-0"]
        assert "TORCHINDUCTOR_CACHE_DIR" not in observed[f"{role}-0"]
    assert observed["kv_store-0"]["COORDINATOR_ENV_MARKER"] == "coordinator"
    assert observed["kv_store-0"]["CONTROLLER_ENV_MARKER"] == "controller"
    for name in engines:
        instance_root = container_root / name
        assert observed[name]["SERVICE_ENGINE_LOG_PATH"] == str(instance_root / "vllm")
        assert observed[name]["MOTOR_LOG_PATH"] == str(instance_root / "motor")
        assert observed[name]["ASCEND_WORK_PATH"] == str(instance_root / "cann/work")
        assert observed[name]["ASCEND_CACHE_PATH"] == str(instance_root / "cann/cache")
        assert observed[name]["TORCHINDUCTOR_CACHE_DIR"] == str(instance_root / "cann/torchinductor")
        assert observed[name]["TRITON_HOME"] == str(instance_root / "triton")
        assert observed[name]["COMPUTE_UNIT_LOG_SUBPATH"] == (f"{subpath}/{name}" if subpath else name)
        assert observed[name]["XDG_CACHE_HOME"] == str(instance_root / "vllm/xdg/cache")
        assert observed[name]["XDG_CONFIG_HOME"] == str(instance_root / "vllm/xdg/config")
        role, index = name.rsplit("-", 1)
        job = {"prefill": "p", "decode": "d", "union": "u"}[role] + index
        assert observed[name]["JOB_NAME"] == job


def test_role_environment_is_applied_before_managed_log_paths(tmp_path):
    environment = _environment(tmp_path)
    script = (DEPLOYER / "startup/roles/engine.sh").read_text()
    common = (DEPLOYER / "startup/common.sh").read_text()
    functions = common[common.index("setup_log_paths()") : common.index("apply_patches()")]
    stub = "\n".join(
        f"{name}() {{ :; }}"
        for name in (
            "set_cann_env",
            "set_mf_store_env",
            "apply_patches",
            "apply_instance_port_env",
            "set_container_snapshot_engine_env",
        )
    )
    stub += "\nis_a5_hardware() { return 1; }\nROLE=prefill\n"
    stub += 'set_prefill_env() { export MOTOR_LOG_ROOT_PATH="$ROLE_ROOT"; }\n'
    stub += 'python3() { if [ "$1" = "-m" ]; then printf "%s\\n" "$MOTOR_LOG_PATH"; else command python3 "$@"; fi; }\n'
    env = {
        "PATH": os.environ["PATH"],
        **environment,
        "ROLE_ROOT": str(tmp_path / "role-root"),
        "CONFIGMAP_PATH": str(DEPLOYER / "startup"),
        "SCRIPT_DIR": str(DEPLOYER / "startup"),
    }
    result = subprocess.run(
        ["bash"],
        input=functions + stub + script,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    expected = tmp_path / "role-root/qwen/run/vllm-0-prefill-0-0/vllm"
    assert str(expected / "motor") in result.stdout
    assert (expected / "cann/work").is_dir()


@pytest.mark.parametrize("mode", ["multi_deployment", "single_container"])
def test_k8s_compute_identity_preserves_existing_mounts(tmp_path, monkeypatch, mode):
    from lib.generator import k8s_utils

    user = _user_config()
    user["motor_deploy_config"]["deploy_mode"] = mode
    user["motor_controller_config"]["logging_config"] = {"host_log_dir": "/host/controller"}
    user["motor_nodemanger_config"] = {"logging_config": {"host_log_dir": "/host/engine"}}
    before = deepcopy(user)
    monkeypatch.setattr(k8s_utils, "g_generate_yaml_list", [])
    monkeypatch.setattr(k8s_utils, "g_kv_store_enabled", False)
    monkeypatch.setattr(k8s_utils, "g_kv_conductor_enabled", False)
    monkeypatch.setattr(k8s_utils, "g_mf_store_enabled", False)
    monkeypatch.setattr(deploy_module, "init_service_domain_name", lambda *args, **kwargs: None)
    paths = {
        f"{role}_input_yaml": str(DEPLOYER / f"yaml_template/{role}_template.yaml")
        for role in ("controller", "coordinator", "engine", "single_container")
    }
    paths.update(
        {
            f"{role}_output_yaml": str(tmp_path / f"{role}.yaml")
            for role in ("controller", "coordinator", "engine", "single_container")
        }
    )
    if mode == "single_container":
        deploy_module.deploy_services_single_container(paths, user, dry_run=True)
        generated = [paths["single_container_output_yaml"]]
    else:
        deploy_module.deploy_services_multi_yaml(paths, user, dry_run=True)
        generated = k8s_utils.g_generate_yaml_list
        assert len(generated) == 4
    pod_count = 0
    for path in generated:
        for document in load_yaml(path, False):
            if document.get("kind") != "Deployment":
                continue
            pod_count += 1
            pod = document["spec"]["template"]["spec"]
            volume = next(v for v in pod["volumes"] if v["name"] == "plog-path")
            for container in pod["containers"]:
                env = {item["name"]: item for item in container["env"]}
                role = env["ROLE"]["value"]
                expected_host = "/root/ascend/log"
                if mode == "multi_deployment":
                    expected_host = {
                        "controller": "/host/controller",
                        "prefill": "/host/engine",
                        "decode": "/host/engine",
                    }.get(role, expected_host)
                assert volume["hostPath"]["path"] == expected_host
                assert "SERVICE_ID" not in env
                assert "MOTOR_LOG_ROOT_PATH" not in env
                if mode == "single_container" or env["ROLE"]["value"] in ("prefill", "decode"):
                    assert env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
                    assert env["COMPUTE_UNIT_LOG_SUBPATH"]["value"] == f"$(POD_NAME)/{container['name']}"
                    names = [item["name"] for item in container["env"]]
                    assert names.index("POD_NAME") < names.index("COMPUTE_UNIT_LOG_SUBPATH")
                mounts = [mount for mount in container["volumeMounts"] if mount["name"] == "plog-path"]
                assert mounts == [{"name": "plog-path", "mountPath": "/root/ascend/log"}]
    assert pod_count == (1 if mode == "single_container" else 4)
    assert user == before


@pytest.mark.parametrize("engine_type", ["vllm", "sglang"])
def test_iss_keeps_fixed_mounts_and_computes_identity_only_for_engine_roles(tmp_path, engine_type):
    user = _user_config()
    user["motor_engine_prefill_config"]["engine_type"] = engine_type
    user["motor_engine_decode_config"]["engine_type"] = engine_type
    original = deepcopy(user)
    output = tmp_path / "infer.yaml"
    generate_yaml_infer_service_set(str(DEPLOYER / "yaml_template/infer_service_template.yaml"), str(output), user)
    document = _find_infer_service_set_doc(load_yaml(str(output), False))
    for role in document["spec"]["template"]["roles"]:
        if role["name"] not in ("controller", "coordinator", "encode", "prefill", "decode", "union"):
            continue
        pod = role["spec"]["template"]["spec"]
        volume = next(v for v in pod["volumes"] if v["name"] == "plog-path")
        assert volume["hostPath"] == {"path": "/root/ascend/log", "type": "DirectoryOrCreate"}
        for container in pod["containers"]:
            mounts = [m for m in container["volumeMounts"] if m["name"] == "plog-path"]
            assert mounts == [{"name": "plog-path", "mountPath": "/root/ascend/log"}]
            env = {item["name"]: item for item in container["env"]}
            assert "MODEL_NAME" not in env and "SERVICE_ID" not in env
            assert "MOTOR_LOG_ROOT_PATH" not in env
            if role["name"] not in ("controller", "coordinator"):
                assert env["COMPUTE_UNIT_LOG_SUBPATH"]["value"] == f"$(POD_NAME)/{container['name']}"
                assert env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
                names = [item["name"] for item in container["env"]]
                assert names.index("POD_NAME") < names.index("COMPUTE_UNIT_LOG_SUBPATH")
    assert user == original


@pytest.mark.parametrize("mode", ["infer_service_set", "multi_deployment", "single_container"])
@pytest.mark.parametrize("explicit_host", [False, True])
def test_generated_workloads_mount_common_custom_log_root(tmp_path, monkeypatch, mode, explicit_host):
    from lib.generator import k8s_utils

    user = _user_config()
    user["motor_deploy_config"]["deploy_mode"] = mode
    host_paths = {"controller": "/host/controller", "coordinator": "/root/ascend/log", "nodemanger": "/host/engine"}
    if explicit_host:
        for role, host_path in host_paths.items():
            user.setdefault(f"motor_{role}_config", {})["logging_config"] = {"host_log_dir": host_path}
    monkeypatch.setattr(k8s_utils, "g_generate_yaml_list", [])
    monkeypatch.setattr(k8s_utils, "g_kv_store_enabled", False)
    monkeypatch.setattr(k8s_utils, "g_kv_conductor_enabled", False)
    monkeypatch.setattr(k8s_utils, "g_mf_store_enabled", False)
    monkeypatch.setattr(deploy_module, "init_service_domain_name", lambda *args, **kwargs: None)
    monkeypatch.setattr(deploy_module, "init_infer_service_domain_name", lambda *args, **kwargs: None)
    paths = {
        f"{name}_input_yaml": str(DEPLOYER / f"yaml_template/{name}_template.yaml")
        for name in ("controller", "coordinator", "engine", "single_container", "infer_service")
    }
    paths.update(
        {
            f"{name}_output_yaml": str(tmp_path / f"{name}.yaml")
            for name in ("controller", "coordinator", "engine", "single_container", "infer_service")
        }
    )
    if mode == "single_container":
        deploy_module.deploy_services_single_container(paths, user, dry_run=True, log_root="/custom/log")
        files = [paths["single_container_output_yaml"]]
    elif mode == "multi_deployment":
        deploy_module.deploy_services_multi_yaml(paths, user, dry_run=True, log_root="/custom/log")
        files = k8s_utils.g_generate_yaml_list
    else:
        deploy_module.deploy_services_infer_service_set(paths, user, dry_run=True, log_root="/custom/log")
        files = k8s_utils.g_generate_yaml_list

    def inspect(value):
        if isinstance(value, list):
            return sum((inspect(item) for item in value), 0)
        if not isinstance(value, dict):
            return 0
        found = 0
        if "containers" in value and "volumes" in value:
            volume = next((v for v in value["volumes"] if v.get("name") == "plog-path"), None)
            if volume:
                for container in value["containers"]:
                    env = {item["name"]: item.get("value") for item in container.get("env", [])}
                    expected_host = "/custom/log"
                    if explicit_host and mode != "single_container":
                        expected_host = host_paths.get(env.get("ROLE"), host_paths["nodemanger"])
                    assert volume["hostPath"]["path"] == expected_host
                    for mount in container.get("volumeMounts", []):
                        if mount.get("name") == "plog-path":
                            assert mount["mountPath"] == "/custom/log"
                found += 1
        return found + sum(inspect(item) for item in value.values())

    assert sum(inspect(load_yaml(path, False)) for path in files) >= 1


@pytest.mark.parametrize("names", [[None], [""], ["../escape"], ["Upper"], ["x" * 64], ["vllm", "vllm"]])
def test_compute_identity_rejects_invalid_or_duplicate_container_names(names):
    from lib.generator.k8s_utils import configure_pod_log_identity

    pod_spec = {"containers": [{"name": name} for name in names]}
    with pytest.raises(ValueError, match="container name"):
        configure_pod_log_identity(pod_spec)


@pytest.mark.parametrize("log_root", ["/root/ascend/log", "/custom/log"])
def test_rbg_engine_templates_mount_common_log_root(log_root):
    from lib.rbg.config import pod_template
    from lib.rbg.render import generate_rbg_resources

    user = _user_config()
    user["motor_deploy_config"]["deploy_mode"] = "rbg"
    resources = generate_rbg_resources(
        user,
        {"motor_common_env": {"MOTOR_LOG_ROOT_PATH": log_root}},
        facts={"accelerator_type": "module-910b-8"},
    )

    for name in ("prefill", "decode"):
        role = next(role for role in resources["rbg"]["spec"]["roles"] if role["name"] == name)
        spec = pod_template(role)["spec"]
        volume = next(volume for volume in spec["volumes"] if volume["name"] == "plog-path")
        mount = next(mount for mount in spec["containers"][0]["volumeMounts"] if mount["name"] == "plog-path")
        assert volume["hostPath"]["path"] == log_root
        assert mount["mountPath"] == log_root
