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
import shutil
import subprocess

import lib.constant as C
from lib.utils import logger, safe_exec_cmd, load_yaml, pipe_kubectl

g_controller_service = "mindie-motor-controller-service"
g_coordinator_service = "mindie-motor-coordinator-mgmt"
g_coordinator_infer_service = "mindie-motor-coordinator-infer"
g_coordinator_obs_service = "mindie-motor-coordinator-obs"
g_kv_store_service = "mindie-motor-kvs-master"
g_kv_conductor_service = "kv-conductor"
g_kv_store_enabled = False
g_kv_conductor_enabled = False
g_kv_cache_store_port = C.DEFAULT_KV_CACHE_STORE_PORT
g_kv_store_backend = C.DEFAULT_KV_STORE_BACKEND
g_mmc_config_store_port = C.DEFAULT_MMC_CONFIG_STORE_PORT
g_mmc_metrics_port = C.DEFAULT_MMC_METRICS_PORT
g_mmc_local_service_mode = ""  # 空 → 由 common.sh 按硬件默认
g_mmc_dram_size = ""  # 空 → daemon 使用默认 10GB
g_engine_base_name = "mindie-server"
g_generate_yaml_list = []
g_user_config_path = None
g_mf_store_service = "mf_store"
g_mf_store_enabled = False
g_engine_type = "vllm"


def set_user_config_path(path):
    global g_user_config_path
    g_user_config_path = path


def build_kv_store_env_items():
    """Return KV-store env items that cannot be derived from config."""
    items = [
        {C.NAME: C.ENV_KVS_MASTER_SERVICE, C.VALUE: g_kv_store_service},
        {C.NAME: C.ENV_KV_STORE_BACKEND, C.VALUE: g_kv_store_backend},
    ]
    if g_kv_store_backend == C.MMC_STORE_BACKEND:
        # memcache C++ layer reads this env var directly at engine startup
        items.append(
            {C.NAME: C.ENV_MMC_LOCAL_CONFIG_PATH, C.VALUE: C.DEFAULT_MMC_LOCAL_CONFIG_PATH},
        )
    return items


def set_controller_service(service_name):
    global g_controller_service
    g_controller_service = service_name


def set_coordinator_service(service_name):
    global g_coordinator_service
    g_coordinator_service = service_name


def set_coordinator_infer_service(service_name):
    global g_coordinator_infer_service
    g_coordinator_infer_service = service_name


def set_coordinator_obs_service(service_name):
    global g_coordinator_obs_service
    g_coordinator_obs_service = service_name


def set_kv_store_service(service_name):
    global g_kv_store_service
    g_kv_store_service = service_name


def set_kv_conductor_service(service_name):
    global g_kv_conductor_service
    g_kv_conductor_service = service_name


def set_mf_store_service(service_name):
    global g_mf_store_service
    g_mf_store_service = service_name


def set_engine_base_name(engine_name):
    global g_engine_base_name
    g_engine_base_name = engine_name


def update_kv_store_enabled_flag(user_config):
    global g_kv_store_enabled
    g_kv_store_enabled = False

    engine_section = user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG) or user_config.get(C.MOTOR_ENGINE_UNION_CONFIG, {})
    kv_connector = engine_section.get(C.ENGINE_CONFIG, {}).get(C.KV_TRANSFER_CONFIG, {}).get(C.KV_CONNECTOR, "")
    kv_store_cfg = user_config.get(C.KV_CACHE_STORE_CONFIG)
    if kv_connector == C.MULTI_CONNECTOR or (isinstance(kv_store_cfg, dict) and kv_store_cfg):
        g_kv_store_enabled = True


def update_kv_conductor_enabled_flag(user_config):
    global g_kv_conductor_enabled
    g_kv_conductor_enabled = False

    kv_conductor_config = user_config.get(C.KV_CONDUCTOR_CONFIG, None)
    if kv_conductor_config is None:
        return
    http_server_port = kv_conductor_config.get(C.KV_CONDUCTOR_PORT, 0)
    if http_server_port != 0:
        g_kv_conductor_enabled = True


def update_engine_type_flag(user_config):
    global g_engine_type
    global g_mf_store_enabled
    g_mf_store_enabled = False

    engine_section = user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG) or user_config.get(C.MOTOR_ENGINE_UNION_CONFIG, {})
    g_engine_type = engine_section.get("engine_type", "")
    if g_engine_type == C.ENGINE_TYPE_SGLANG:
        g_mf_store_enabled = True


def get_deploy_mode_from_config(deploy_config):
    """Read deploy_mode from motor_deploy_config; default infer_service_set; validate value."""
    mode = deploy_config.get(C.DEPLOY_MODE_CONFIG_KEY, C.DEPLOY_MODE_INFER_SERVICE_SET)
    if mode not in C.VALID_DEPLOY_MODES:
        raise ValueError(
            f"motor_deploy_config.{C.DEPLOY_MODE_CONFIG_KEY} must be one of {list(C.VALID_DEPLOY_MODES)}, got: {mode}"
        )
    return mode


def _pick_coordinator_services(docs: list[dict]):
    """Return a dict mapping port->Service for all coordinator Services.

    The coordinator template defines three separate Services:
      - mindie-motor-coordinator-infer (NodePort, port 1025)
      - mindie-motor-coordinator-mgmt  (ClusterIP, port 1026)
      - mindie-motor-coordinator-obs   (NodePort, port 1027)
    Each is identified by its port for robust matching.
    """
    result = {}
    for doc in docs:
        if doc.get(C.KIND) == C.SERVICE:
            for port_entry in doc.get("spec", {}).get("ports", []):
                port = port_entry.get("port")
                if port in (1025, 1026, 1027):
                    result[port] = doc
                    break
    return result


def init_service_domain_name(paths, deploy_config):
    controller_data = load_yaml(paths["controller_input_yaml"], False)
    coordinator_data = load_yaml(paths["coordinator_input_yaml"], False)
    kv_store_data = load_yaml(paths["kv_store_input_yaml"], False)
    kv_conductor_data = load_yaml(paths["kv_conductor_input_yaml"], False)
    mf_store_data = load_yaml(paths["mf_store_input_yaml"], False)

    controller_service_data = None
    for doc in controller_data:
        if doc.get(C.KIND) == C.SERVICE:
            controller_service_data = doc
            break

    coord_services = _pick_coordinator_services(coordinator_data)

    kv_store_service_data = None
    for doc in kv_store_data:
        if doc.get(C.KIND) == C.SERVICE:
            kv_store_service_data = doc
            break

    kv_conductor_service_data = None
    for doc in kv_conductor_data:
        if doc.get(C.KIND) == C.SERVICE:
            kv_conductor_service_data = doc
            break

    mf_store_service_data = None
    for doc in mf_store_data:
        if doc.get(C.KIND) == C.SERVICE:
            mf_store_service_data = doc
            break

    controller_name = controller_service_data[C.METADATA][C.NAME]
    set_controller_service(f"{controller_name}.{deploy_config[C.CONFIG_JOB_ID]}.svc.cluster.local")

    ns = deploy_config[C.CONFIG_JOB_ID]
    infer_svc = coord_services.get(1025)
    mgmt_svc = coord_services.get(1026)
    obs_svc = coord_services.get(1027)
    if infer_svc:
        set_coordinator_infer_service(f"{infer_svc[C.METADATA][C.NAME]}.{ns}.svc.cluster.local")
    if mgmt_svc:
        set_coordinator_service(f"{mgmt_svc[C.METADATA][C.NAME]}.{ns}.svc.cluster.local")
    if obs_svc:
        set_coordinator_obs_service(f"{obs_svc[C.METADATA][C.NAME]}.{ns}.svc.cluster.local")

    kv_store_svc_name = kv_store_service_data[C.METADATA][C.NAME]
    set_kv_store_service(f"{kv_store_svc_name}.{deploy_config[C.CONFIG_JOB_ID]}.svc.cluster.local")
    kv_conductor_name = kv_conductor_service_data[C.METADATA][C.NAME]
    set_kv_conductor_service(f"{kv_conductor_name}.{deploy_config[C.CONFIG_JOB_ID]}.svc.cluster.local")
    mf_store_name = mf_store_service_data[C.METADATA][C.NAME]
    set_mf_store_service(f"{mf_store_name}.{deploy_config[C.CONFIG_JOB_ID]}.svc.cluster.local")


def run_cmd_get_output(args, timeout=60):
    """Run command and return stdout. args: list of command and arguments. Raises on non-zero return code."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        cmd = " ".join(args)
        raise RuntimeError(f"Command timed out after {timeout}s: {cmd}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {result.stderr or result.stdout}")
    return result.stdout.strip()


_g_accelerator_type_cache = {}


def _get_kubectl_path():
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl not found in PATH")
    return kubectl


def _get_cluster_nodes(label_selector):
    kubectl = _get_kubectl_path()
    out = run_cmd_get_output([kubectl, "get", "nodes", "-l", label_selector, "-o", "json"])
    return json.loads(out).get("items", [])


def _collect_node_accelerator_types(nodes):
    accelerator_types = set()
    for node in nodes:
        labels = node.get("metadata", {}).get("labels", {})
        accelerator_type = labels.get(C.ACCELERATOR_TYPE)
        if accelerator_type:
            accelerator_types.add(accelerator_type)
    return accelerator_types


def _matches_hardware_generation(accelerator_type, hardware_type):
    if hardware_type in C.HARDWARE_TYPE_A2:
        return "910b" in accelerator_type.lower()
    if hardware_type in C.HARDWARE_TYPE_A3:
        return "a3" in accelerator_type.lower()
    return True


def _resolve_accelerator_type_from_nodes(nodes, hardware_type):
    accelerator_types = _collect_node_accelerator_types(nodes)
    if not accelerator_types:
        raise RuntimeError(f"Matched nodes for hardware_type={hardware_type} do not have label {C.ACCELERATOR_TYPE}")

    matched_types = {value for value in accelerator_types if _matches_hardware_generation(value, hardware_type)}
    if not matched_types:
        raise RuntimeError(
            f"No {C.ACCELERATOR_TYPE} on cluster matches hardware_type={hardware_type}. "
            f"Found values: {sorted(accelerator_types)}"
        )
    if len(matched_types) == 1:
        return next(iter(matched_types))

    raise RuntimeError(
        f"Multiple {C.ACCELERATOR_TYPE} values match hardware_type={hardware_type}: {sorted(matched_types)}"
    )


def get_accelerator_type_from_cluster(hardware_type):
    """Resolve accelerator-type node label value from cluster nodes via kubectl."""
    if hardware_type in _g_accelerator_type_cache:
        return _g_accelerator_type_cache[hardware_type]

    if hardware_type in C.HARDWARE_TYPE_950I_A5:
        label_selector = f"{C.ACCELERATOR}={C.ACCELERATOR_A5},{C.ACCELERATOR_TYPE}={hardware_type}"
        nodes = _get_cluster_nodes(label_selector)
        if not nodes:
            raise RuntimeError(f"No node in cluster matches {label_selector} for hardware_type={hardware_type}")
        accelerator_type = hardware_type
    elif hardware_type in C.HARDWARE_TYPE_A2 or hardware_type in C.HARDWARE_TYPE_A3:
        nodes = _get_cluster_nodes(f"{C.ACCELERATOR}={C.ACCELERATOR_910}")
        if not nodes:
            raise RuntimeError(
                f"No node in cluster with label {C.ACCELERATOR}={C.ACCELERATOR_910} for hardware_type={hardware_type}"
            )
        accelerator_type = _resolve_accelerator_type_from_nodes(nodes, hardware_type)
    else:
        known = [*sorted(C.HARDWARE_TYPE_A2), *sorted(C.HARDWARE_TYPE_A3), *C.HARDWARE_TYPE_950I_A5]
        raise ValueError(f"Unknown hardware_type '{hardware_type}'. Supported values: {known}")

    logger.info(
        "Resolved %s=%s from cluster for hardware_type=%s",
        C.ACCELERATOR_TYPE,
        accelerator_type,
        hardware_type,
    )
    _g_accelerator_type_cache[hardware_type] = accelerator_type
    return accelerator_type


def get_baseline_config_from_configmap(job_id):
    """Get current deployed user_config from cluster ConfigMap. Returns None if CM missing or no user_config."""
    try:
        out = run_cmd_get_output(
            ["kubectl", "get", "configmap", C.MOTOR_CONFIG_CONFIGMAP_NAME, "-n", job_id, "-o", "json"]
        )
        data = json.loads(out)
        if C.DATA not in data or "user_config.json" not in data[C.DATA]:
            return None
        return json.loads(data[C.DATA]["user_config.json"])
    except (RuntimeError, json.JSONDecodeError, KeyError):
        return None


def apply_configmap(create_cmd):
    """Create or update a configmap by applying the generated manifest."""
    pipe_kubectl(create_cmd)


def extract_resources(data):
    """Extract deployment, services, and RBAC resources from YAML data"""
    deployment_data = None
    service_list = []
    rbac_resources = []

    if isinstance(data, list):
        for item in data:
            if item.get(C.KIND) == C.DEPLOYMENT_KIND:
                deployment_data = item
            elif item.get(C.KIND) == C.SERVICE:
                service_list.append(item)
            else:
                rbac_resources.append(item)
    else:
        deployment_data = data

    return deployment_data, service_list, rbac_resources


def extract_rbac_resources(docs):
    """Extract RBAC resources (ServiceAccount, ClusterRole, ClusterRoleBinding) from YAML docs"""
    return [
        doc for doc in docs if doc and doc.get(C.KIND) in (C.SERVICE_ACCOUNT, "ClusterRole", C.CLUSTER_ROLE_BINDING)
    ]


def set_rbac_namespace(rbac_resources, namespace):
    """Set namespace for RBAC resources"""
    for rbac_resource in rbac_resources:
        if rbac_resource.get(C.KIND) == C.SERVICE_ACCOUNT:
            rbac_resource[C.METADATA][C.NAMESPACE] = namespace
        elif rbac_resource.get(C.KIND) == "ClusterRole":
            rbac_resource[C.METADATA][C.NAME] = f"{rbac_resource[C.METADATA][C.NAME]}-{namespace}"
        elif rbac_resource.get(C.KIND) == C.CLUSTER_ROLE_BINDING:
            rbac_resource[C.METADATA][C.NAME] = f"{rbac_resource[C.METADATA][C.NAME]}-{namespace}"
            # Update roleRef to reference the namespace-scoped ClusterRole name
            role_ref = rbac_resource.get("roleRef")
            if role_ref:
                role_ref[C.NAME] = f"{role_ref[C.NAME]}-{namespace}"
            if C.SUBJECTS in rbac_resource:
                for subject in rbac_resource[C.SUBJECTS]:
                    if subject.get(C.KIND) == C.SERVICE_ACCOUNT:
                        subject[C.NAMESPACE] = namespace


def set_services_namespace(service_list, namespace):
    """Set namespace for all services"""
    for service_data in service_list:
        service_data[C.METADATA][C.NAMESPACE] = namespace


def apply_sp_block_annotation(metadata, sp_block_num, hardware_type):
    """Apply sp_block annotation based on hardware type"""
    if hardware_type in C.HARDWARE_TYPE_A2:
        if C.ANNOTATIONS in metadata:
            del metadata[C.ANNOTATIONS]
        return
    annotations = metadata.setdefault(C.ANNOTATIONS, {})
    if C.SP_BLOCK in annotations:
        logger.info(
            "Skip setting %s annotation to %s because template already configures it as %s",
            C.SP_BLOCK,
            sp_block_num,
            annotations[C.SP_BLOCK],
        )
        return
    annotations[C.SP_BLOCK] = str(sp_block_num)


def modify_sp_block_num(data, pd_flag, config):
    hardware_type = config.get(C.HARDWARE_TYPE, C.HARDWARE_TYPE_800I_A2)
    if hardware_type in C.HARDWARE_TYPE_A2:
        if C.ANNOTATIONS in data[C.SPEC][C.TEMPLATE][C.METADATA]:
            del data[C.SPEC][C.TEMPLATE][C.METADATA][C.ANNOTATIONS]
        return
    if pd_flag == C.NODE_TYPE_E:
        sp_block_num = int(config[C.SINGER_E_INSTANCES_NUM]) * int(config[C.E_POD_NPU_NUM])
    elif pd_flag == C.NODE_TYPE_D:
        sp_block_num = int(config[C.SINGER_D_INSTANCES_NUM]) * int(config[C.D_POD_NPU_NUM])
    elif pd_flag == C.NODE_TYPE_P:
        sp_block_num = int(config[C.SINGER_P_INSTANCES_NUM]) * int(config[C.P_POD_NPU_NUM])
    elif pd_flag == C.NODE_TYPE_U:
        sp_block_num = int(config[C.SINGLE_HYBRID_INSTANCE_POD_NUM]) * int(config[C.HYBRID_POD_NPU_NUM])
    else:
        return
    apply_sp_block_annotation(data[C.SPEC][C.TEMPLATE][C.METADATA], sp_block_num, hardware_type)


def _user_config_path_for_configmap(user_config=None, effective_deploy_mode=None):
    """Return path to user_config.json for ConfigMap; inject effective deploy_mode when requested."""
    if user_config is None or effective_deploy_mode is None:
        if not g_user_config_path:
            raise ValueError("g_user_config_path is not set")
        if not os.path.exists(g_user_config_path):
            raise FileNotFoundError(f"user_config file not found: {g_user_config_path}")
        return g_user_config_path

    config_copy = json.loads(json.dumps(user_config))
    motor_deploy = config_copy.setdefault(C.MOTOR_DEPLOY_CONFIG, {})
    motor_deploy[C.DEPLOY_MODE_CONFIG_KEY] = effective_deploy_mode
    os.makedirs(C.OUTPUT_ROOT_PATH, exist_ok=True)
    effective_path = os.path.join(C.OUTPUT_ROOT_PATH, ".motor_config_user_config.json")
    with open(effective_path, "w", encoding="utf-8") as f:
        json.dump(config_copy, f, indent=2)
        f.write("\n")
    return effective_path


def create_motor_config_configmap(job_id, user_config=None, effective_deploy_mode=None):
    """Create or update ConfigMap motor-config with all mounted files (scripts + user_config.json)."""
    config_path = _user_config_path_for_configmap(user_config, effective_deploy_mode)
    apply_configmap(
        [
            "kubectl",
            "create",
            "configmap",
            C.MOTOR_CONFIG_CONFIGMAP_NAME,
            f"--from-file=./{C.STARTUP_ROOT_PATH}/boot.sh",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/common.sh",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/hccl_tools.py",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/kv_store_backends/mooncake/mooncake_config.py",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/controller.sh",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/coordinator.sh",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/engine.sh",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/kv_cache_store.sh",
            f"--from-file=kv_store_backends.mooncake.mooncake.sh=./{C.STARTUP_ROOT_PATH}/roles/kv_store_backends/mooncake/mooncake.sh",
            f"--from-file=kv_store_backends.memcache.memcache.sh=./{C.STARTUP_ROOT_PATH}/roles/kv_store_backends/memcache/memcache.sh",
            f"--from-file=kv_store_backends.memcache.memcache_meta_service.py=./{C.STARTUP_ROOT_PATH}/roles/kv_store_backends/memcache/memcache_meta_service.py",
            f"--from-file=kv_store_backends.memcache.mmc-local.conf=./{C.STARTUP_ROOT_PATH}/roles/kv_store_backends/memcache/mmc-local.conf",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/kv_conductor.sh",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/mf_store.sh",
            f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/all_combine_in_single_container.sh",
            "--from-file=./probe/probe.sh",
            "--from-file=./probe/probe.py",
            "--from-file=./prestop/prestop.sh",
            "--from-file=./prestop/prestop.py",
            f"--from-file=user_config.json={config_path}",
            "-n",
            job_id,
        ]
    )


def exec_all_kubectl_multi(
    deploy_config,
    baseline_config,
    deploy_mode_arg=C.DEPLOY_MODE_INFER_SERVICE_SET,
    user_config=None,
):
    """Execute kubectl commands for multi-deployment or infer-service-set mode."""
    job_id = deploy_config[C.CONFIG_JOB_ID]
    out_deploy_yaml_path = C.OUTPUT_ROOT_PATH
    create_motor_config_configmap(job_id, user_config=user_config, effective_deploy_mode=deploy_mode_arg)

    if baseline_config is None:
        for yaml_file in g_generate_yaml_list:
            safe_exec_cmd(["kubectl", "apply", "-f", yaml_file, "-n", job_id])
    elif deploy_mode_arg == C.DEPLOY_MODE_INFER_SERVICE_SET:
        for yaml_file in g_generate_yaml_list:
            safe_exec_cmd(["kubectl", "apply", "-f", yaml_file, "-n", job_id])
    else:
        baseline_deploy_config = baseline_config.get(C.MOTOR_DEPLOY_CONFIG, {})
        elastic_distributed_engine_deploy(deploy_config, baseline_deploy_config, out_deploy_yaml_path)


def exec_all_kubectl_singer(deploy_config, yaml_file):
    """Execute kubectl commands for single container deployment."""
    job_id = deploy_config[C.CONFIG_JOB_ID]
    create_motor_config_configmap(job_id)
    safe_exec_cmd(["kubectl", "apply", "-f", yaml_file, "-n", job_id])


def scale_engine_by_type(deploy_config, baseline_deploy_config, out_deploy_yaml_path, node_type):
    """Scale engine instances by type (p, d or u)."""
    from lib.utils import obtain_engine_instance_total

    job_id = deploy_config[C.CONFIG_JOB_ID]
    totals = obtain_engine_instance_total(deploy_config)
    bases = obtain_engine_instance_total(baseline_deploy_config)
    if node_type in (C.NODE_TYPE_P, C.NODE_TYPE_U):
        total = totals[0]
        base = bases[0]
    else:
        total = totals[1]
        base = bases[1]
    if total < base:
        logger.info("Scale-in %s instance, %s -> %s", node_type, base, total)
        for index in reversed(range(total, base)):
            yaml_path = os.path.join(out_deploy_yaml_path, f"{g_engine_base_name}_{node_type}{index}.yaml")
            safe_exec_cmd(["kubectl", "delete", "-f", yaml_path, "-n", job_id])
            if os.path.exists(yaml_path):
                os.remove(yaml_path)
    if total > base:
        logger.info("Scale-out %s instance, %s -> %s", node_type, base, total)
        for index in range(base, total):
            yaml_path = os.path.join(out_deploy_yaml_path, f"{g_engine_base_name}_{node_type}{index}.yaml")
            safe_exec_cmd(["kubectl", "apply", "-f", yaml_path, "-n", job_id])


def scale_engine_e_by_type(deploy_config, baseline_deploy_config, out_deploy_yaml_path):
    """Scale engine instances by type (p, d or u)."""
    from lib.utils import obtain_engine_e_instance_total

    job_id = deploy_config[C.CONFIG_JOB_ID]
    total = obtain_engine_e_instance_total(deploy_config)
    base = obtain_engine_e_instance_total(baseline_deploy_config)
    if total < base:
        logger.info("Scale-in %s instance, %s -> %s", C.NODE_TYPE_E, base, total)
        for index in reversed(range(total, base)):
            yaml_path = os.path.join(out_deploy_yaml_path, f"{g_engine_base_name}_{C.NODE_TYPE_E}{index}.yaml")
            safe_exec_cmd(["kubectl", "delete", "-f", yaml_path, "-n", job_id])
            if os.path.exists(yaml_path):
                os.remove(yaml_path)
    if total > base:
        logger.info("Scale-out %s instance, %s -> %s", C.NODE_TYPE_E, base, total)
        for index in range(base, total):
            yaml_path = os.path.join(out_deploy_yaml_path, f"{g_engine_base_name}_{C.NODE_TYPE_E}{index}.yaml")
            safe_exec_cmd(["kubectl", "apply", "-f", yaml_path, "-n", job_id])


def elastic_distributed_engine_deploy(deploy_config, baseline_deploy_config, out_deploy_yaml_path):
    """Elastic distributed engine deployment - scale in/out engine instances."""
    if C.E_INSTANCES_NUM in deploy_config:
        scale_engine_e_by_type(deploy_config, baseline_deploy_config, out_deploy_yaml_path)

    if C.HYBRID_INSTANCES_NUM in deploy_config:
        scale_engine_by_type(deploy_config, baseline_deploy_config, out_deploy_yaml_path, C.NODE_TYPE_U)
        logger.info("Engine scale done.")
        return

    scale_engine_by_type(deploy_config, baseline_deploy_config, out_deploy_yaml_path, C.NODE_TYPE_P)
    scale_engine_by_type(deploy_config, baseline_deploy_config, out_deploy_yaml_path, C.NODE_TYPE_D)
    logger.info("Engine scale done.")


def apply_yaml_files(deploy_config):
    job_id = deploy_config[C.CONFIG_JOB_ID]
    create_motor_config_configmap(job_id)
    for yaml_file in g_generate_yaml_list:
        safe_exec_cmd(["kubectl", "apply", "-f", yaml_file, "-n", job_id])


def apply_single_yaml(deploy_config, yaml_file):
    job_id = deploy_config[C.CONFIG_JOB_ID]
    create_motor_config_configmap(job_id)
    safe_exec_cmd(["kubectl", "apply", "-f", yaml_file, "-n", job_id])


def scale_engine(deploy_config, baseline_deploy_config):
    job_id = deploy_config[C.CONFIG_JOB_ID]
    out_deploy_yaml_path = C.OUTPUT_ROOT_PATH
    create_motor_config_configmap(job_id)
    elastic_distributed_engine_deploy(deploy_config, baseline_deploy_config, out_deploy_yaml_path)
