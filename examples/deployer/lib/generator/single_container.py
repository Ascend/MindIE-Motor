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
    generate_unique_id, load_yaml, write_yaml, logger
)
from lib.generator import k8s_utils
from lib.generator.engine import (
    set_engine_weight_mount, apply_node_selector_by_hardware, set_container_npu,
    apply_a5_workload,
)
from lib.generator.kv_pool import normalize_kv_cache_pool_config, gen_kv_pool_env


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
    container[C.ENV].extend([
        {C.NAME: C.ENV_ROLE, C.VALUE: role},
        {C.NAME: C.ENV_JOB_NAME, C.VALUE: job_name},
    ])
    if k8s_utils.g_kv_pool_enabled:
        kv_pool_config = normalize_kv_cache_pool_config(user_config)
        kv_pool_env = gen_kv_pool_env(kv_pool_config)
        container[C.ENV].extend(kv_pool_env)

    npu_num = max(int(deploy_config[C.P_POD_NPU_NUM]), int(deploy_config[C.D_POD_NPU_NUM]))
    set_container_npu(container, npu_num, deploy_config)

    hardware_type = deploy_config[C.HARDWARE_TYPE]
    if hardware_type == C.HARDWARE_TYPE_800I_A2:
        deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.NODE_SELECTOR][C.ACCELERATOR_TYPE] = C.ACCELERATOR_TYPE_910B
        del deployment_data[C.METADATA][C.ANNOTATIONS]
    elif hardware_type == C.HARDWARE_TYPE_800I_A3:
        deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.NODE_SELECTOR][C.ACCELERATOR_TYPE] = C.ACCELERATOR_TYPE_A3
        deployment_data[C.METADATA][C.ANNOTATIONS][C.SP_BLOCK] = f"{npu_num}"
    elif hardware_type in C.HARDWARE_TYPE_950I_A5:
        apply_node_selector_by_hardware(deployment_data[C.SPEC][C.TEMPLATE][C.SPEC], hardware_type)
        apply_a5_workload(deployment_data, deploy_config)
        deployment_data[C.METADATA][C.ANNOTATIONS][C.SP_BLOCK] = f"{npu_num}"

    set_engine_weight_mount(deployment_data, container, deploy_config)

    write_yaml(data, output_file, False)
