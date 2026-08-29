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
    apply_coordinator_infer_node_port,
    apply_coordinator_obs_node_port,
    apply_node_selector_override,
    generate_unique_id,
    get_coordinator_service_name,
    is_observability_service_name,
    load_yaml,
    logger,
    modify_log_mount,
    write_yaml,
)
from lib.generator import k8s_utils
from lib.generator.k8s_utils import extract_resources, set_rbac_namespace, set_services_namespace
from lib.generator.engine import apply_a5_dns_config, set_engine_weight_mount
from lib.generator.render import configure_render_sidecar


def modify_coordinator_replicas(data, user_config):
    if (
        C.MOTOR_COORDINATOR_CONFIG in user_config
        and C.STANDBY_CONFIG in user_config[C.MOTOR_COORDINATOR_CONFIG]
        and user_config[C.MOTOR_COORDINATOR_CONFIG][C.STANDBY_CONFIG][C.ENABLE_MASTER_STANDBY]
    ):
        data[C.SPEC][C.REPLICAS] = 2


def modify_coordinator_deployment(deployment_data, user_config):
    if not deployment_data:
        return
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    namespace = deploy_config[C.CONFIG_JOB_ID]
    deployment_data[C.METADATA][C.NAMESPACE] = namespace

    container = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]

    if C.ENV not in container:
        container[C.ENV] = []

    container[C.ENV].append({C.NAME: C.ENV_ROLE, C.VALUE: C.COORDINATOR})

    uuid_spec = generate_unique_id()
    job_name = f"{deploy_config[C.CONFIG_JOB_ID]}-{C.COORDINATOR}-{uuid_spec}"
    deployment_data[C.METADATA][C.LABELS]["job-name"] = job_name
    container[C.ENV].append({C.NAME: C.ENV_JOB_NAME, C.VALUE: job_name})

    container[C.ENV].extend(
        [
            {C.NAME: C.ENV_CONTROLLER_SERVICE, C.VALUE: k8s_utils.g_controller_service},
            {C.NAME: C.ENV_COORDINATOR_SERVICE, C.VALUE: k8s_utils.g_coordinator_service},
            {C.NAME: C.ENV_COORDINATOR_INFER_SERVICE, C.VALUE: k8s_utils.g_coordinator_infer_service},
            {C.NAME: C.ENV_COORDINATOR_OBS_SERVICE, C.VALUE: k8s_utils.g_coordinator_obs_service},
        ]
    )

    if k8s_utils.g_kv_conductor_enabled:
        container[C.ENV].append({C.NAME: C.ENV_KV_CONDUCTOR_SERVICE, C.VALUE: k8s_utils.g_kv_conductor_service})

    container[C.ENV].extend(k8s_utils.build_kv_store_env_items())

    modify_coordinator_replicas(deployment_data, user_config)
    pod_spec = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC]
    apply_node_selector_override(pod_spec, deploy_config, C.COORDINATOR_NODE_SELECTOR)
    apply_a5_dns_config(deployment_data[C.SPEC][C.TEMPLATE][C.SPEC], deploy_config)
    modify_log_mount(deployment_data, user_config, "mindie-motor-coordinator")


def modify_coordinator_yaml(data, user_config):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    namespace = deploy_config[C.CONFIG_JOB_ID]
    deployment_data, service_list, rbac_resources = extract_resources(data)
    set_rbac_namespace(rbac_resources, namespace)
    modify_coordinator_deployment(deployment_data, user_config)
    # In some cloud-based multi-tenant environments, inference services from different
    # tenants are deployed within the same cluster.
    # Consequently, the externally facing service must differentiate between tenants
    # and support customized naming.
    # For example, the inference service entrypoint for Tenant A might be
    # mindie-ms-coordinator-infer-tenant-A, while Tenant B uses
    # mindie-ms-coordinator-infer-tenant-B.
    # An upper-layer gateway then routes requests based on these tenant-specific
    # service endpoints.
    coordinator_service_name = get_coordinator_service_name(deploy_config)
    for service_data in service_list:
        svc_name = service_data[C.METADATA][C.NAME]
        if svc_name == "mindie-motor-coordinator-infer":
            service_data[C.METADATA][C.NAME] = coordinator_service_name
            apply_coordinator_infer_node_port(service_data, deploy_config)
        elif is_observability_service_name(svc_name):
            apply_coordinator_obs_node_port(service_data, deploy_config)
    set_services_namespace(service_list, namespace)
    container = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    set_engine_weight_mount(deployment_data, container, deploy_config)
    configure_render_sidecar(deployment_data[C.SPEC][C.TEMPLATE][C.SPEC], user_config)


def generate_yaml_coordinator(input_yaml, output_file, user_config):
    logger.info(f"Generating YAML from {input_yaml} to {output_file}")
    data = load_yaml(input_yaml, False)
    modify_coordinator_yaml(data, user_config)
    write_yaml(data, output_file, False)
    k8s_utils.g_generate_yaml_list.append(output_file)
