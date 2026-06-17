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
    load_yaml, write_yaml, logger
)
from lib.generator import k8s_utils


def normalize_kv_cache_pool_config(user_config):
    kv_config = user_config.get(C.KV_CACHE_POOL_CONFIG)
    if not isinstance(kv_config, dict):
        raise ValueError(f"Missing or invalid '{C.KV_CACHE_POOL_CONFIG}' in user config")

    if C.KV_POOL_PORT not in kv_config:
        kv_config[C.KV_POOL_PORT] = C.DEFAULT_KV_POOL_PORT

    return kv_config


def gen_kv_pool_env(kv_pool_config):
    service_port = kv_pool_config.get(C.KV_POOL_PORT)
    missing_keys = []
    if C.KV_POOL_EVICTION_HIGH_WATERMARK_RATIO not in kv_pool_config:
        missing_keys.append(C.KV_POOL_EVICTION_HIGH_WATERMARK_RATIO)
    if C.KV_POOL_EVICTION_RATIO not in kv_pool_config:
        missing_keys.append(C.KV_POOL_EVICTION_RATIO)
    if missing_keys:
        raise ValueError(
            f"Missing required kv cache pool config: {missing_keys}. "
            f"Please configure them in '{C.KV_CACHE_POOL_CONFIG}'."
        )
    if C.DEFAULT_KV_LEASE_TTL not in kv_pool_config:
        # Set default value for DEFAULT_KV_LEASE_TTL if not provided
        # Default lease TTL in milliseconds
        kv_pool_config[C.DEFAULT_KV_LEASE_TTL] = 11000 

    kv_pool_env = [
        {C.NAME: C.ENV_KVP_MASTER_SERVICE, C.VALUE: k8s_utils.g_kv_pool_service},
        {C.NAME: C.ENV_KV_POOL_PORT, C.VALUE: str(service_port)},
        {C.NAME: C.ENV_KV_POOL_EVICTION_HIGH_WATERMARK_RATIO,
            C.VALUE: str(kv_pool_config[C.KV_POOL_EVICTION_HIGH_WATERMARK_RATIO])},
        {C.NAME: C.ENV_KV_POOL_EVICTION_RATIO, C.VALUE: str(kv_pool_config[C.KV_POOL_EVICTION_RATIO])},
        {C.NAME: C.ENV_DEFAULT_KV_LEASE_TTL, C.VALUE: str(kv_pool_config[C.DEFAULT_KV_LEASE_TTL])}
    ]

    return kv_pool_env


def generate_yaml_kv_pool(input_yaml, output_file, user_config, kv_pool_config):
    logger.info(f"Generating YAML from {input_yaml} to {output_file}")
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    data = load_yaml(input_yaml, False)
    deployment_data = data[0]
    deployment_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]

    container = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]

    if C.ENV not in container:
        container[C.ENV] = []

    service_port = kv_pool_config.get(C.KV_POOL_PORT)
    kv_pool_env = gen_kv_pool_env(kv_pool_config)
    container[C.ENV].extend(kv_pool_env)
    
    service_data = data[1]
    service_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]
    ports = service_data.get(C.SPEC, {}).get(C.PORTS, [])
    if not ports:
        raise ValueError(
            "Missing required service ports in 'kv_pool_template.yaml'. "
            "Please configure spec.ports for KV pool service."
        )
    ports[0][C.PORT] = service_port
    ports[0][C.TARGET_PORT] = service_port

    write_yaml(data, output_file, False)
    k8s_utils.g_generate_yaml_list.append(output_file)
