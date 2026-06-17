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
from lib.utils import load_yaml, write_yaml, logger
from lib.generator import k8s_utils


def normalize_kv_conductor_config(user_config):
    kv_config = user_config.get(C.KV_CONDUCTOR_CONFIG)
    if not isinstance(kv_config, dict):
        raise ValueError(f"Missing or invalid '{C.KV_CONDUCTOR_CONFIG}' in user config")
    return kv_config


def generate_yaml_kv_conductor(input_yaml, output_file, user_config, kv_conductor_config):
    logger.info(f"Generating YAML from {input_yaml} to {output_file}")
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    data = load_yaml(input_yaml, False)

    deployment_data = None
    service_list = []
    for item in data:
        if item.get(C.KIND) == C.DEPLOYMENT_KIND:
            deployment_data = item
        elif item.get(C.KIND) == C.SERVICE:
            service_list.append(item)

    deployment_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]

    container = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]

    if C.ENV not in container:
        container[C.ENV] = []

    for svc in service_list:
        svc[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]

    service_port = kv_conductor_config.get(C.KV_CONDUCTOR_PORT)

    ports = service_list[0].get(C.SPEC, {}).get(C.PORTS, [])
    if not ports:
        raise ValueError(
            "Missing required service ports in 'kv_conductor_template.yaml'. "
            "Please configure spec.ports for KV conductor service."
        )
    ports[0][C.PORT] = service_port
    ports[0][C.TARGET_PORT] = service_port

    # Inject KV_CONDUCTOR_SERVICE so that coordinator/engine pods can discover
    # the kv-conductor endpoint for /register and /query calls.
    kv_conductor_service_name = kv_conductor_config.get("conductor_service", "kv-conductor")
    kv_conductor_env = [{C.NAME: C.ENV_KV_CONDUCTOR_SERVICE, C.VALUE: kv_conductor_service_name}]
    container[C.ENV].extend(kv_conductor_env)

    write_yaml(data, output_file, False)
    k8s_utils.g_generate_yaml_list.append(output_file)
