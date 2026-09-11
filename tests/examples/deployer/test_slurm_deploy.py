# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import argparse
import base64
from io import BytesIO
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path


DEPLOYER_DIR = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_DIR))

import slurm_deploy  # noqa: E402


def test_slurm_deploy_does_not_require_pyyaml_at_import():
    script = f"""
import builtins
import sys

sys.path.insert(0, {str(DEPLOYER_DIR)!r})
original_import = builtins.__import__

def import_without_yaml(name, *args, **kwargs):
    if name == "yaml" or name.startswith("yaml."):
        raise ImportError("PyYAML is unavailable")
    return original_import(name, *args, **kwargs)

builtins.__import__ = import_without_yaml
import slurm_deploy
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


def test_base_job_args_use_valid_long_output_options():
    args = slurm_deploy._base_job_args("test-partition", "prefill", 2, 16)

    assert "--chdir=/tmp" in args
    assert "--output=/dev/null" in args
    assert "--error=/dev/null" in args
    assert "-o=/dev/null" not in args
    assert "-e=/dev/null" not in args


def _user_config():
    return {
        "motor_deploy_config": {
            "deploy_mode": "multi_deployment",
            "job_id": "slurm-test",
            "image_name": "/shared/motor.sif",
            "weight_mount_path": "/shared/model",
            "hardware_type": "800I_A2",
            "p_instances_num": 1,
            "single_p_instance_pod_num": 2,
            "p_pod_npu_num": 8,
            "d_instances_num": 1,
            "single_d_instance_pod_num": 1,
            "d_pod_npu_num": 8,
        },
        "motor_engine_prefill_config": {
            "engine_type": "vllm",
            "engine_config": {"served_model_name": "test-model"},
        },
        "motor_engine_decode_config": {
            "engine_type": "vllm",
            "engine_config": {"served_model_name": "test-model"},
        },
        "north_config": {"name": "slurm"},
    }


def test_prepare_runs_once_on_host_and_copies_resolved_config(tmp_path, monkeypatch):
    source = tmp_path / "conf"
    configmap = tmp_path / "configmap"
    source.mkdir()
    user_config_path = source / "custom-user.json"
    env_path = source / "custom-env.json"
    user_config = _user_config()
    user_config["motor_deploy_config"].pop("deploy_mode")
    user_config_path.write_text(json.dumps(user_config), encoding="utf-8")
    env_path.write_text(
        json.dumps(
            {
                "motor_common_env": {},
                "motor_controller_env": {"SLURM_TEST_MARKER": "configured"},
                "motor_mf_store_env": {"MF_STORE_TEST_MARKER": "configured"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(slurm_deploy, "CONFIGMAP_PREPARE_PATH", configmap)

    slurm_deploy._prepare_configmap(str(user_config_path), str(env_path))

    common = (configmap / "common.sh").read_text(encoding="utf-8")
    controller = (configmap / "controller.sh").read_text(encoding="utf-8")
    mf_store = (configmap / "mf_store.sh").read_text(encoding="utf-8")
    single_container = (configmap / "all_combine_in_single_container.sh").read_text(encoding="utf-8")
    assert 'export service_id="slurm-test_' in common
    assert 'export SLURM_TEST_MARKER="configured"' in controller
    assert 'export MF_STORE_TEST_MARKER="configured"' in mf_store
    assert "SLURM_TEST_MARKER" not in single_container
    assert (configmap / "kv_store_backends.memcache.mmc-local-inprocess.conf").is_file()
    assert json.loads((configmap / "user_config.json").read_text(encoding="utf-8")) == user_config


def test_slurm_job_broadcasts_configmap_to_configurable_node_local_workspace():
    job_script = (DEPLOYER_DIR / "slurm_job.sh").read_text(encoding="utf-8")

    assert slurm_deploy.USER_WORKSPACE_PATH == Path("slurm_workspace")
    assert slurm_deploy.CONFIGMAP_PREPARE_PATH == Path("slurm_workspace/configmap")
    assert slurm_deploy.SLURM_DISTRIBUTION_PATH == "/tmp"
    assert slurm_deploy.SLURM_LOG_PATH == "./slurm_workspace"
    assert slurm_deploy.ENCODE_CPUS == 16
    assert slurm_deploy.PREFILL_CPUS == 16
    assert slurm_deploy.DECODE_CPUS == 16
    assert slurm_deploy.UNION_CPUS == 16
    assert slurm_deploy.JOB_SCRIPT == Path("slurm_workspace/slurm_job.sh")
    assert slurm_deploy.DEPLOYMENT_STATE_FILE == Path("slurm_workspace/slurm_deployment.json")
    assert slurm_deploy.CONFIGMAP_ARCHIVE_MARKER in job_script
    assert 'sbcast --force "$0" "$SLURM_LOCAL_WORKER_SCRIPT"' in job_script
    assert 'SLURM_DEPLOYMENT_PATH="${SLURM_DISTRIBUTION_PATH}/${SLURM_DEPLOYMENT_ID}"' in job_script
    assert 'SLURM_DEPLOYMENT_LOG_PATH="${SLURM_LOG_PATH}/${SLURM_DEPLOYMENT_ID}"' in job_script
    assert (
        'srun --ntasks-per-node=1 mkdir -m 700 -p "$SLURM_DEPLOYMENT_PATH" "$SLURM_DEPLOYMENT_LOG_PATH"' in job_script
    )
    assert '${SLURM_DEPLOYMENT_PATH}/mindie_motor_${SLURM_JOB_ID}.sh' in job_script
    assert '${SLURM_DEPLOYMENT_PATH}/mindie_motor_${SLURM_JOB_ID}_${SLURM_PROCID:-0}' in job_script
    assert 'export CONFIGMAP_PATH="$LOCAL_WORKSPACE_PATH/configmap"' in job_script
    assert '--bind "$CONFIGMAP_PATH:$CONFIGMAP_PATH:ro"' in job_script
    assert 'LOCAL_LOG_FILE="$SLURM_DEPLOYMENT_LOG_PATH/${ROLE}_${SLURM_JOB_ID}_task' in job_script
    assert 'LOCAL_LOG_DIR=' not in job_script
    assert 'exec >>"$LOCAL_LOG_FILE" 2>&1' in job_script
    assert 'rm -rf "$LOCAL_WORKSPACE_PATH/configmap"' in job_script
    assert "LOG_RUN_DIR" not in job_script
    assert "srun -o" not in job_script
    assert "--no-mount tmp" not in job_script
    assert "set_env_docker.py" not in job_script
    assert '${SERVICE_RUNTIME_ENV[@]+"${SERVICE_RUNTIME_ENV[@]}"}' in job_script
    assert '${KV_RUNTIME_ENV[@]+"${KV_RUNTIME_ENV[@]}"}' in job_script
    assert '${MF_RUNTIME_ENV[@]+"${MF_RUNTIME_ENV[@]}"}' in job_script


def test_generated_job_script_contains_complete_configmap(tmp_path, monkeypatch):
    configmap = tmp_path / "configmap"
    configmap.mkdir()
    (configmap / "boot.sh").write_text("source common.sh\n", encoding="utf-8")
    (configmap / "user_config.json").write_text('{"job_id": "test"}', encoding="utf-8")
    (configmap / "kv_store_backends.memcache.mmc-local-inprocess.conf").write_text(
        "custom memcache config", encoding="utf-8"
    )
    job_script = tmp_path / "slurm_job.sh"
    monkeypatch.setattr(slurm_deploy, "CONFIGMAP_PREPARE_PATH", configmap)
    monkeypatch.setattr(slurm_deploy, "JOB_SCRIPT", job_script)

    slurm_deploy._prepare_job_script()

    rendered = job_script.read_text(encoding="utf-8")
    assert slurm_deploy.CONFIGMAP_ARCHIVE_MARKER not in rendered
    payload = rendered.split('CONFIGMAP_ARCHIVE_B64="', 1)[1].split('"', 1)[0]
    with tarfile.open(fileobj=BytesIO(base64.b64decode(payload)), mode="r:gz") as archive:
        assert archive.extractfile("configmap/user_config.json").read() == b'{"job_id": "test"}'
        memcache = archive.extractfile("configmap/kv_store_backends.memcache.mmc-local-inprocess.conf")
        assert memcache.read() == b"custom memcache config"


def test_submit_engine_jobs_reports_skipped_resources(monkeypatch, capsys):
    submitted = []
    deploy_config = {
        "p_instances_num": 1,
        "single_p_instance_pod_num": 0,
        "p_pod_npu_num": 8,
    }
    monkeypatch.setattr(slurm_deploy, "_submit_job", lambda label, args: submitted.append((label, args)))

    state = {"engine_jobs": {role: {} for role in slurm_deploy.ENGINE_ROLES}}
    engine_cpus = {role: 16 for role in slurm_deploy.ENGINE_ROLES}
    slurm_deploy._submit_engine_jobs(deploy_config, "test-partition", "npu", engine_cpus, state)

    assert not submitted
    assert "Skipping prefill: pod_num=0, npu_num=8" in capsys.readouterr().out


def test_cli_uses_configurable_distribution_and_log_paths(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "slurm_deploy.py",
            "start",
            "--encode-cpus",
            "24",
            "--prefill-cpus",
            "32",
            "--decode-cpus",
            "40",
            "--union-cpus",
            "48",
            "--distribution-path",
            "/data/slurm",
            "--log-path",
            "/data/slurm-logs",
        ],
    )

    args = slurm_deploy.parse_arguments()

    assert not hasattr(args, "coordinator_infer_service")
    assert not hasattr(args, "coordinator_obs_service")
    assert args.encode_cpus == 24
    assert args.prefill_cpus == 32
    assert args.decode_cpus == 40
    assert args.union_cpus == 48
    assert args.coordinator_cpus == 64
    assert args.controller_cpus == 8
    assert args.kv_store_cpus == 8
    assert args.kv_conductor_cpus == 8
    assert args.mf_store_cpus == 8
    assert args.distribution_path == "/data/slurm"
    assert args.log_path == "/data/slurm-logs"
    assert not args.update_instance_num


def test_cli_accepts_update_instance_num(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["slurm_deploy.py", "start", "--config_dir", "/data/config", "--update_instance_num"],
    )

    args = slurm_deploy.parse_arguments()

    assert args.update_instance_num


def test_start_without_k8s_deploy_mode_prepares_once_and_submits_roles(tmp_path, monkeypatch):
    source = tmp_path / "conf"
    source.mkdir()
    user_config = _user_config()
    user_config["motor_deploy_config"].pop("deploy_mode")
    (source / "user_config.json").write_text(json.dumps(user_config), encoding="utf-8")
    (source / "env.json").write_text(json.dumps({"motor_common_env": {}}), encoding="utf-8")
    submitted = []
    monkeypatch.chdir(tmp_path)
    previous_log = tmp_path / "slurm_workspace" / "logs" / "previous" / "coordinator.log"
    previous_log.parent.mkdir(parents=True)
    previous_log.write_text("previous deployment", encoding="utf-8")
    stale_config = tmp_path / "slurm_workspace" / "configmap" / "stale.txt"
    stale_config.parent.mkdir()
    stale_config.write_text("stale config", encoding="utf-8")
    stale_state = tmp_path / "slurm_workspace" / "slurm_deployment.json"
    stale_state.write_text('{"deployment_id": "stale"}', encoding="utf-8")
    monkeypatch.setattr(slurm_deploy, "_resolve_node_name", lambda *_args: "node-1")
    monkeypatch.setattr(
        slurm_deploy,
        "_submit_job",
        lambda label, args: submitted.append((label, args)) or str(100 + len(submitted)),
    )
    args = argparse.Namespace(
        config_dir=str(source),
        user_config_path=None,
        env_config_path=None,
        partition="test-partition",
        device="npu",
        coordinator_cpus=64,
        controller_cpus=8,
        kv_store_cpus=8,
        kv_conductor_cpus=8,
        mf_store_cpus=8,
        encode_cpus=16,
        prefill_cpus=24,
        decode_cpus=32,
        union_cpus=40,
        distribution_path="/data/slurm",
        log_path="/data/slurm-logs",
        coordinator_service="10.0.0.1",
        controller_service="10.0.0.2",
        kvs_master_service="",
        kv_conductor_service="",
        mf_store_service="",
        ascend_mf_store_port="50089",
    )

    assert slurm_deploy.start(args) == 0

    assert (tmp_path / "slurm_workspace" / "configmap" / "boot.sh").is_file()
    assert (tmp_path / "slurm_workspace" / "slurm_job.sh").is_file()
    assert not stale_config.exists()
    assert previous_log.read_text(encoding="utf-8") == "previous deployment"
    assert os.environ["SLURM_DEPLOYMENT_ID"].startswith("slurm-test_")
    assert os.environ["SLURM_DISTRIBUTION_PATH"] == "/data/slurm"
    assert os.environ["SLURM_LOG_PATH"] == "/data/slurm-logs"
    assert os.environ["COORDINATOR_INFER_SERVICE"] == args.coordinator_service
    assert os.environ["COORDINATOR_OBS_SERVICE"] == args.coordinator_service
    assert [label for label, _args in submitted] == [
        "coordinator",
        "controller",
        "prefill instance 0",
        "decode instance 0",
    ]
    for label, job_args in submitted:
        expected_cpus = {
            "coordinator": 64,
            "controller": 8,
            "prefill instance 0": 24,
            "decode instance 0": 32,
        }[label]
        assert f"--cpus-per-task={expected_cpus}" in job_args
    state = json.loads((tmp_path / "slurm_workspace" / "slurm_deployment.json").read_text(encoding="utf-8"))
    assert state["partition"] == "test-partition"
    assert state["device"] == "npu"
    assert state["engine_cpus"] == {
        "encode": 16,
        "prefill": 24,
        "decode": 32,
        "union": 40,
    }
    assert state["service_jobs"] == {"coordinator": "101", "controller": "102"}
    assert state["engine_jobs"]["prefill"] == {"0": "103"}
    assert state["engine_jobs"]["decode"] == {"0": "104"}


def test_start_submits_basic_memcache_pool_and_kv_conductor(tmp_path, monkeypatch):
    source = tmp_path / "conf"
    source.mkdir()
    user_config = _user_config()
    user_config["kv_cache_store_config"] = {
        "backend": "memcache",
        "port": 50088,
        "config_store_port": 50089,
        "metrics_port": 50090,
        "local_service_mode": "inprocess",
    }
    user_config["kv_conductor_config"] = {"http_server_port": 14444}
    (source / "user_config.json").write_text(json.dumps(user_config), encoding="utf-8")
    (source / "env.json").write_text(json.dumps({"motor_common_env": {}}), encoding="utf-8")
    submitted = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(slurm_deploy, "_resolve_node_name", lambda *_args: "node-1")
    monkeypatch.setattr(
        slurm_deploy,
        "_submit_job",
        lambda label, args: submitted.append((label, args)) or str(100 + len(submitted)),
    )
    args = argparse.Namespace(
        config_dir=str(source),
        user_config_path=None,
        env_config_path=None,
        partition="test-partition",
        device="npu",
        coordinator_cpus=64,
        controller_cpus=8,
        kv_store_cpus=8,
        kv_conductor_cpus=8,
        mf_store_cpus=8,
        encode_cpus=16,
        prefill_cpus=16,
        decode_cpus=16,
        union_cpus=16,
        distribution_path="/data/slurm",
        log_path="/data/slurm-logs",
        coordinator_service="10.0.0.1",
        controller_service="10.0.0.2",
        kvs_master_service="10.0.0.3",
        kv_conductor_service="10.0.0.4",
        mf_store_service="",
        ascend_mf_store_port="50089",
    )

    assert slurm_deploy.start(args) == 0

    assert os.environ["KV_STORE_ENABLED"] == "1"
    assert os.environ["KV_STORE_BACKEND"] == "memcache"
    assert os.environ["KV_CONDUCTOR_ENABLED"] == "1"
    assert os.environ["KV_CONDUCTOR_PORT"] == "14444"
    assert [label for label, _args in submitted] == [
        "coordinator",
        "controller",
        "kv_store",
        "kv_conductor",
        "prefill instance 0",
        "decode instance 0",
    ]
    for label, job_args in submitted:
        if "instance" in label:
            expected_cpus = 16
        elif label == "coordinator":
            expected_cpus = 64
        else:
            expected_cpus = 8
        assert f"--cpus-per-task={expected_cpus}" in job_args


def test_update_instance_num_scales_engine_jobs_and_keeps_deployment_id(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "conf"
    source.mkdir()
    baseline_config = _user_config()
    target_config = _user_config()
    target_config["motor_deploy_config"]["p_instances_num"] = 2
    (source / "user_config.json").write_text(json.dumps(target_config), encoding="utf-8")
    (source / "env.json").write_text("{}", encoding="utf-8")

    configmap = tmp_path / "slurm_workspace" / "configmap"
    configmap.mkdir(parents=True)
    (configmap / "user_config.json").write_text(json.dumps(baseline_config), encoding="utf-8")
    state = {
        "deployment_id": "slurm-test-deployment",
        "partition": "test-partition",
        "device": "npu",
        "engine_cpus": {"encode": 16, "prefill": 24, "decode": 32, "union": 40},
        "runtime_env": {"SLURM_DEPLOYMENT_ID": "slurm-test-deployment"},
        "service_jobs": {"coordinator": "101", "controller": "102"},
        "engine_jobs": {
            "union": {},
            "encode": {},
            "prefill": {"0": "201"},
            "decode": {"0": "202"},
        },
    }
    state_path = tmp_path / "slurm_workspace" / "slurm_deployment.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    job_script = tmp_path / "slurm_workspace" / "slurm_job.sh"
    job_script.write_text("existing", encoding="utf-8")
    submitted = []
    monkeypatch.setattr(
        slurm_deploy,
        "_submit_job",
        lambda label, job_args: submitted.append((label, job_args)) or "203",
    )
    args = argparse.Namespace(
        config_dir=str(source),
        user_config_path=None,
        env_config_path=None,
    )

    assert slurm_deploy.update_instance_num(args) == 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["deployment_id"] == "slurm-test-deployment"
    assert state["engine_jobs"]["prefill"] == {"0": "201", "1": "203"}
    assert submitted[0][0] == "prefill instance 1"
    assert "--cpus-per-task=24" in submitted[0][1]
    prepared_config = json.loads((configmap / "user_config.json").read_text(encoding="utf-8"))
    assert prepared_config["motor_deploy_config"]["p_instances_num"] == 2

    target_config["motor_deploy_config"]["p_instances_num"] = 1
    (source / "user_config.json").write_text(json.dumps(target_config), encoding="utf-8")
    cancelled = []
    monkeypatch.setattr(slurm_deploy, "_command", lambda name: name)

    def fake_run(command, check=False):
        del check
        cancelled.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(slurm_deploy.subprocess, "run", fake_run)

    assert slurm_deploy.update_instance_num(args) == 0
    assert cancelled == [["scancel", "--quiet", "203"]]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["engine_jobs"]["prefill"] == {"0": "201"}


def test_update_instance_num_rejects_other_config_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "conf"
    source.mkdir()
    baseline_config = _user_config()
    target_config = _user_config()
    target_config["motor_deploy_config"]["weight_mount_path"] = "/another/model"
    (source / "user_config.json").write_text(json.dumps(target_config), encoding="utf-8")
    (source / "env.json").write_text("{}", encoding="utf-8")
    configmap = tmp_path / "slurm_workspace" / "configmap"
    configmap.mkdir(parents=True)
    (configmap / "user_config.json").write_text(json.dumps(baseline_config), encoding="utf-8")
    job_script = tmp_path / "slurm_workspace" / "slurm_job.sh"
    job_script.write_text("existing", encoding="utf-8")
    args = argparse.Namespace(config_dir=str(source), user_config_path=None, env_config_path=None)

    try:
        slurm_deploy.update_instance_num(args)
    except ValueError as exc:
        assert "Only e_instances_num" in str(exc)
    else:
        raise AssertionError("Expected a non-instance config change to be rejected")


def test_kv_conductor_does_not_allocate_a_kv_store_node_by_itself(monkeypatch):
    user_config = _user_config()
    user_config["kv_conductor_config"] = {"http_server_port": 14444}
    slurm_deploy._init_deploy_flags(user_config)
    resolved_labels = []
    monkeypatch.setattr(
        slurm_deploy,
        "_resolve_node_name",
        lambda _service, label, _explicit="": resolved_labels.append(label) or "node-1",
    )
    args = argparse.Namespace(
        coordinator_service="10.0.0.1",
        controller_service="10.0.0.2",
        kvs_master_service="10.0.0.3",
        kv_conductor_service="10.0.0.4",
        mf_store_service="",
    )

    nodes = slurm_deploy._management_nodes(args)

    assert "kv_store" not in nodes
    assert "KVS_MASTER_SERVICE" not in resolved_labels
    assert "KV_CONDUCTOR_SERVICE" in resolved_labels


def test_stop_cancels_jobs_and_preserves_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "slurm_workspace"
    workspace.mkdir()
    state_file = workspace / "slurm_deployment.json"
    state = {
        "deployment_id": "test-deployment",
        "partition": "test-partition",
        "device": "npu",
        "engine_cpus": {"encode": 16, "prefill": 24, "decode": 32, "union": 40},
        "runtime_env": {"SLURM_DEPLOYMENT_ID": "test-deployment"},
        "service_jobs": {"coordinator": "101"},
        "engine_jobs": {
            "union": {},
            "encode": {},
            "prefill": {"0": "102"},
            "decode": {},
        },
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")
    configmap = workspace / "configmap"
    configmap.mkdir()
    (configmap / "user_config.json").write_text("{}", encoding="utf-8")
    logs = workspace / "logs" / "20260909_120000"
    logs.mkdir(parents=True)
    log_file = logs / "coordinator.log"
    log_file.write_text("persistent log", encoding="utf-8")
    unrelated = tmp_path / "kernel_meta"
    unrelated.mkdir()
    cancelled = []

    monkeypatch.setattr(slurm_deploy, "_command", lambda name: name)

    def fake_run(command, check=False):
        del check
        cancelled.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(slurm_deploy.subprocess, "run", fake_run)

    assert slurm_deploy.stop() == 0
    assert cancelled == [["scancel", "--quiet", "101"], ["scancel", "--quiet", "102"]]
    assert workspace.exists()
    assert configmap.exists()
    assert json.loads(state_file.read_text(encoding="utf-8")) == state
    assert log_file.read_text(encoding="utf-8") == "persistent log"
    assert unrelated.exists()


def test_stop_failure_preserves_complete_job_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "slurm_workspace"
    workspace.mkdir()
    state_file = workspace / "slurm_deployment.json"
    state = {
        "deployment_id": "test-deployment",
        "partition": "test-partition",
        "device": "npu",
        "engine_cpus": {"encode": 16, "prefill": 24, "decode": 32, "union": 40},
        "runtime_env": {"SLURM_DEPLOYMENT_ID": "test-deployment"},
        "service_jobs": {"coordinator": "101"},
        "engine_jobs": {
            "union": {},
            "encode": {},
            "prefill": {"0": "102"},
            "decode": {},
        },
    }
    state_file.write_text(json.dumps(state), encoding="utf-8")
    configmap = workspace / "configmap"
    configmap.mkdir()
    monkeypatch.setattr(slurm_deploy, "_command", lambda name: name)

    def fake_run(command, check=False):
        del check
        return subprocess.CompletedProcess(command, int(command[-1] == "102"))

    monkeypatch.setattr(slurm_deploy.subprocess, "run", fake_run)

    assert slurm_deploy.stop() == 1
    assert workspace.exists()
    assert configmap.exists()
    assert json.loads(state_file.read_text(encoding="utf-8")) == state
