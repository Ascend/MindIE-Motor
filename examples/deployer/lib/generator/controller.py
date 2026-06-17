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
    generate_unique_id, load_yaml, write_yaml, logger, modify_log_mount
)
from lib.generator import k8s_utils
from lib.generator.k8s_utils import extract_resources, set_rbac_namespace, set_services_namespace


def modify_controller_replicas(data, user_config):
    if (
        C.MOTOR_CONTROLLER_CONFIG in user_config
        and C.STANDBY_CONFIG in user_config[C.MOTOR_CONTROLLER_CONFIG]
        and user_config[C.MOTOR_CONTROLLER_CONFIG][C.STANDBY_CONFIG][C.ENABLE_MASTER_STANDBY]
    ):
        data[C.SPEC][C.REPLICAS] = 2


def modify_controller_deployment(deployment_data, user_config):
    if not deployment_data:
        return
    
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    namespace = deploy_config[C.CONFIG_JOB_ID]
    deployment_data[C.METADATA][C.NAMESPACE] = namespace

    container = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]

    if C.ENV not in container:
        container[C.ENV] = []

    container[C.ENV].append({
        C.NAME: C.ENV_ROLE,
        C.VALUE: C.CONTROLLER
    })

    uuid_spec = generate_unique_id()
    job_name = f"{deploy_config[C.CONFIG_JOB_ID]}-{C.CONTROLLER}-{uuid_spec}"
    deployment_data[C.METADATA][C.LABELS]["job-name"] = job_name
    container[C.ENV].append({
        C.NAME: C.ENV_JOB_NAME,
        C.VALUE: job_name
    })

    container[C.ENV].extend([
        {C.NAME: C.ENV_CONTROLLER_SERVICE, C.VALUE: k8s_utils.g_controller_service},
        {C.NAME: C.ENV_COORDINATOR_SERVICE, C.VALUE: k8s_utils.g_coordinator_service},
        {C.NAME: C.ENV_COORDINATOR_INFER_SERVICE, C.VALUE: k8s_utils.g_coordinator_infer_service},
        {C.NAME: C.ENV_COORDINATOR_OBS_SERVICE, C.VALUE: k8s_utils.g_coordinator_obs_service}
    ])

    modify_controller_replicas(deployment_data, user_config)
    modify_log_mount(deployment_data, user_config, "mindie-motor-controller")


def modify_controller_yaml(data, user_config):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    namespace = deploy_config[C.CONFIG_JOB_ID]
    deployment_data, service_list, rbac_resources = extract_resources(data)
    set_rbac_namespace(rbac_resources, namespace)
    modify_controller_deployment(deployment_data, user_config)
    set_services_namespace(service_list, namespace)


def generate_yaml_controller(input_yaml, output_file, user_config):
    logger.info(f"Generating YAML from {input_yaml} to {output_file}")
    data = load_yaml(input_yaml, False)
    modify_controller_yaml(data, user_config)
    write_yaml(data, output_file, False)
    k8s_utils.g_generate_yaml_list.append(output_file)
