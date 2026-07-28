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
import subprocess

import lib.constant as C
from lib.utils import logger, safe_exec_cmd, load_yaml

g_controller_service = "mindie-motor-controller-service"
g_coordinator_service = "mindie-motor-coordinator-service"
g_kv_pool_service = "kvp-master"
g_kv_conductor_service = "kv-conductor"
g_kv_pool_enabled = False
g_kv_conductor_enabled = False
g_engine_base_name = "mindie-server"
g_generate_yaml_list = []
g_user_config_path = None
g_mf_store_service = "mf_store"
g_mf_store_enabled = False
g_engine_type = "vllm"


def set_user_config_path(path):
    global g_user_config_path
    g_user_config_path = path


def set_controller_service(service_name):
    global g_controller_service
    g_controller_service = service_name


def set_coordinator_service(service_name):
    global g_coordinator_service
    g_coordinator_service = service_name


def set_kv_pool_service(service_name):
    global g_kv_pool_service
    g_kv_pool_service = service_name


def set_kv_conductor_service(service_name):
    global g_kv_conductor_service
    g_kv_conductor_service = service_name


def set_mf_store_service(service_name):
    global g_mf_store_service
    g_mf_store_service = service_name


def set_engine_base_name(engine_name):
    global g_engine_base_name
    g_engine_base_name = engine_name


def update_kv_pool_enabled_flag(user_config):
    global g_kv_pool_enabled
    g_kv_pool_enabled = False

    kv_connector = user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG, {}).get(C.ENGINE_CONFIG, {})\
        .get(C.KV_TRANSFER_CONFIG, {}).get(C.KV_CONNECTOR, "")
    if kv_connector == C.MULTI_CONNECTOR:
        g_kv_pool_enabled = True


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

    g_engine_type = user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG, {}).get("engine_type", "")
    if g_engine_type == C.ENGINE_TYPE_SGLANG:
        g_mf_store_enabled = True


def get_deploy_mode_from_config(deploy_config):
    """Read deploy_mode from motor_deploy_config; default infer_service_set; validate value."""
    mode = deploy_config.get(C.DEPLOY_MODE_CONFIG_KEY, C.DEPLOY_MODE_INFER_SERVICE_SET)
    if mode not in C.VALID_DEPLOY_MODES:
        raise ValueError(
            f"motor_deploy_config.{C.DEPLOY_MODE_CONFIG_KEY} must be one of {list(C.VALID_DEPLOY_MODES)}, "
            f"got: {mode}"
        )
    return mode


def init_service_domain_name(paths, deploy_config):
    controller_data = load_yaml(paths["controller_input_yaml"], False)
    coordinator_data = load_yaml(paths["coordinator_input_yaml"], False)
    kv_pool_data = load_yaml(paths["kv_pool_input_yaml"], False)
    kv_conductor_data = load_yaml(paths["kv_conductor_input_yaml"], False)
    mf_store_data = load_yaml(paths["mf_store_input_yaml"], False)

    controller_service_data = None
    for doc in controller_data:
        if doc.get(C.KIND) == C.SERVICE:
            controller_service_data = doc
            break

    coordinator_service_data = None
    for doc in coordinator_data:
        if doc.get(C.KIND) == C.SERVICE:
            coordinator_service_data = doc
            break

    kv_pull_service_data = None
    for doc in kv_pool_data:
        if doc.get(C.KIND) == C.SERVICE:
            kv_pull_service_data = doc
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
    coordinator_name = coordinator_service_data[C.METADATA][C.NAME]
    set_coordinator_service(f"{coordinator_name}.{deploy_config[C.CONFIG_JOB_ID]}.svc.cluster.local")
    kv_pool_name = kv_pull_service_data[C.METADATA][C.NAME]
    set_kv_pool_service(f"{kv_pool_name}.{deploy_config[C.CONFIG_JOB_ID]}.svc.cluster.local")
    kv_conductor_name = kv_conductor_service_data[C.METADATA][C.NAME]
    set_kv_conductor_service(f"{kv_conductor_name}.{deploy_config[C.CONFIG_JOB_ID]}.svc.cluster.local")
    mf_store_name = mf_store_service_data[C.METADATA][C.NAME]
    set_mf_store_service(f"{mf_store_name}.{deploy_config[C.CONFIG_JOB_ID]}.svc.cluster.local")


def run_cmd_get_output(args):
    """Run command and return stdout. args: list of command and arguments. Raises on non-zero return code."""
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {result.stderr or result.stdout}")
    return result.stdout.strip()


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


def apply_configmap(create_cmd: str):
    """Create or update a configmap by applying the generated manifest."""
    safe_exec_cmd(f"{create_cmd} --dry-run=client -o yaml | kubectl apply -f -")


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
    """Extract RBAC resources (ServiceAccount, ClusterRoleBinding) from YAML docs"""
    return [doc for doc in docs if doc and doc.get(C.KIND) in (C.SERVICE_ACCOUNT, C.CLUSTER_ROLE_BINDING)]


def set_rbac_namespace(rbac_resources, namespace):
    """Set namespace for RBAC resources"""
    for rbac_resource in rbac_resources:
        if rbac_resource.get(C.KIND) == C.SERVICE_ACCOUNT:
            rbac_resource[C.METADATA][C.NAMESPACE] = namespace
        elif rbac_resource.get(C.KIND) == C.CLUSTER_ROLE_BINDING:
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
    if hardware_type == C.HARDWARE_TYPE_800I_A2:
        if C.ANNOTATIONS in metadata:
            del metadata[C.ANNOTATIONS]
        return
    annotations = metadata.setdefault(C.ANNOTATIONS, {})
    annotations[C.SP_BLOCK] = str(sp_block_num)


def modify_sp_block_num(data, pd_flag, config):
    hardware_type = config.get(C.HARDWARE_TYPE, C.HARDWARE_TYPE_800I_A2)
    if hardware_type == C.HARDWARE_TYPE_800I_A2:
        if C.ANNOTATIONS in data[C.SPEC][C.TEMPLATE][C.METADATA]:
            del data[C.SPEC][C.TEMPLATE][C.METADATA][C.ANNOTATIONS]
        return
    if pd_flag == C.NODE_TYPE_D:
        sp_block_num = int(config[C.SINGER_D_INSTANCES_NUM]) * int(config[C.D_POD_NPU_NUM])
    elif pd_flag == C.NODE_TYPE_P:
        sp_block_num = int(config[C.SINGER_P_INSTANCES_NUM]) * int(config[C.P_POD_NPU_NUM])
    else:
        return
    apply_sp_block_annotation(data[C.SPEC][C.TEMPLATE][C.METADATA], sp_block_num, hardware_type)


def create_motor_config_configmap(job_id):
    """Create or update ConfigMap motor-config with all mounted files (scripts + user_config.json)."""
    if not g_user_config_path:
        raise ValueError("g_user_config_path is not set")
    if not os.path.exists(g_user_config_path):
        raise FileNotFoundError(f"user_config file not found: {g_user_config_path}")
    apply_configmap(
        f"kubectl create configmap {C.MOTOR_CONFIG_CONFIGMAP_NAME} "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/boot.sh "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/common.sh "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/hccl_tools.py "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/mooncake_config.py "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/controller.sh "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/coordinator.sh "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/engine.sh "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/kv_pool.sh "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/kv_conductor.sh "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/mf_store.sh "
        f"--from-file=./{C.STARTUP_ROOT_PATH}/roles/all_combine_in_single_container.sh "
        "--from-file=./probe/probe.sh "
        "--from-file=./probe/probe.py "
        f"--from-file=user_config.json={g_user_config_path}"
        + " -n " + job_id
    )


def exec_all_kubectl_multi(
    deploy_config,
    baseline_config,
    deploy_mode_arg=C.DEPLOY_MODE_INFER_SERVICE_SET
):
    """Execute kubectl commands for multi-deployment or infer-service-set mode."""
    job_id = deploy_config[C.CONFIG_JOB_ID]
    out_deploy_yaml_path = C.OUTPUT_ROOT_PATH
    create_motor_config_configmap(job_id)

    if baseline_config is None:
        for yaml_file in g_generate_yaml_list:
            safe_exec_cmd(f"kubectl apply -f {yaml_file} -n {job_id}")
    elif deploy_mode_arg == C.DEPLOY_MODE_INFER_SERVICE_SET:
        for yaml_file in g_generate_yaml_list:
            safe_exec_cmd(f"kubectl apply -f {yaml_file} -n {job_id}")
    else:
        baseline_deploy_config = baseline_config.get(C.MOTOR_DEPLOY_CONFIG, {})
        elastic_distributed_engine_deploy(deploy_config, baseline_deploy_config, out_deploy_yaml_path)


def exec_all_kubectl_singer(deploy_config, yaml_file):
    """Execute kubectl commands for single container deployment."""
    job_id = deploy_config[C.CONFIG_JOB_ID]
    create_motor_config_configmap(job_id)
    safe_exec_cmd(f"kubectl apply -f {yaml_file} -n {job_id}")


def scale_engine_by_type(deploy_config, baseline_deploy_config, out_deploy_yaml_path, node_type):
    """Scale engine instances by type (p or d)."""
    from lib.generator.engine import obtain_engine_instance_total
    
    job_id = deploy_config[C.CONFIG_JOB_ID]
    totals = obtain_engine_instance_total(deploy_config)
    bases = obtain_engine_instance_total(baseline_deploy_config)
    total = totals[0] if node_type == C.NODE_TYPE_P else totals[1]
    base = bases[0] if node_type == C.NODE_TYPE_P else bases[1]
    if total < base:
        logger.info(f"Scale-in {node_type} instance, {base} -> {total}")
        for index in reversed(range(total, base)):
            yaml_path = os.path.join(out_deploy_yaml_path, f"{g_engine_base_name}_{node_type}{index}.yaml")
            safe_exec_cmd(f"kubectl delete -f {yaml_path} -n {job_id}")
            if os.path.exists(yaml_path):
                os.remove(yaml_path)
    if total > base:
        logger.info(f"Scale-out {node_type} instance, {base} -> {total}")
        for index in range(base, total):
            yaml_path = os.path.join(out_deploy_yaml_path, f"{g_engine_base_name}_{node_type}{index}.yaml")
            safe_exec_cmd(f"kubectl apply -f {yaml_path} -n {job_id}")


def elastic_distributed_engine_deploy(deploy_config, baseline_deploy_config, out_deploy_yaml_path):
    """Elastic distributed engine deployment - scale in/out engine instances."""
    scale_engine_by_type(deploy_config, baseline_deploy_config, out_deploy_yaml_path, C.NODE_TYPE_P)
    scale_engine_by_type(deploy_config, baseline_deploy_config, out_deploy_yaml_path, C.NODE_TYPE_D)
    logger.info("Engine scale done.")


def apply_yaml_files(deploy_config):
    job_id = deploy_config[C.CONFIG_JOB_ID]
    create_motor_config_configmap(job_id)
    for yaml_file in g_generate_yaml_list:
        safe_exec_cmd(f"kubectl apply -f {yaml_file} -n {job_id}")


def apply_single_yaml(deploy_config, yaml_file):
    job_id = deploy_config[C.CONFIG_JOB_ID]
    create_motor_config_configmap(job_id)
    safe_exec_cmd(f"kubectl apply -f {yaml_file} -n {job_id}")


def scale_engine(deploy_config, baseline_deploy_config):
    job_id = deploy_config[C.CONFIG_JOB_ID]
    out_deploy_yaml_path = C.OUTPUT_ROOT_PATH
    create_motor_config_configmap(job_id)
    elastic_distributed_engine_deploy(deploy_config, baseline_deploy_config, out_deploy_yaml_path)
