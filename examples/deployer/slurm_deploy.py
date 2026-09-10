# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Deploy MindIE Motor roles through Slurm and Apptainer."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path

import lib.constant as C
from lib.config_validator import (
    resolve_config_paths,
    validate_pd_hybrid_config,
    validate_reserved_labels,
)
from lib.prepare_utils import prepare_rendered_local_configmap
from lib.generator import k8s_utils
from lib.generator.engine import validate_instance_nums
from lib.generator.kv_cache_store import normalize_kv_cache_store_config
from lib.generator.kv_conductor import normalize_kv_conductor_config
from lib.utils import read_json


# Slurm site settings. Edit these defaults here, or override them with
# environment variables / command-line options.
COORDINATOR_SERVICE = "<coordinator-ip>"
CONTROLLER_SERVICE = "<controller-ip>"
KVS_MASTER_SERVICE = "<kvs-master-ip>"
KV_CONDUCTOR_SERVICE = "<kv-conductor-ip>"
MF_STORE_SERVICE = "<mf-store-ip>"
ASCEND_MF_STORE_PORT = "50089"
PARTITION = "<partition>"
DEVICE = "npu"
COORDINATOR_CPUS = 64
CONTROLLER_CPUS = 8
KV_STORE_CPUS = 8
KV_CONDUCTOR_CPUS = 8
MF_STORE_CPUS = 8
ENCODE_CPUS = 16
PREFILL_CPUS = 16
DECODE_CPUS = 16
UNION_CPUS = 16
SLURM_DISTRIBUTION_PATH = "./slurm_workspace"

DEPLOYER_DIR = Path(__file__).resolve().parent
JOB_SCRIPT_TEMPLATE = DEPLOYER_DIR / "slurm_job.sh"
USER_WORKSPACE_PATH = Path("./slurm_workspace")
CONFIGMAP_PREPARE_PATH = USER_WORKSPACE_PATH / "configmap"
JOB_SCRIPT = USER_WORKSPACE_PATH / "slurm_job.sh"
DEPLOYMENT_STATE_FILE = USER_WORKSPACE_PATH / "slurm_deployment.json"
CONTAINER_CONFIG_PATH = "/usr/local/Ascend/pyMotor/conf"
CONFIGMAP_ARCHIVE_MARKER = "__MOTOR_CONFIGMAP_ARCHIVE_B64__"
DEFAULT_MAX_BATCH_SCRIPT_BYTES = 4 * 1024 * 1024
ENGINE_RESOURCE_KEYS = {
    "encode": (C.E_INSTANCES_NUM, C.SINGER_E_INSTANCES_NUM, C.E_POD_NPU_NUM),
    "prefill": (C.P_INSTANCES_NUM, C.SINGER_P_INSTANCES_NUM, C.P_POD_NPU_NUM),
    "decode": (C.D_INSTANCES_NUM, C.SINGER_D_INSTANCES_NUM, C.D_POD_NPU_NUM),
    "union": (C.HYBRID_INSTANCES_NUM, C.SINGLE_HYBRID_INSTANCE_POD_NUM, C.HYBRID_POD_NPU_NUM),
}
ENGINE_ROLE_PREFIXES = {"union": "u", "encode": "e", "prefill": "p", "decode": "d"}
ENGINE_ROLES = tuple(ENGINE_RESOURCE_KEYS)


# Basic value and command helpers
def _env_value(name: str, default: str) -> str:
    """Read a setting from the environment, or return its default value."""
    return os.environ.get(name, default)


def _as_int(value, default: int) -> int:
    """Return an integer, using the default when the value is empty."""
    return default if value is None or value == "" else int(value)


def _as_str(value, default: str = "") -> str:
    """Return a string, using the default when the value is empty."""
    return default if value is None or value == "" else str(value)


def _positive_int(value: str) -> int:
    """Read a command-line integer and require a value greater than zero."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def _absolute_directory(value: str) -> str:
    """Convert a directory to an absolute path based on the current directory."""
    path = os.path.abspath(os.path.normpath(value))
    if path == os.path.sep:
        raise ValueError("value must resolve to a directory other than /")
    return path


def _require_service(name: str, value: str) -> None:
    """Check that a required service address has been set."""
    if not value or "<" in value or ">" in value:
        raise ValueError(f"{name} is empty or still a placeholder: {value or '<empty>'}")


def _command(name: str) -> str:
    """Find a required command on the submission host."""
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required command is not available: {name}")
    return path


def _resolve_node_name(service: str, label: str, explicit_node: str = "") -> str:
    """Find the Slurm node name for a service address."""
    if explicit_node:
        return explicit_node
    lookup = service.strip("[]")
    result = subprocess.run([_command("getent"), "hosts", lookup], capture_output=True, text=True, check=False)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2:
            return fields[1]
    result = subprocess.run([_command("scontrol"), "show", "node", "-o"], capture_output=True, text=True, check=False)
    for line in result.stdout.splitlines():
        fields = dict(item.split("=", 1) for item in line.split() if "=" in item)
        if fields.get("NodeAddr", "").strip("[]") == lookup:
            return fields.get("NodeName", "")
    raise ValueError(f"Cannot map {label}={service} to a Slurm NodeName; set the corresponding *_NODE variable")


def _format_host_for_url(host: str) -> str:
    """Add brackets when an IPv6 address is used in a URL."""
    if host.startswith("[") and host.endswith("]"):
        return host
    return f"[{host}]" if ":" in host else host


# Files prepared on the submission host
def _deployment_id(user_config: dict) -> str:
    """Create a safe, unique directory name for one start operation."""
    job_id = _as_str(user_config.get(C.MOTOR_DEPLOY_CONFIG, {}).get(C.CONFIG_JOB_ID), "motor")
    safe_job_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", job_id).strip("-.") or "motor"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"{safe_job_id}_{timestamp}_{uuid.uuid4().hex[:8]}"


def _prepare_configmap(user_config_path: str, env_config_path: str) -> None:
    """Prepare one ConfigMap directory on the submission host.

    Copy the startup files, user_config.json, and env.json into the directory.
    Render the shell files before jobs start so every role uses one service_id.
    """
    prepare_rendered_local_configmap(DEPLOYER_DIR, CONFIGMAP_PREPARE_PATH, user_config_path, env_config_path)
    CONFIGMAP_PREPARE_PATH.chmod(0o700)


def _prepare_job_script() -> None:
    """Pack the ConfigMap and place it inside the generated Slurm script."""
    archive = BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        tar.add(CONFIGMAP_PREPARE_PATH, arcname="configmap")

    payload = base64.b64encode(archive.getvalue()).decode("ascii")
    template = JOB_SCRIPT_TEMPLATE.read_text(encoding="utf-8")
    if template.count(CONFIGMAP_ARCHIVE_MARKER) != 1:
        raise ValueError(f"Expected one {CONFIGMAP_ARCHIVE_MARKER} marker in {JOB_SCRIPT_TEMPLATE}")
    rendered = template.replace(CONFIGMAP_ARCHIVE_MARKER, payload)
    rendered_size = len(rendered.encode("utf-8"))
    if rendered_size > DEFAULT_MAX_BATCH_SCRIPT_BYTES:
        raise ValueError(
            f"Generated batch script is {rendered_size} bytes; it exceeds Slurm's default "
            f"MaxScriptSize of {DEFAULT_MAX_BATCH_SCRIPT_BYTES} bytes"
        )

    JOB_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    JOB_SCRIPT.write_text(rendered, encoding="utf-8")
    JOB_SCRIPT.chmod(0o700)


def _write_prepared_user_config(user_config: dict) -> None:
    """Update the prepared user config and rebuild the job script."""
    user_config_path = CONFIGMAP_PREPARE_PATH / "user_config.json"
    user_config_path.write_text(json.dumps(user_config, indent=4, ensure_ascii=False), encoding="utf-8")
    _prepare_job_script()


# Deployment state used by later scale operations
def _write_deployment_state(state: dict) -> None:
    """Save the instance Job IDs and settings needed by scale operations."""
    DEPLOYMENT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = DEPLOYMENT_STATE_FILE.with_suffix(".tmp")
    temporary_path.write_text(json.dumps(state, indent=4), encoding="utf-8")
    temporary_path.replace(DEPLOYMENT_STATE_FILE)


def _read_deployment_state() -> dict:
    """Read and check the state written by a successful start command."""
    if not DEPLOYMENT_STATE_FILE.is_file():
        raise FileNotFoundError(f"Deployment state not found at {DEPLOYMENT_STATE_FILE}; run start before scaling")
    state = read_json(str(DEPLOYMENT_STATE_FILE))
    if not isinstance(state, dict):
        raise ValueError(f"Invalid deployment state at {DEPLOYMENT_STATE_FILE}")
    required_fields = {
        "deployment_id": str,
        "partition": str,
        "device": str,
        "engine_cpus": dict,
        "runtime_env": dict,
        "service_jobs": dict,
        "engine_jobs": dict,
    }
    for field, expected_type in required_fields.items():
        if not isinstance(state.get(field), expected_type):
            raise ValueError(f"Invalid deployment state field: {field}")
    if set(state["engine_jobs"]) != set(ENGINE_ROLES):
        raise ValueError("Deployment state does not contain all Engine roles")
    if set(state["engine_cpus"]) != set(ENGINE_ROLES) or not all(
        isinstance(cpus, int) and cpus > 0 for cpus in state["engine_cpus"].values()
    ):
        raise ValueError("Deployment state contains invalid Engine CPU settings")
    if not all(isinstance(name, str) and isinstance(value, str) for name, value in state["runtime_env"].items()):
        raise ValueError("Deployment state contains an invalid runtime environment")
    if not all(
        isinstance(role, str) and isinstance(job_id, str) and job_id.isdigit()
        for role, job_id in state["service_jobs"].items()
    ):
        raise ValueError("Deployment state contains invalid service Job IDs")
    for role, jobs in state["engine_jobs"].items():
        if not isinstance(jobs, dict) or not all(
            isinstance(index, str) and index.isdigit() and isinstance(job_id, str) and job_id.isdigit()
            for index, job_id in jobs.items()
        ):
            raise ValueError(f"Deployment state contains invalid {role} instance Job IDs")
    return state


def _validate_scale_config(user_config: dict, baseline_config: dict) -> None:
    """Allow instance count changes only; all other settings must stay the same."""
    current = json.loads(json.dumps(user_config))
    baseline = json.loads(json.dumps(baseline_config))
    for instance_key, _pod_key, _npu_key in ENGINE_RESOURCE_KEYS.values():
        current[C.MOTOR_DEPLOY_CONFIG].pop(instance_key, None)
        baseline[C.MOTOR_DEPLOY_CONFIG].pop(instance_key, None)
    if current != baseline:
        raise ValueError(
            "Only e_instances_num, p_instances_num, d_instances_num, and "
            "hybrid_instances_num can be changed when scaling"
        )


def _engine_settings(deploy_config: dict, role: str) -> tuple[int, int, int]:
    """Read the instance, node, and NPU counts for one Engine role."""
    instance_key, pod_key, npu_key = ENGINE_RESOURCE_KEYS[role]
    return (
        _as_int(deploy_config.get(instance_key), 1),
        _as_int(deploy_config.get(pod_key), 1),
        _as_int(deploy_config.get(npu_key), 0),
    )


def _check_engine_state(deploy_config: dict, engine_jobs: dict) -> None:
    """Require saved instance indexes to match the prepared user config."""
    for role in ENGINE_ROLES:
        instances, pod_num, npu_num = _engine_settings(deploy_config, role)
        expected_instances = instances if pod_num > 0 and npu_num > 0 else 0
        expected_indexes = {str(index) for index in range(expected_instances)}
        if set(engine_jobs[role]) != expected_indexes:
            raise ValueError(f"Saved {role} instances do not match {CONFIGMAP_PREPARE_PATH / 'user_config.json'}")


# Slurm job creation and submission
def _base_job_args(partition: str, role: str, nodes: int, cpus: int) -> list[str]:
    """Build the sbatch arguments shared by all roles."""
    return [
        "--export=ALL",
        f"--partition={partition}",
        f"--chdir={Path.cwd()}",
        f"--nodes={nodes}",
        f"--cpus-per-task={cpus}",
        f"--job-name={role}",
        "-o=/dev/null",
        "-e=/dev/null",
    ]


def _submit_job(label: str, args: list[str]) -> str:
    """Submit one job, check its Job ID, and return the ID."""
    result = subprocess.run([_command("sbatch"), "--parsable", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to submit {label}: {(result.stderr or result.stdout).strip()}")
    job_id = result.stdout.strip().split(";", 1)[0]
    if not job_id.isdigit():
        raise RuntimeError(f"Cannot parse the {label} Job ID from sbatch output: {result.stdout.strip()}")
    print(f"Submitted {label}, Job ID: {job_id}")
    return job_id


def _submit_engine_instance(
    role: str, index: int, deploy_config: dict, partition: str, device: str, engine_cpus: int
) -> str:
    """Submit one Engine instance and return its Slurm Job ID."""
    _instances, pod_num, npu_num = _engine_settings(deploy_config, role)
    if pod_num <= 0 or npu_num <= 0:
        raise ValueError(f"Cannot submit {role}: pod_num={pod_num}, npu_num={npu_num}")
    job_args = _base_job_args(partition, role, pod_num, engine_cpus)
    job_args.extend([f"--gres={device}:{npu_num}", str(JOB_SCRIPT), role, f"{ENGINE_ROLE_PREFIXES[role]}{index}"])
    return _submit_job(f"{role} instance {index}", job_args)


def _submit_engine_jobs(deploy_config: dict, partition: str, device: str, engine_cpus: dict, state: dict) -> None:
    """Submit enabled Engine instances and save each Job ID immediately."""
    engine_jobs = state["engine_jobs"]
    for role in ENGINE_ROLES:
        instances, pod_num, npu_num = _engine_settings(deploy_config, role)
        if pod_num <= 0 or npu_num <= 0:
            print(f"Skipping {role}: pod_num={pod_num}, npu_num={npu_num}")
            continue
        for index in range(instances):
            engine_jobs[role][str(index)] = _submit_engine_instance(
                role, index, deploy_config, partition, device, engine_cpus[role]
            )
            _write_deployment_state(state)


# Runtime settings passed to Slurm and Apptainer
def _init_deploy_flags(user_config: dict) -> dict:
    """Read which engine and optional services are enabled."""
    k8s_utils.update_kv_store_enabled_flag(user_config)
    k8s_utils.update_kv_conductor_enabled_flag(user_config)
    k8s_utils.update_engine_type_flag(user_config)
    if k8s_utils.g_kv_conductor_enabled:
        normalize_kv_conductor_config(user_config)
    if k8s_utils.g_kv_store_enabled:
        return normalize_kv_cache_store_config(user_config)
    return {}


def _management_nodes(args: argparse.Namespace) -> dict[str, str]:
    """Find the Slurm node for each management or storage service."""
    nodes = {
        "coordinator": _resolve_node_name(
            args.coordinator_service, "COORDINATOR_SERVICE", _env_value("COORDINATOR_NODE", "")
        ),
        "controller": _resolve_node_name(
            args.controller_service, "CONTROLLER_SERVICE", _env_value("CONTROLLER_NODE", "")
        ),
    }
    if k8s_utils.g_kv_store_enabled:
        hint = _env_value("KVS_MASTER_NODE", _env_value("KVS_STORE_NODE", ""))
        nodes["kv_store"] = _resolve_node_name(args.kvs_master_service, "KVS_MASTER_SERVICE", hint)
    if k8s_utils.g_kv_conductor_enabled:
        nodes["kv_conductor"] = _resolve_node_name(
            args.kv_conductor_service, "KV_CONDUCTOR_SERVICE", _env_value("KV_CONDUCTOR_NODE", "")
        )
    if k8s_utils.g_mf_store_enabled:
        nodes["mf_store"] = _resolve_node_name(
            args.mf_store_service, "MF_STORE_SERVICE", _env_value("MF_STORE_NODE", "")
        )
    return nodes


def _export_runtime_env(user_config: dict, kv_store: dict, args: argparse.Namespace, deployment_id: str) -> dict:
    """Set the environment variables used by Slurm jobs and containers."""
    deploy = user_config[C.MOTOR_DEPLOY_CONFIG]
    hardware_type = _as_str(deploy.get(C.HARDWARE_TYPE))
    kv_backend = _as_str(kv_store.get(C.KV_STORE_BACKEND), C.DEFAULT_KV_STORE_BACKEND)
    kv_port = _as_str(kv_store.get(C.KV_CACHE_STORE_PORT), str(C.DEFAULT_KV_CACHE_STORE_PORT))
    metrics_port = _as_str(kv_store.get(C.MMC_METRICS_PORT_KEY))
    if not metrics_port:
        metrics_port = kv_port if kv_backend == "mooncake" else str(C.DEFAULT_MMC_METRICS_PORT)
    high_watermark = _as_str(kv_store.get(C.KV_STORE_EVICTION_HIGH_WATERMARK_RATIO))
    eviction_ratio = _as_str(kv_store.get(C.KV_STORE_EVICTION_RATIO))
    if k8s_utils.g_kv_store_enabled and kv_backend == "mooncake" and (not high_watermark or not eviction_ratio):
        raise ValueError("backend=mooncake requires eviction_high_watermark_ratio and eviction_ratio")

    values = {
        "COORDINATOR_SERVICE": args.coordinator_service,
        "COORDINATOR_INFER_SERVICE": args.coordinator_service,
        "COORDINATOR_OBS_SERVICE": args.coordinator_service,
        "CONTROLLER_SERVICE": args.controller_service,
        "KVS_MASTER_SERVICE": args.kvs_master_service,
        "KV_CONDUCTOR_SERVICE": args.kv_conductor_service,
        "MF_STORE_SERVICE": args.mf_store_service,
        "IMAGE_NAME": _as_str(deploy.get(C.IMAGE_NAME)),
        "MODEL_PATH": _as_str(deploy.get(C.WEIGHT_MOUNT_PATH)),
        "HARDWARE_TYPE": hardware_type,
        "ENGINE_TYPE": k8s_utils.g_engine_type,
        "KV_STORE_ENABLED": str(int(k8s_utils.g_kv_store_enabled)),
        "KV_CONDUCTOR_ENABLED": str(int(k8s_utils.g_kv_conductor_enabled)),
        "MF_STORE_ENABLED": str(int(k8s_utils.g_mf_store_enabled)),
        "KV_STORE_BACKEND": kv_backend if k8s_utils.g_kv_store_enabled else "",
        "KV_CACHE_STORE_PORT": kv_port,
        "KV_STORE_EVICTION_HIGH_WATERMARK_RATIO": high_watermark,
        "KV_STORE_EVICTION_RATIO": eviction_ratio,
        "DEFAULT_KV_LEASE_TTL": _as_str(kv_store.get(C.DEFAULT_KV_LEASE_TTL), "11000"),
        "MMC_CONFIG_STORE_PORT": _as_str(
            kv_store.get(C.MMC_CONFIG_STORE_PORT_KEY), str(C.DEFAULT_MMC_CONFIG_STORE_PORT)
        ),
        "MMC_METRICS_PORT": metrics_port,
        "MMC_LOCAL_SERVICE_MODE": _as_str(kv_store.get(C.MMC_LOCAL_SERVICE_CONFIG_KEY)),
        "KV_CONDUCTOR_PORT": _as_str(user_config.get(C.KV_CONDUCTOR_CONFIG, {}).get(C.KV_CONDUCTOR_PORT, 0)),
        "ASCEND_MF_STORE_PORT": args.ascend_mf_store_port,
        "ASCEND_MF_TRANSFER_PROTOCOL": "device_rdma" if hardware_type in C.HARDWARE_TYPE_A2 else "sdma",
        "CONFIG_PATH": CONTAINER_CONFIG_PATH,
        "SLURM_DEPLOYMENT_ID": deployment_id,
        "SLURM_DISTRIBUTION_PATH": args.distribution_path,
    }
    if k8s_utils.g_mf_store_enabled:
        values["ASCEND_MF_STORE_URL"] = (
            f"tcp://{_format_host_for_url(args.mf_store_service)}:{args.ascend_mf_store_port}"
        )
    os.environ.update(values)
    return values


# Start and stop commands
def start(args: argparse.Namespace) -> int:
    """Check the config, prepare local files, and submit all required jobs."""
    # Check every input before creating files or submitting jobs.
    user_config_path, env_config_path = resolve_config_paths(
        args.config_dir, args.user_config_path, args.env_config_path
    )
    user_config = read_json(user_config_path)
    validate_reserved_labels(user_config)
    if C.HYBRID_INSTANCES_NUM in user_config.get(C.MOTOR_DEPLOY_CONFIG, {}):
        validate_pd_hybrid_config(user_config)
    validate_instance_nums(user_config)
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    if not deploy_config.get(C.IMAGE_NAME):
        raise ValueError(f"{C.MOTOR_DEPLOY_CONFIG}.{C.IMAGE_NAME} is required")
    if not deploy_config.get(C.WEIGHT_MOUNT_PATH):
        raise ValueError(f"{C.MOTOR_DEPLOY_CONFIG}.{C.WEIGHT_MOUNT_PATH} is required")
    kv_store_config = _init_deploy_flags(user_config)

    _require_service("COORDINATOR_SERVICE", args.coordinator_service)
    _require_service("CONTROLLER_SERVICE", args.controller_service)
    if k8s_utils.g_kv_store_enabled or k8s_utils.g_kv_conductor_enabled:
        _require_service("KVS_MASTER_SERVICE", args.kvs_master_service)
    if k8s_utils.g_kv_conductor_enabled:
        _require_service("KV_CONDUCTOR_SERVICE", args.kv_conductor_service)
    if k8s_utils.g_mf_store_enabled:
        _require_service("MF_STORE_SERVICE", args.mf_store_service)
    nodes = _management_nodes(args)

    # Rebuild prepared files. Logs from earlier runs are kept.
    shutil.rmtree(CONFIGMAP_PREPARE_PATH, ignore_errors=True)
    JOB_SCRIPT.unlink(missing_ok=True)
    DEPLOYMENT_STATE_FILE.unlink(missing_ok=True)
    deployment_id = _deployment_id(user_config)
    _prepare_configmap(user_config_path, env_config_path)
    _prepare_job_script()
    runtime_env = _export_runtime_env(user_config, kv_store_config, args, deployment_id)
    engine_cpus = {
        "encode": args.encode_cpus,
        "prefill": args.prefill_cpus,
        "decode": args.decode_cpus,
        "union": args.union_cpus,
    }
    state = {
        "deployment_id": deployment_id,
        "partition": args.partition,
        "device": args.device,
        "engine_cpus": engine_cpus,
        "runtime_env": runtime_env,
        "service_jobs": {},
        "engine_jobs": {role: {} for role in ENGINE_ROLES},
    }
    _write_deployment_state(state)

    try:
        # Coordinator and Controller always run. Other services depend on the config.
        for role, cpus in (
            ("coordinator", args.coordinator_cpus),
            ("controller", args.controller_cpus),
        ):
            job_args = _base_job_args(args.partition, role, 1, cpus)
            job_args.extend([f"--nodelist={nodes[role]}", str(JOB_SCRIPT), role])
            state["service_jobs"][role] = _submit_job(role, job_args)
            _write_deployment_state(state)
        for enabled, role, cpus in (
            (k8s_utils.g_kv_store_enabled, "kv_store", args.kv_store_cpus),
            (k8s_utils.g_kv_conductor_enabled, "kv_conductor", args.kv_conductor_cpus),
            (k8s_utils.g_mf_store_enabled, "mf_store", args.mf_store_cpus),
        ):
            if enabled:
                job_args = _base_job_args(args.partition, role, 1, cpus)
                job_args.extend([f"--nodelist={nodes[role]}", str(JOB_SCRIPT), role])
                state["service_jobs"][role] = _submit_job(role, job_args)
                _write_deployment_state(state)
        _submit_engine_jobs(deploy_config, args.partition, args.device, engine_cpus, state)
    except Exception:
        stop(ignore_missing=True)
        raise
    return 0


def update_instance_num(args: argparse.Namespace) -> int:
    """Scale Engine instances without restarting the other service roles."""
    user_config_path, _env_config_path = resolve_config_paths(
        args.config_dir, args.user_config_path, args.env_config_path
    )
    user_config = read_json(user_config_path)
    validate_reserved_labels(user_config)
    if C.HYBRID_INSTANCES_NUM in user_config.get(C.MOTOR_DEPLOY_CONFIG, {}):
        validate_pd_hybrid_config(user_config)
    validate_instance_nums(user_config)

    baseline_path = CONFIGMAP_PREPARE_PATH / "user_config.json"
    if not baseline_path.is_file():
        raise FileNotFoundError(f"Prepared user config not found at {baseline_path}; run start before scaling")
    if not JOB_SCRIPT.is_file():
        raise FileNotFoundError(f"Generated Slurm job script not found at {JOB_SCRIPT}; run start before scaling")

    baseline_config = read_json(str(baseline_path))
    _validate_scale_config(user_config, baseline_config)
    state = _read_deployment_state()
    baseline_deploy = baseline_config[C.MOTOR_DEPLOY_CONFIG]
    target_deploy = user_config[C.MOTOR_DEPLOY_CONFIG]
    engine_jobs = state["engine_jobs"]
    _check_engine_state(baseline_deploy, engine_jobs)

    changes = []
    for role in ENGINE_ROLES:
        instance_key, _pod_key, _npu_key = ENGINE_RESOURCE_KEYS[role]
        current_count, pod_num, npu_num = _engine_settings(baseline_deploy, role)
        target_count, _target_pod_num, _target_npu_num = _engine_settings(target_deploy, role)
        if target_count < 0:
            raise ValueError(f"{instance_key} must not be less than zero")
        if current_count != target_count:
            if pod_num <= 0 or npu_num <= 0:
                raise ValueError(f"Cannot scale {role}: pod_num={pod_num}, npu_num={npu_num}")
            changes.append((role, instance_key, current_count, target_count))

    if not changes:
        print("Engine instance counts are unchanged")
        return 0

    if state["runtime_env"].get("SLURM_DEPLOYMENT_ID") != state["deployment_id"]:
        raise ValueError("Deployment state contains inconsistent SLURM_DEPLOYMENT_ID values")
    os.environ.update(state["runtime_env"])

    # New jobs must receive the requested instance counts in their ConfigMap.
    _write_prepared_user_config(user_config)
    try:
        for role, _instance_key, current_count, target_count in changes:
            for index in range(current_count - 1, target_count - 1, -1):
                job_id = engine_jobs[role][str(index)]
                result = subprocess.run([_command("scancel"), "--quiet", job_id], check=False)
                if result.returncode:
                    raise RuntimeError(f"Failed to cancel {role} instance {index}, Job ID: {job_id}")
                del engine_jobs[role][str(index)]
                _write_deployment_state(state)
                print(f"Cancelled {role} instance {index}, Job ID: {job_id}")

            for index in range(current_count, target_count):
                job_id = _submit_engine_instance(
                    role,
                    index,
                    target_deploy,
                    state["partition"],
                    state["device"],
                    state["engine_cpus"][role],
                )
                engine_jobs[role][str(index)] = job_id
                _write_deployment_state(state)
    except Exception:
        # Keep the prepared config aligned with every instance change that completed.
        for role, instance_key, _current_count, _target_count in changes:
            baseline_deploy[instance_key] = len(engine_jobs[role])
        _write_prepared_user_config(baseline_config)
        raise
    return 0


def stop(*, ignore_missing: bool = False) -> int:
    """Cancel all jobs in the deployment state and keep the workspace."""
    if not DEPLOYMENT_STATE_FILE.is_file():
        if ignore_missing:
            return 0
        raise RuntimeError(f"No deployment state found at {DEPLOYMENT_STATE_FILE}")

    state = _read_deployment_state()
    job_ids = list(state["service_jobs"].values())
    for jobs in state["engine_jobs"].values():
        job_ids.extend(jobs.values())
    if not job_ids:
        if ignore_missing:
            return 0
        raise RuntimeError(f"No Job ID found in {DEPLOYMENT_STATE_FILE}")

    failed_job_ids = []
    for job_id in job_ids:
        result = subprocess.run([_command("scancel"), "--quiet", job_id], check=False)
        if result.returncode:
            failed_job_ids.append(job_id)
        else:
            print(f"Cancelled Job ID: {job_id}")
    return int(bool(failed_job_ids))


# Command-line entry
def parse_arguments() -> argparse.Namespace:
    """Read the command and its settings."""
    parser = argparse.ArgumentParser(description="Deploy MindIE Motor with Slurm and Apptainer")
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--config_dir", "--dir")
    parser.add_argument("--user_config_path", "--config")
    parser.add_argument("--env_config_path", "--env")
    parser.add_argument(
        "--update_instance_num",
        action="store_true",
        help="scale Engine instances by comparing user_config with the prepared config",
    )
    parser.add_argument("--partition", default=_env_value("PARTITION", PARTITION))
    parser.add_argument("--device", default=_env_value("DEVICE", DEVICE))
    parser.add_argument(
        "--coordinator-cpus",
        type=_positive_int,
        default=_env_value("COORDINATOR_CPUS", str(COORDINATOR_CPUS)),
    )
    parser.add_argument(
        "--controller-cpus", type=_positive_int, default=_env_value("CONTROLLER_CPUS", str(CONTROLLER_CPUS))
    )
    parser.add_argument("--kv-store-cpus", type=_positive_int, default=_env_value("KV_STORE_CPUS", str(KV_STORE_CPUS)))
    parser.add_argument(
        "--kv-conductor-cpus",
        type=_positive_int,
        default=_env_value("KV_CONDUCTOR_CPUS", str(KV_CONDUCTOR_CPUS)),
    )
    parser.add_argument("--mf-store-cpus", type=_positive_int, default=_env_value("MF_STORE_CPUS", str(MF_STORE_CPUS)))
    parser.add_argument("--encode-cpus", type=_positive_int, default=_env_value("ENCODE_CPUS", str(ENCODE_CPUS)))
    parser.add_argument("--prefill-cpus", type=_positive_int, default=_env_value("PREFILL_CPUS", str(PREFILL_CPUS)))
    parser.add_argument("--decode-cpus", type=_positive_int, default=_env_value("DECODE_CPUS", str(DECODE_CPUS)))
    parser.add_argument("--union-cpus", type=_positive_int, default=_env_value("UNION_CPUS", str(UNION_CPUS)))
    parser.add_argument(
        "--distribution-path",
        type=_absolute_directory,
        default=_env_value("SLURM_DISTRIBUTION_PATH", SLURM_DISTRIBUTION_PATH) or None,
        help="root on every Slurm node used for distributed files and logs (default: ./slurm_workspace)",
    )
    parser.add_argument("--coordinator-service", default=_env_value("COORDINATOR_SERVICE", COORDINATOR_SERVICE))
    parser.add_argument("--controller-service", default=_env_value("CONTROLLER_SERVICE", CONTROLLER_SERVICE))
    parser.add_argument("--kvs-master-service", default=_env_value("KVS_MASTER_SERVICE", KVS_MASTER_SERVICE))
    parser.add_argument("--kv-conductor-service", default=_env_value("KV_CONDUCTOR_SERVICE", KV_CONDUCTOR_SERVICE))
    parser.add_argument("--mf-store-service", default=_env_value("MF_STORE_SERVICE", MF_STORE_SERVICE))
    parser.add_argument("--ascend-mf-store-port", default=_env_value("ASCEND_MF_STORE_PORT", ASCEND_MF_STORE_PORT))
    return parser.parse_args()


def main() -> None:
    """Run start or stop and print expected errors."""
    args = parse_arguments()
    try:
        if args.action == "start":
            if args.update_instance_num:
                result = update_instance_num(args)
            else:
                _require_service("PARTITION", args.partition)
                if args.distribution_path is None:
                    raise ValueError("--distribution-path or SLURM_DISTRIBUTION_PATH is required")
                args.distribution_path = _absolute_directory(args.distribution_path)
                result = start(args)
        else:
            if args.update_instance_num:
                raise ValueError("--update_instance_num can only be used with start")
            result = stop()
    except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        result = 1
    sys.exit(result)


if __name__ == "__main__":
    main()
