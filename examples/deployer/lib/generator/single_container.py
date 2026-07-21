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
from lib.utils import generate_unique_id, load_yaml, write_yaml, logger
from lib.generator import k8s_utils
from lib.generator.engine import (
    set_engine_weight_mount,
    apply_node_selector_by_hardware,
    set_container_npu,
    apply_a5_workload,
    apply_a5_engine_pod_config,
)
from lib.generator.kv_cache_store import normalize_kv_cache_store_config, gen_kv_store_env
from lib.generator.storage import (
    apply_storage_volumes,
    apply_dshm_size,
    get_storage_entries,
    build_storage_pvc_docs,
)


def generate_yaml_single_container(input_yaml, output_file, user_config):
    logger.info(f"Generating YAML from {input_yaml} to {output_file}")
    data = load_yaml(input_yaml, False)

    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    job_id = deploy_config[C.CONFIG_JOB_ID]

    deployment_data = data[0] if isinstance(data, list) else data
    app_name = f"{job_id}-single-container"
    deployment_data[C.METADATA][C.NAME] = app_name
    deployment_data[C.METADATA][C.LABELS][C.APP] = app_name
    deployment_data[C.SPEC][C.SELECTOR][C.MATCHLABELS][C.APP] = app_name
    deployment_data[C.SPEC][C.TEMPLATE][C.METADATA][C.LABELS][C.APP] = app_name
    deployment_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]

    container = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]

    service_data = data[1]
    service_data[C.METADATA][C.NAME] = f"{job_id}-coordinator-service"
    service_data[C.METADATA][C.LABELS][C.APP] = app_name
    service_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]
    service_data[C.SPEC][C.SELECTOR][C.APP] = app_name

    if C.ENV not in container:
        container[C.ENV] = []
    role = C.ROLE_SINGLE_CONTAINER
    uuid_spec = generate_unique_id()
    job_name = f"{deploy_config[C.CONFIG_JOB_ID]}-{role}-{uuid_spec}"
    container[C.ENV].extend(
        [
            {C.NAME: C.ENV_ROLE, C.VALUE: role},
            {C.NAME: C.ENV_JOB_NAME, C.VALUE: job_name},
        ]
    )
    if k8s_utils.g_kv_store_enabled:
        kv_store_config = normalize_kv_cache_store_config(user_config)
        kv_store_env = gen_kv_store_env(kv_store_config)
        container[C.ENV].extend(kv_store_env)

    npu_num = max(int(deploy_config[C.P_POD_NPU_NUM]), int(deploy_config[C.D_POD_NPU_NUM]))
    set_container_npu(container, npu_num, deploy_config)

    hardware_type = deploy_config[C.HARDWARE_TYPE]
    pod_spec = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC]
    pod_spec[C.NODE_SELECTOR] = pod_spec.get(C.NODE_SELECTOR, {})
    if hardware_type in C.HARDWARE_TYPE_A2:
        apply_node_selector_by_hardware(pod_spec, hardware_type)
        del deployment_data[C.SPEC][C.TEMPLATE][C.METADATA][C.ANNOTATIONS]
    elif hardware_type in C.HARDWARE_TYPE_A3:
        apply_node_selector_by_hardware(pod_spec, hardware_type)
        k8s_utils.apply_sp_block_annotation(deployment_data[C.SPEC][C.TEMPLATE][C.METADATA], npu_num, hardware_type)
    elif hardware_type in C.HARDWARE_TYPE_950I_A5:
        apply_node_selector_by_hardware(pod_spec, hardware_type)
        apply_a5_engine_pod_config(pod_spec, container, deploy_config)
        apply_a5_workload(deployment_data, deploy_config)
        k8s_utils.apply_sp_block_annotation(deployment_data[C.SPEC][C.TEMPLATE][C.METADATA], npu_num, hardware_type)

    set_engine_weight_mount(deployment_data, container, deploy_config)
    sc_pod_spec = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC]
    storage_entries = get_storage_entries(user_config)
    apply_storage_volumes(sc_pod_spec, container, user_config, storage_entries)
    apply_dshm_size(sc_pod_spec, user_config)
    if storage_entries:
        # Embed the PVC(s) as extra documents so `kubectl apply -f` creates them with the pod.
        pvc_template = os.path.join(os.path.dirname(input_yaml), "storage_pvc_template.yaml")
        data.extend(build_storage_pvc_docs(pvc_template, user_config, storage_entries))

    write_yaml(data, output_file, False)
