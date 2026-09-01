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
from lib.utils import apply_node_selector_override, load_yaml, write_yaml, logger
from lib.generator import k8s_utils


def generate_yaml_mf_store(input_yaml, output_file, user_config):
    logger.info(f"Generating YAML from {input_yaml} to {output_file}")
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    data = load_yaml(input_yaml, False)
    deployment_data = data[0]
    deployment_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]

    pod_spec = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC]
    apply_node_selector_override(pod_spec, deploy_config, C.MF_STORE_NODE_SELECTOR)

    container = pod_spec[C.CONTAINERS][0]
    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]

    if C.ENV not in container:
        container[C.ENV] = []
    container[C.ENV].append({C.NAME: C.ENV_ASCEND_MF_STORE_PORT, C.VALUE: str(C.DEFAULT_MF_STORE_PORT)})

    service_data = data[1]
    service_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]
    ports = service_data.get(C.SPEC, {}).get(C.PORTS, [])
    if not ports:
        raise ValueError(f"Missing required service ports in {input_yaml}.")
    ports[0][C.PORT] = C.DEFAULT_MF_STORE_PORT
    ports[0][C.TARGET_PORT] = C.DEFAULT_MF_STORE_PORT

    write_yaml(data, output_file, False)
    k8s_utils.g_generate_yaml_list.append(output_file)
