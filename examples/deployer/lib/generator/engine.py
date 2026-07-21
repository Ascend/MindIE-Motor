# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import lib.constant as C
from lib.utils import (
    generate_unique_id,
    load_yaml,
    write_yaml,
    logger,
    modify_log_mount,
    obtain_engine_instance_total,
    obtain_engine_e_instance_total,
)
from lib.generator import k8s_utils
from lib.generator.k8s_utils import set_engine_base_name, modify_sp_block_num
from lib.generator.storage import apply_storage_volumes, apply_dshm_size


def _pop_ring_controller_atlas_from_labels(labels):
    if isinstance(labels, dict):
        labels.pop(C.RING_CONTROLLER_ATLAS_LABEL, None)


def _apply_a5_schedule_policy_annotation(template_metadata, hardware_type):
    policy = C.A5_SCHEDULE_POLICY_BY_ACCELERATOR_TYPE.get(hardware_type)
    if policy is None:
        raise ValueError(
            f"No huawei.com/schedule_policy mapping for A5 hardware_type '{hardware_type}'. "
            f"Supported accelerator-type values: {list(C.A5_SCHEDULE_POLICY_BY_ACCELERATOR_TYPE.keys())}"
        )
    template_metadata.setdefault(C.ANNOTATIONS, {})[C.HUAWEI_SCHEDULE_POLICY_ANNOTATION] = policy
    logger.info(
        "Applied A5 annotation %s=%s for hardware_type=%s",
        C.HUAWEI_SCHEDULE_POLICY_ANNOTATION,
        policy,
        hardware_type,
    )


def _append_a5_host_path_volumes(pod_spec, container):
    existing_volume_names = {volume[C.NAME] for volume in pod_spec.get(C.VOLUMES, []) if C.NAME in volume}
    existing_mount_names = {mount[C.NAME] for mount in container.get(C.VOLUME_MOUNTS, []) if C.NAME in mount}
    for volume_def in C.A5_HOST_PATH_VOLUMES:
        volume_name = volume_def[C.NAME]
        if volume_name not in existing_volume_names:
            pod_spec[C.VOLUMES].append({C.NAME: volume_name, C.HOST_PATH: {C.PATH: volume_def[C.PATH]}})
            existing_volume_names.add(volume_name)
        if volume_name not in existing_mount_names:
            container[C.VOLUME_MOUNTS].append(
                {C.NAME: volume_name, C.MOUNT_PATH: volume_def.get("mountPath", volume_def[C.PATH])}
            )
            existing_mount_names.add(volume_name)


def apply_a5_dns_config(pod_spec, deploy_config):
    """Lower ndots so cluster FQDNs resolve directly without corporate search suffixes."""
    hardware_type = deploy_config.get(C.HARDWARE_TYPE) if deploy_config else None
    if hardware_type not in C.HARDWARE_TYPE_950I_A5:
        return
    pod_spec[C.DNS_CONFIG] = {C.DNS_OPTIONS: [{C.NAME: C.A5_DNS_NDOTS_OPTION, C.VALUE: C.A5_DNS_NDOTS_VALUE}]}
    logger.info(
        "Applied A5 dnsConfig %s=%s for hardware_type=%s",
        C.A5_DNS_NDOTS_OPTION,
        C.A5_DNS_NDOTS_VALUE,
        hardware_type,
    )


def apply_a5_engine_pod_config(pod_spec, container, deploy_config):
    """Apply A5-specific pod network and hostPath settings to engine pods."""
    hardware_type = deploy_config.get(C.HARDWARE_TYPE) if deploy_config else None
    if hardware_type not in C.HARDWARE_TYPE_950I_A5:
        return
    _append_a5_host_path_volumes(pod_spec, container)
    apply_a5_dns_config(pod_spec, deploy_config)
    logger.info("Applied A5 engine pod config for hardware_type=%s", hardware_type)


def _apply_a5_inferservice_id_label(template_metadata, deploy_config, hardware_type):
    if hardware_type not in ("850-SuperPod-Atlas-8", "950-SuperPod-Atlas-8"):
        return
    job_id = deploy_config.get(C.CONFIG_JOB_ID) if deploy_config else None
    if not job_id:
        logger.warning("job_id is missing in deploy config, skip applying A5 label %s", C.INFERSERVICE_ID_LABEL)
        return
    template_metadata.setdefault(C.LABELS, {})[C.INFERSERVICE_ID_LABEL] = job_id


def apply_a5_workload(workload, deploy_config):
    hardware_type = deploy_config.get(C.HARDWARE_TYPE) if deploy_config else None
    if hardware_type not in C.HARDWARE_TYPE_950I_A5:
        return
    template_section = workload.get(C.SPEC, {}).get(C.TEMPLATE)
    if template_section is not None:
        _pop_ring_controller_atlas_from_labels(workload.get(C.METADATA, {}).get(C.LABELS))
        template_meta = template_section[C.METADATA]
    else:
        template_meta = workload.setdefault(C.METADATA, {})
    _pop_ring_controller_atlas_from_labels(template_meta.get(C.LABELS))
    _apply_a5_schedule_policy_annotation(template_meta, hardware_type)
    _apply_a5_inferservice_id_label(template_meta, deploy_config, hardware_type)


def update_engine_base_name(user_config):
    engine_section = user_config.get(C.MOTOR_ENGINE_PREFILL_CONFIG) or user_config.get(C.MOTOR_ENGINE_UNION_CONFIG, {})
    engine_type = engine_section.get(C.ENGINE_TYPE, C.ENGINE_TYPE_MINDIE_LLM)
    if engine_type in C.SERVER_BASE_NAME_MAP:
        set_engine_base_name(C.SERVER_BASE_NAME_MAP[engine_type])
    else:
        set_engine_base_name(C.ENGINE_TYPE_MINDIE_SERVER)


def is_hybrid_deploy(deploy_config):
    return C.HYBRID_INSTANCES_NUM in deploy_config


def build_engine_env_items(role, deploy_config, job_name, include_kv_store=False):
    env_items = [
        {C.NAME: C.ENV_ROLE, C.VALUE: role},
        {C.NAME: C.ENV_JOB_NAME, C.VALUE: job_name},
        {C.NAME: C.ENV_CONTROLLER_SERVICE, C.VALUE: k8s_utils.g_controller_service},
        {C.NAME: C.ENV_COORDINATOR_SERVICE, C.VALUE: k8s_utils.g_coordinator_service},
        {C.NAME: C.ENV_COORDINATOR_INFER_SERVICE, C.VALUE: k8s_utils.g_coordinator_infer_service},
        {C.NAME: C.ENV_COORDINATOR_OBS_SERVICE, C.VALUE: k8s_utils.g_coordinator_obs_service},
    ]
    if include_kv_store and k8s_utils.g_kv_store_enabled:
        env_items.extend(k8s_utils.build_kv_store_env_items())
    if k8s_utils.g_mf_store_enabled:
        ascend_mf_store_url = f"tcp://{k8s_utils.g_mf_store_service}:{C.DEFAULT_MF_STORE_PORT}"
        hardware_type = deploy_config.get(C.HARDWARE_TYPE, C.HARDWARE_TYPE_800I_A2)
        ascend_mf_transfer_protocol = "device_rdma" if hardware_type in C.HARDWARE_TYPE_A2 else "sdma"
        env_items.extend(
            [
                {C.NAME: C.ENV_ASCEND_MF_STORE_URL, C.VALUE: ascend_mf_store_url},
                {C.NAME: C.ENV_ASCEND_MF_TRANSFER_PROTOCOL, C.VALUE: ascend_mf_transfer_protocol},
            ]
        )
    if k8s_utils.g_engine_type == C.ENGINE_TYPE_SGLANG:
        env_items.append({C.NAME: C.ENV_SGLANG_HOST_IP, "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}})
    return env_items


def set_engine_metadata(deployment_data, deploy_config, index, node_type, job_name):
    deployment_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]
    unique_name = f"{k8s_utils.g_engine_base_name}-{node_type}{index}"
    deployment_data[C.METADATA][C.NAME] = unique_name
    deployment_data[C.METADATA][C.LABELS][C.APP] = unique_name
    deployment_data[C.SPEC][C.SELECTOR][C.MATCHLABELS][C.APP] = unique_name
    deployment_data[C.SPEC][C.TEMPLATE][C.METADATA][C.LABELS][C.APP] = unique_name
    deployment_data[C.METADATA][C.LABELS][C.JOB_NAME] = job_name


def set_engine_env(container, deploy_config, node_type, job_name):
    if node_type == C.NODE_TYPE_U:
        role = C.ROLE_UNION
    else:
        role_map = {C.NODE_TYPE_E: C.ROLE_ENCODE, C.NODE_TYPE_P: C.ROLE_PREFILL, C.NODE_TYPE_D: C.ROLE_DECODE}
        role = role_map.get(node_type)
    if C.ENV not in container:
        container[C.ENV] = []
    container[C.ENV].extend(build_engine_env_items(role, deploy_config, job_name, include_kv_store=True))


def set_engine_replicas(deployment_data, deploy_config, node_type):
    if node_type == C.NODE_TYPE_U:
        instance_pod_num_key = C.SINGLE_HYBRID_INSTANCE_POD_NUM
    elif node_type == C.NODE_TYPE_E:
        instance_pod_num_key = C.SINGER_E_INSTANCES_NUM
    else:
        instance_pod_num_key = C.SINGER_P_INSTANCES_NUM if node_type == C.NODE_TYPE_P else C.SINGER_D_INSTANCES_NUM
    if instance_pod_num_key in deploy_config:
        deployment_data[C.SPEC][C.REPLICAS] = int(deploy_config[instance_pod_num_key])


def set_container_npu(container, npu_num, deploy_config=None):
    if C.RESOURCES not in container:
        return
    requests = container[C.RESOURCES].setdefault(C.REQUESTS, {})
    limits = container[C.RESOURCES].setdefault(C.LIMITS, {})
    hardware_type = deploy_config.get(C.HARDWARE_TYPE) if deploy_config else None
    if hardware_type in C.HARDWARE_TYPE_950I_A5:
        requests.pop(C.ASCEND_910_NPU_NUM, None)
        limits.pop(C.ASCEND_910_NPU_NUM, None)
        requests[C.ASCEND_950_NPU_NUM] = npu_num
        limits[C.ASCEND_950_NPU_NUM] = npu_num
    else:
        requests[C.ASCEND_910_NPU_NUM] = npu_num
        limits[C.ASCEND_910_NPU_NUM] = npu_num


def set_engine_npu(container, deploy_config, node_type):
    if node_type == C.NODE_TYPE_U and C.HYBRID_POD_NPU_NUM in deploy_config:
        npu_num = int(deploy_config[C.HYBRID_POD_NPU_NUM])
    elif node_type == C.NODE_TYPE_E and C.E_POD_NPU_NUM in deploy_config:
        npu_num = int(deploy_config[C.E_POD_NPU_NUM])
    elif node_type == C.NODE_TYPE_P and C.P_POD_NPU_NUM in deploy_config:
        npu_num = int(deploy_config[C.P_POD_NPU_NUM])
    elif node_type == C.NODE_TYPE_D and C.D_POD_NPU_NUM in deploy_config:
        npu_num = int(deploy_config[C.D_POD_NPU_NUM])
    else:
        return
    set_container_npu(container, npu_num, deploy_config)


def apply_node_selector_by_hardware(pod_spec, hardware_type):
    if hardware_type in C.HARDWARE_TYPE_A2 or hardware_type in C.HARDWARE_TYPE_A3:
        pod_spec[C.NODE_SELECTOR][C.ACCELERATOR] = C.ACCELERATOR_910
    if hardware_type in C.HARDWARE_TYPE_950I_A5:
        pod_spec[C.NODE_SELECTOR][C.ACCELERATOR] = C.ACCELERATOR_A5
    pod_spec[C.NODE_SELECTOR][C.ACCELERATOR_TYPE] = k8s_utils.get_accelerator_type_from_cluster(hardware_type)


def apply_pd_heterogeneous_node_selector(pod_spec, deploy_config, node_type):
    if deploy_config.get(C.ENABLE_PD_HETEROGENEOUS) is not True:
        return
    label_key = deploy_config.get(C.PD_HETEROGENEOUS_LABEL_KEY, C.DEFAULT_PD_HETEROGENEOUS_LABEL_KEY)
    label_value_map = {
        C.NODE_TYPE_P: deploy_config.get(
            C.PD_HETEROGENEOUS_PREFILL_LABEL_VALUE, C.DEFAULT_PD_HETEROGENEOUS_PREFILL_VALUE
        ),
        C.NODE_TYPE_D: deploy_config.get(
            C.PD_HETEROGENEOUS_DECODE_LABEL_VALUE, C.DEFAULT_PD_HETEROGENEOUS_DECODE_VALUE
        ),
    }
    if node_type in label_value_map:
        pod_spec[C.NODE_SELECTOR][label_key] = label_value_map[node_type]
        logger.info(
            "Applied PD heterogeneous node selector: node_type=%s, %s=%s",
            node_type,
            label_key,
            label_value_map[node_type],
        )
    else:
        logger.warning(
            "PD heterogeneous enabled but unexpected node_type=%s, expected one of %s, node selector not applied",
            node_type,
            list(label_value_map.keys()),
        )


def set_engine_node_selector(deployment_data, deploy_config, node_type):
    modify_sp_block_num(deployment_data, node_type, deploy_config)
    hardware_type = deploy_config[C.HARDWARE_TYPE]
    pod_spec = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC]
    pod_spec[C.NODE_SELECTOR] = pod_spec.get(C.NODE_SELECTOR, {})
    apply_node_selector_by_hardware(pod_spec, hardware_type)
    apply_pd_heterogeneous_node_selector(pod_spec, deploy_config, node_type)


def set_weight_mount(pod_spec, container, weight_mount_path):
    volume_found = False
    for volume in pod_spec.get(C.VOLUMES, []):
        if volume[C.NAME] == C.WEIGHT_MOUNT:
            volume[C.HOST_PATH][C.PATH] = weight_mount_path
            volume_found = True
            break
    if not volume_found:
        pod_spec.setdefault(C.VOLUMES, []).append({C.NAME: C.WEIGHT_MOUNT, C.HOST_PATH: {C.PATH: weight_mount_path}})
    volume_mount_found = False
    for volume_mount in container.get(C.VOLUME_MOUNTS, []):
        if volume_mount[C.NAME] == C.WEIGHT_MOUNT:
            volume_mount[C.MOUNT_PATH] = weight_mount_path
            volume_mount_found = True
            break
    if not volume_mount_found:
        container.setdefault(C.VOLUME_MOUNTS, []).append({C.NAME: C.WEIGHT_MOUNT, C.MOUNT_PATH: weight_mount_path})


def set_engine_weight_mount(deployment_data, container, deploy_config):
    weight_mount_path = deploy_config.get(C.WEIGHT_MOUNT_PATH, C.DEFAULT_WEIGHT_MOUNT_PATH)
    pod_spec = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC]
    set_weight_mount(pod_spec, container, weight_mount_path)


def modify_engine_yaml(deployment_data, user_config, index, node_type):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    container = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]

    if k8s_utils.g_engine_type == C.ENGINE_TYPE_SGLANG:
        container[C.SECURITY_CONTEXT] = {}
        container[C.SECURITY_CONTEXT][C.PRIVILEGED] = True

    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]
    job_name = f"{deploy_config[C.CONFIG_JOB_ID]}-{node_type}{index}-{generate_unique_id()}"
    set_engine_metadata(deployment_data, deploy_config, index, node_type, job_name)
    container[C.NAME] = k8s_utils.g_engine_base_name
    if C.ENV not in container:
        container[C.ENV] = []
    set_engine_env(container, deploy_config, node_type, job_name)
    set_engine_replicas(deployment_data, deploy_config, node_type)
    set_engine_npu(container, deploy_config, node_type)
    set_engine_node_selector(deployment_data, deploy_config, node_type)
    set_engine_weight_mount(deployment_data, container, deploy_config)
    engine_pod_spec = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC]
    apply_storage_volumes(engine_pod_spec, container, user_config)
    apply_dshm_size(engine_pod_spec, user_config)
    apply_a5_engine_pod_config(engine_pod_spec, container, deploy_config)
    apply_a5_workload(deployment_data, deploy_config)
    modify_log_mount(deployment_data, user_config, deployment_data[C.METADATA][C.NAME])


def validate_instance_nums(user_config):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    if is_hybrid_deploy(deploy_config):
        hybrid_total, _ = obtain_engine_instance_total(deploy_config)
        if hybrid_total <= C.INSTANCE_NUM_ZERO:
            raise ValueError(f"{C.HYBRID_INSTANCES_NUM} must be greater than {C.INSTANCE_NUM_ZERO}")
        if hybrid_total > C.INSTANCE_NUM_MAX:
            raise ValueError(f"{C.HYBRID_INSTANCES_NUM} must not exceed {C.INSTANCE_NUM_MAX}")
        return

    p_total, d_total = obtain_engine_instance_total(deploy_config)
    if p_total <= C.INSTANCE_NUM_ZERO:
        raise ValueError(f"{C.P_INSTANCES_NUM} must be greater than {C.INSTANCE_NUM_ZERO}")
    if p_total > C.INSTANCE_NUM_MAX:
        raise ValueError(f"{C.P_INSTANCES_NUM} must not exceed {C.INSTANCE_NUM_MAX}")
    if d_total <= C.INSTANCE_NUM_ZERO:
        raise ValueError(f"{C.D_INSTANCES_NUM} must be greater than {C.INSTANCE_NUM_ZERO}")
    if d_total > C.INSTANCE_NUM_MAX:
        raise ValueError(f"{C.D_INSTANCES_NUM} must not exceed {C.INSTANCE_NUM_MAX}")


def generate_yaml_engine(input_yaml, output_file, user_config):
    logger.info("Generating YAML from %s to %s", input_yaml, output_file)
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    # generate yaml engine E
    e_total = obtain_engine_e_instance_total(deploy_config)
    for e_index in range(e_total):
        data = load_yaml(input_yaml, True)
        modify_engine_yaml(data, user_config, e_index, C.NODE_TYPE_E)
        output_file_e = output_file + f"_{C.NODE_TYPE_E}{e_index}.yaml"
        write_yaml(data, output_file_e, True)
        k8s_utils.g_generate_yaml_list.append(output_file_e)

    # generate yaml engine P/D
    p_total, d_total = obtain_engine_instance_total(deploy_config)
    if is_hybrid_deploy(deploy_config):
        for u_index in range(p_total):
            data = load_yaml(input_yaml, True)
            modify_engine_yaml(data, user_config, u_index, C.NODE_TYPE_U)
            output_file_u = output_file + f"_{C.NODE_TYPE_U}{u_index}.yaml"
            write_yaml(data, output_file_u, True)
            k8s_utils.g_generate_yaml_list.append(output_file_u)
        return

    for p_index in range(p_total):
        data = load_yaml(input_yaml, True)
        modify_engine_yaml(data, user_config, p_index, C.NODE_TYPE_P)
        output_file_p = output_file + f"_{C.NODE_TYPE_P}{p_index}.yaml"
        write_yaml(data, output_file_p, True)
        k8s_utils.g_generate_yaml_list.append(output_file_p)
    for d_index in range(d_total):
        data = load_yaml(input_yaml, True)
        modify_engine_yaml(data, user_config, d_index, C.NODE_TYPE_D)
        output_file_d = output_file + f"_{C.NODE_TYPE_D}{d_index}.yaml"
        write_yaml(data, output_file_d, True)
        k8s_utils.g_generate_yaml_list.append(output_file_d)
