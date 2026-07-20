# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
import os

import lib.constant as C
from lib.utils import (
    generate_unique_id,
    load_yaml,
    logger,
    write_yaml,
    obtain_engine_instance_total,
    obtain_engine_e_instance_total,
)
from lib.generator import k8s_utils
from lib.generator.k8s_utils import (
    set_controller_service,
    set_coordinator_service,
    set_coordinator_infer_service,
    set_coordinator_obs_service,
    set_kv_store_service,
    set_kv_conductor_service,
    set_rbac_namespace,
    extract_rbac_resources,
    apply_sp_block_annotation,
)
from lib.generator.engine import (
    build_engine_env_items,
    set_container_npu,
    apply_node_selector_by_hardware,
    set_weight_mount,
    is_hybrid_deploy,
    apply_pd_heterogeneous_node_selector,
    apply_a5_workload,
    apply_a5_engine_pod_config,
    apply_a5_dns_config,
)
from lib.generator.storage import apply_storage_volumes, apply_dshm_size
from lib.generator.kv_cache_store import normalize_kv_cache_store_config, gen_kv_store_env
from lib.generator.kv_conductor import normalize_kv_conductor_config


def get_infer_role(infer_service_set, role_name):
    """Get role by name from InferServiceSet spec.template.roles."""
    roles = infer_service_set.get(C.SPEC, {}).get(C.TEMPLATE, {}).get(C.ROLES, [])
    for role in roles:
        if role.get(C.NAME) == role_name:
            return role
    return None


def set_container_env(container, env_list):
    """Append or update env vars in container."""
    if C.ENV not in container:
        container[C.ENV] = []
    existing_names = {e[C.NAME] for e in container[C.ENV] if isinstance(e, dict) and C.NAME in e}
    for env_item in env_list:
        name = env_item.get(C.NAME)
        if name not in existing_names:
            container[C.ENV].append(env_item)
            existing_names.add(name)


def _find_infer_service_set_doc(all_docs):
    for doc in all_docs:
        if doc and doc.get(C.KIND) == "InferServiceSet":
            return doc
    raise ValueError("InferServiceSet document not found in infer_service_template.yaml")


def _configure_control_role(infer_doc, user_config, role_name, config_key):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    role = get_infer_role(infer_doc, role_name)
    if not role:
        return None
    role[C.REPLICAS] = 1
    cfg = user_config.get(config_key, {})
    standby_cfg = cfg.get(C.STANDBY_CONFIG, {})
    replicas = 2 if standby_cfg.get(C.ENABLE_MASTER_STANDBY) else 1
    workload_spec = role.setdefault(C.SPEC, {})
    workload_spec[C.REPLICAS] = replicas
    template = workload_spec.setdefault(C.TEMPLATE, {})
    pod_spec = template.setdefault(C.SPEC, {})
    containers = pod_spec.get(C.CONTAINERS, [])
    if not containers:
        return None
    container = containers[0]
    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]
    job_id = deploy_config[C.CONFIG_JOB_ID]
    uuid_spec = generate_unique_id()
    job_name = f"{job_id}-{role_name}-{uuid_spec}"
    set_container_env(container, build_engine_env_items(role_name, deploy_config, job_name))
    apply_a5_dns_config(pod_spec, deploy_config)
    return container


def _configure_controller_role(infer_doc, user_config):
    _configure_control_role(infer_doc, user_config, C.CONTROLLER, C.MOTOR_CONTROLLER_CONFIG)


def _configure_coordinator_role(infer_doc, user_config):
    container = _configure_control_role(infer_doc, user_config, C.COORDINATOR, C.MOTOR_COORDINATOR_CONFIG)
    if not container:
        return

    coordinator_env = list(k8s_utils.build_kv_store_env_items())
    if k8s_utils.g_kv_conductor_enabled:
        coordinator_env.append({C.NAME: C.ENV_KV_CONDUCTOR_SERVICE, C.VALUE: k8s_utils.g_kv_conductor_service})

    disaggregation_bootstrap_port = (
        user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG, {})
        .get(C.ENGINE_CONFIG, {})
        .get("disaggregation_bootstrap_port", "")
    )
    if disaggregation_bootstrap_port:
        coordinator_env.append(
            {C.NAME: C.ENV_DISAGGREGATION_BOOTSTRAP_PORT, C.VALUE: str(disaggregation_bootstrap_port)}
        )

    if coordinator_env:
        set_container_env(container, coordinator_env)


def _apply_infer_node_selector_and_sp_block(deploy_config, pod_spec, template, pods_key, npu_key, role_name=None):
    hardware_type = deploy_config.get(C.HARDWARE_TYPE, C.HARDWARE_TYPE_800I_A2)
    pod_spec[C.NODE_SELECTOR] = pod_spec.get(C.NODE_SELECTOR, {})
    apply_node_selector_by_hardware(pod_spec, hardware_type)
    if role_name:
        node_type = {C.ROLE_PREFILL: C.NODE_TYPE_P, C.ROLE_DECODE: C.NODE_TYPE_D}.get(role_name)
        if node_type:
            apply_pd_heterogeneous_node_selector(pod_spec, deploy_config, node_type)

    if hardware_type in C.HARDWARE_TYPE_A3 or hardware_type in C.HARDWARE_TYPE_950I_A5:
        # CRD uses StatefulSet; MindCluster sp-block differs from Deployment (see engine.py multi_deployment)
        sp_block_num = int(deploy_config.get(pods_key, 1)) * int(deploy_config.get(npu_key, 1))
        apply_sp_block_annotation(template.setdefault(C.METADATA, {}), sp_block_num, hardware_type)
    if hardware_type in C.HARDWARE_TYPE_950I_A5:
        apply_a5_workload(template, deploy_config)


def _zero_engine_role_replicas(infer_doc, role_name):
    role = get_infer_role(infer_doc, role_name)
    if role:
        role[C.REPLICAS] = 0


def _configure_engine_role(infer_doc, user_config, infer_name, role_name):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    role = get_infer_role(infer_doc, role_name)
    if not role:
        return
    if role_name == C.ROLE_UNION:
        instances_key = C.HYBRID_INSTANCES_NUM
        pods_key = C.SINGLE_HYBRID_INSTANCE_POD_NUM
        npu_key = C.HYBRID_POD_NPU_NUM
        env_role = C.ROLE_UNION
    else:
        prefix_map = {C.ROLE_PREFILL: "p", C.ROLE_DECODE: "d", C.ROLE_ENCODE: "e"}
        prefix = prefix_map.get(role_name)
        if not prefix:
            return
        instances_key = f"{prefix}_instances_num"
        pods_key = f"single_{prefix}_instance_pod_num"
        npu_key = f"{prefix}_pod_npu_num"
        env_role = role_name

    total_instances = int(deploy_config.get(instances_key, 1))
    single_instance = int(deploy_config.get(pods_key, 1))
    role[C.REPLICAS] = total_instances
    workload_spec = role.setdefault(C.SPEC, {})
    workload_spec[C.REPLICAS] = single_instance
    selector = workload_spec.setdefault(C.SELECTOR, {}).setdefault(C.MATCHLABELS, {})
    selector[C.APP] = infer_name
    template = workload_spec.setdefault(C.TEMPLATE, {})
    template.setdefault(C.METADATA, {}).setdefault(C.LABELS, {})[C.APP] = infer_name
    pod_spec = template.setdefault(C.SPEC, {})
    containers = pod_spec.get(C.CONTAINERS, [])
    if not containers:
        return
    container = containers[0]
    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]
    container[C.NAME] = infer_name
    job_id = deploy_config[C.CONFIG_JOB_ID]
    job_name_base = f"{job_id}-{infer_name}"
    set_container_env(
        container,
        build_engine_env_items(env_role, deploy_config, job_name_base, include_kv_store=True),
    )
    npu_num = int(deploy_config.get(npu_key, 1))
    set_container_npu(container, npu_num, deploy_config)
    weight_path = deploy_config.get(C.WEIGHT_MOUNT_PATH, C.DEFAULT_WEIGHT_MOUNT_PATH)
    set_weight_mount(pod_spec, container, weight_path)
    apply_storage_volumes(pod_spec, container, user_config)
    apply_dshm_size(pod_spec, user_config)
    apply_a5_engine_pod_config(pod_spec, container, deploy_config)
    _apply_infer_node_selector_and_sp_block(deploy_config, pod_spec, template, pods_key, npu_key, role_name)


def _set_role_primary_service_port(role, service_port):
    services = role.get(C.SERVICES, [])
    if not services:
        raise ValueError(f"Service definition not found for role '{role.get(C.NAME)}' in infer_service_template.yaml")
    ports = services[0].get(C.SPEC, {}).get(C.PORTS, [])
    if not ports:
        raise ValueError(f"Missing required service ports for role '{role.get(C.NAME)}' in infer_service_template.yaml")
    ports[0][C.PORT] = service_port
    ports[0][C.TARGET_PORT] = service_port


def _configure_kv_store_role(infer_doc, user_config):
    role = get_infer_role(infer_doc, C.ROLE_KV_STORE)
    if not role:
        return
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    workload_spec = role.setdefault(C.SPEC, {})
    template = workload_spec.setdefault(C.TEMPLATE, {})
    pod_spec = template.setdefault(C.SPEC, {})
    containers = pod_spec.get(C.CONTAINERS, [])
    if containers:
        containers[0][C.IMAGE] = deploy_config[C.IMAGE_NAME]
    if not k8s_utils.g_kv_store_enabled:
        role[C.REPLICAS] = 0
        workload_spec[C.REPLICAS] = 1
        return

    kv_store_config = normalize_kv_cache_store_config(user_config)
    role[C.REPLICAS] = 1
    workload_spec[C.REPLICAS] = 1
    _set_role_primary_service_port(role, kv_store_config[C.KV_CACHE_STORE_PORT])
    # Sync memcache MetaService ports from config (port indices 1,2 in template)
    backend = kv_store_config.get(C.KV_STORE_BACKEND, C.DEFAULT_KV_STORE_BACKEND)
    if backend == C.MMC_STORE_BACKEND:
        services = role.get(C.SERVICES, [])
        if services:
            ports = services[0].get(C.SPEC, {}).get(C.PORTS, [])
            if len(ports) > 1:
                config_store_port = kv_store_config.get(C.MMC_CONFIG_STORE_PORT_KEY, C.DEFAULT_MMC_CONFIG_STORE_PORT)
                ports[1][C.PORT] = config_store_port
                ports[1][C.TARGET_PORT] = config_store_port
            if len(ports) > 2:
                metrics_port = kv_store_config.get(C.MMC_METRICS_PORT_KEY, C.DEFAULT_MMC_METRICS_PORT)
                ports[2][C.PORT] = metrics_port
                ports[2][C.TARGET_PORT] = metrics_port
    if not containers:
        return
    container = containers[0]
    set_container_env(container, gen_kv_store_env(kv_store_config))


def _configure_kv_conductor_role(infer_doc, user_config):
    role = get_infer_role(infer_doc, C.ROLE_KV_CONDUCTOR)
    if not role:
        return
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    workload_spec = role.setdefault(C.SPEC, {})
    template = workload_spec.setdefault(C.TEMPLATE, {})
    pod_spec = template.setdefault(C.SPEC, {})
    containers = pod_spec.get(C.CONTAINERS, [])
    if containers:
        containers[0][C.IMAGE] = deploy_config[C.IMAGE_NAME]
    if not k8s_utils.g_kv_conductor_enabled:
        role[C.REPLICAS] = 0
        workload_spec[C.REPLICAS] = 1
        return

    kv_conductor_config = normalize_kv_conductor_config(user_config)
    role[C.REPLICAS] = 1
    workload_spec[C.REPLICAS] = 1
    _set_role_primary_service_port(role, kv_conductor_config[C.KV_CONDUCTOR_PORT])
    if not containers:
        return
    container = containers[0]
    set_container_env(container, [{C.NAME: C.ENV_KVS_MASTER_SERVICE, C.VALUE: k8s_utils.g_kv_store_service}])


def generate_yaml_infer_service_set(input_yaml, output_file, user_config):
    """Generate InferServiceSet yaml from template and user_config."""
    logger.info("Generating InferServiceSet YAML from %s to %s", input_yaml, output_file)
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    all_docs = load_yaml(input_yaml, False)
    if not isinstance(all_docs, list):
        all_docs = [all_docs]
    namespace = deploy_config[C.CONFIG_JOB_ID]
    infer_doc = _find_infer_service_set_doc(all_docs)
    infer_name = infer_doc.get(C.METADATA, {}).get(C.NAME, "mindie-server")
    set_rbac_namespace(extract_rbac_resources(all_docs), namespace)
    infer_doc[C.METADATA][C.NAMESPACE] = namespace
    # Must call before engine config so g_mmc_local_service_mode is set
    # when build_engine_env_items() reads it. Second call in _configure_kv_store_role is idempotent.
    if k8s_utils.g_kv_store_enabled:
        normalize_kv_cache_store_config(user_config)
    _configure_controller_role(infer_doc, user_config)
    _configure_coordinator_role(infer_doc, user_config)
    if C.E_INSTANCES_NUM in deploy_config:
        _configure_engine_role(infer_doc, user_config, infer_name, C.ROLE_ENCODE)
    else:
        _zero_engine_role_replicas(infer_doc, C.ROLE_ENCODE)
    if is_hybrid_deploy(deploy_config):
        _configure_engine_role(infer_doc, user_config, infer_name, C.ROLE_UNION)
        _zero_engine_role_replicas(infer_doc, C.ROLE_PREFILL)
        _zero_engine_role_replicas(infer_doc, C.ROLE_DECODE)
    else:
        _configure_engine_role(infer_doc, user_config, infer_name, C.ROLE_PREFILL)
        _configure_engine_role(infer_doc, user_config, infer_name, C.ROLE_DECODE)
        _zero_engine_role_replicas(infer_doc, C.ROLE_UNION)
    _configure_kv_store_role(infer_doc, user_config)
    _configure_kv_conductor_role(infer_doc, user_config)
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    write_yaml(all_docs, output_file, False)
    k8s_utils.g_generate_yaml_list.append(output_file)


def init_infer_service_domain_name(infer_service_template_yaml, deploy_config):
    """
    Set g_controller_service and g_coordinator_*_service for CRD InferServiceSet mode.
    CRD creates services with naming: {service_name}-{infer_service_set_name}-0-{role_name}
    """
    all_docs = load_yaml(infer_service_template_yaml, False)
    if not isinstance(all_docs, list):
        all_docs = [all_docs]
    infer_doc = _find_infer_service_set_doc(all_docs)
    infer_name = infer_doc.get(C.METADATA, {}).get(C.NAME, "mindie-server")
    namespace = deploy_config[C.CONFIG_JOB_ID]

    def _build_fqdn(service, role_name_val):
        service_name = service.get(C.NAME, "")
        full_service_name = f"{service_name}-{infer_name}-0-{role_name_val}"
        return f"{full_service_name}.{namespace}.svc.cluster.local"

    def get_service_fqdn_for_role(role_name):
        """Return the first service's FQDN for non-coordinator roles."""
        role = get_infer_role(infer_doc, role_name)
        if not role:
            return None
        services = role.get(C.SERVICES, [])
        if not services:
            return None
        service = services[0]
        role_name_val = role.get(C.NAME, role_name)
        return _build_fqdn(service, role_name_val)

    def get_coordinator_fqdns():
        """Return a dict of port->FQDN for the coordinator role's three services."""
        role = get_infer_role(infer_doc, C.COORDINATOR)
        if not role:
            return {}
        services = role.get(C.SERVICES, [])
        role_name_val = role.get(C.NAME, C.COORDINATOR)
        result = {}
        for svc in services:
            for port_entry in svc.get("spec", {}).get("ports", []):
                port = port_entry.get("port")
                if port in (1025, 1026, 1027):
                    result[port] = _build_fqdn(svc, role_name_val)
                    break
        return result

    controller_service = get_service_fqdn_for_role(C.CONTROLLER)
    coord_fqdns = get_coordinator_fqdns()
    if not controller_service or not coord_fqdns:
        raise ValueError("Controller or coordinator role not found in infer_service_template.yaml")
    set_controller_service(controller_service)
    set_coordinator_service(coord_fqdns.get(1026, ""))
    set_coordinator_infer_service(coord_fqdns.get(1025, ""))
    set_coordinator_obs_service(coord_fqdns.get(1027, ""))

    kv_store_service = get_service_fqdn_for_role(C.ROLE_KV_STORE)
    if kv_store_service:
        set_kv_store_service(kv_store_service)

    kv_conductor_service = get_service_fqdn_for_role(C.ROLE_KV_CONDUCTOR)
    if kv_conductor_service:
        set_kv_conductor_service(kv_conductor_service)


def update_infer_service_replicas_only(infer_service_yaml_path, deploy_config):
    """Update engine role.replicas in infer_service.yaml for scaling (union or prefill/decode)."""
    logger.info("Updating InferServiceSet instance replicas in %s", infer_service_yaml_path)
    all_docs = load_yaml(infer_service_yaml_path, False)
    if not isinstance(all_docs, list):
        all_docs = [all_docs]
    infer_doc = _find_infer_service_set_doc(all_docs)

    e_total = obtain_engine_e_instance_total(deploy_config)
    encode_role = get_infer_role(infer_doc, C.ROLE_ENCODE)
    if encode_role:
        encode_role[C.REPLICAS] = e_total

    if is_hybrid_deploy(deploy_config):
        union_role = get_infer_role(infer_doc, C.ROLE_UNION)
        if not union_role:
            raise ValueError(
                "union role not found in infer_service.yaml. "
                "Regenerate infer_service.yaml with PD hybrid CRD deploy first."
            )
        union_role[C.REPLICAS] = int(deploy_config[C.HYBRID_INSTANCES_NUM])
    else:
        p_total, d_total = obtain_engine_instance_total(deploy_config)
        prefill_role = get_infer_role(infer_doc, C.ROLE_PREFILL)
        if prefill_role:
            prefill_role[C.REPLICAS] = p_total
        decode_role = get_infer_role(infer_doc, C.ROLE_DECODE)
        if decode_role:
            decode_role[C.REPLICAS] = d_total
        _zero_engine_role_replicas(infer_doc, C.ROLE_UNION)

    os.makedirs(os.path.dirname(infer_service_yaml_path), exist_ok=True)
    write_yaml(all_docs, infer_service_yaml_path, False)
    k8s_utils.g_generate_yaml_list.append(infer_service_yaml_path)
