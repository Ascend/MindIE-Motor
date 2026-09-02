# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import lib.constant as C
from lib.utils import apply_node_selector_override, load_yaml, write_yaml, logger
from lib.generator import k8s_utils


def resolve_kv_store_target_job_id(kv_config, current_job_id):
    """Return target job_id when reuse is requested; otherwise None."""
    target = kv_config.get(C.TARGET_JOB_ID)
    if not target or target == current_job_id:
        return None
    return target


def get_multi_deployment_kv_store_service_name(paths):
    kv_store_data = load_yaml(paths["kv_store_input_yaml"], False)
    for doc in kv_store_data:
        if doc.get(C.KIND) == C.SERVICE:
            return doc[C.METADATA][C.NAME]
    raise ValueError("KV store service not found in kv_cache_store_template.yaml")


def get_infer_service_kv_store_service_name(infer_service_template_yaml):
    all_docs = load_yaml(infer_service_template_yaml, False)
    if not isinstance(all_docs, list):
        all_docs = [all_docs]
    infer_doc = None
    for doc in all_docs:
        if doc.get(C.KIND) == "InferServiceSet":
            infer_doc = doc
            break
    if infer_doc is None:
        raise ValueError("InferServiceSet document not found in infer_service_template.yaml")
    roles = infer_doc.get(C.SPEC, {}).get(C.TEMPLATE, {}).get(C.ROLES, [])
    role = None
    for r in roles:
        if r.get(C.NAME) == C.ROLE_KV_STORE:
            role = r
            break
    if not role:
        raise ValueError(f"Role '{C.ROLE_KV_STORE}' not found in infer_service_template.yaml")
    services = role.get(C.SERVICES, [])
    if not services:
        raise ValueError(f"Missing services for role '{C.ROLE_KV_STORE}' in infer_service_template.yaml")
    service_name = services[0].get(C.NAME, "")
    infer_name = infer_doc.get(C.METADATA, {}).get(C.NAME, "mindie-server")
    role_name_val = role.get(C.NAME, C.ROLE_KV_STORE)
    return f"{service_name}-{infer_name}-0-{role_name_val}"


def build_kv_store_service_fqdn(service_name, job_id):
    return f"{service_name}.{job_id}.svc.cluster.local"


def apply_kv_store_service_domain(deploy_config, user_config, *, paths=None, infer_service_template_yaml=None):
    """Configure kv_store service FQDN and whether to deploy a local kv_store pod."""
    current_job_id = deploy_config[C.CONFIG_JOB_ID]
    kv_config = user_config.get(C.KV_CACHE_STORE_CONFIG, {}) if user_config else {}
    if not isinstance(kv_config, dict):
        kv_config = {}

    if infer_service_template_yaml:
        service_name = get_infer_service_kv_store_service_name(infer_service_template_yaml)
    elif paths:
        service_name = get_multi_deployment_kv_store_service_name(paths)
    else:
        service_name = k8s_utils.g_kv_store_service.split(".")[0]

    target_job_id = resolve_kv_store_target_job_id(kv_config, current_job_id)
    if target_job_id and k8s_utils.kv_store_reusable(target_job_id, service_name):
        fqdn = build_kv_store_service_fqdn(service_name, target_job_id)
        k8s_utils.set_kv_store_service(fqdn)
        k8s_utils.g_kv_store_deploy_pod = False
        logger.info(
            "Reusing kv_store service %s from target_job_id '%s'",
            fqdn,
            target_job_id,
        )
        return

    if target_job_id:
        logger.warning(
            "target_job_id '%s' kv_store service '%s' or running pod not found in cluster, deploying new kv_store pod",
            target_job_id,
            service_name,
        )

    k8s_utils.set_kv_store_service(build_kv_store_service_fqdn(service_name, current_job_id))
    k8s_utils.g_kv_store_deploy_pod = True


def normalize_kv_cache_store_config(user_config):
    kv_config = user_config.get(C.KV_CACHE_STORE_CONFIG)
    if not isinstance(kv_config, dict):
        raise ValueError(f"Missing or invalid '{C.KV_CACHE_STORE_CONFIG}' in user config")

    if C.KV_CACHE_STORE_PORT not in kv_config:
        kv_config[C.KV_CACHE_STORE_PORT] = C.DEFAULT_KV_CACHE_STORE_PORT
    if C.KV_STORE_BACKEND not in kv_config:
        kv_config[C.KV_STORE_BACKEND] = C.DEFAULT_KV_STORE_BACKEND

    # Store for use by engine generator
    k8s_utils.g_kv_cache_store_port = kv_config[C.KV_CACHE_STORE_PORT]
    k8s_utils.g_kv_store_backend = kv_config[C.KV_STORE_BACKEND]
    k8s_utils.g_mmc_config_store_port = kv_config.get(C.MMC_CONFIG_STORE_PORT_KEY, C.DEFAULT_MMC_CONFIG_STORE_PORT)
    k8s_utils.g_mmc_metrics_port = kv_config.get(
        C.MMC_METRICS_PORT_KEY,
        C.DEFAULT_KV_CACHE_STORE_PORT if kv_config[C.KV_STORE_BACKEND] == "mooncake" else C.DEFAULT_MMC_METRICS_PORT,
    )
    k8s_utils.g_mmc_local_service_mode = kv_config.get(C.MMC_LOCAL_SERVICE_CONFIG_KEY, "")

    return kv_config


def gen_kv_store_env(kv_store_config):
    service_port = kv_store_config.get(C.KV_CACHE_STORE_PORT)
    backend = kv_store_config.get(C.KV_STORE_BACKEND, C.DEFAULT_KV_STORE_BACKEND)

    kv_store_env = [
        {C.NAME: C.ENV_KVS_MASTER_SERVICE, C.VALUE: k8s_utils.g_kv_store_service},
        {C.NAME: C.ENV_KV_STORE_BACKEND, C.VALUE: backend},
        {C.NAME: C.ENV_KV_CACHE_STORE_PORT, C.VALUE: str(service_port)},
    ]

    if backend == "mooncake":
        # mooncake: eviction params are required
        missing_keys = []
        if C.KV_STORE_EVICTION_HIGH_WATERMARK_RATIO not in kv_store_config:
            missing_keys.append(C.KV_STORE_EVICTION_HIGH_WATERMARK_RATIO)
        if C.KV_STORE_EVICTION_RATIO not in kv_store_config:
            missing_keys.append(C.KV_STORE_EVICTION_RATIO)
        if missing_keys:
            raise ValueError(
                f"Missing required kv cache pool config: {missing_keys}. "
                f"Please configure them in '{C.KV_CACHE_STORE_CONFIG}'."
            )
        lease_ttl = kv_store_config.get(C.DEFAULT_KV_LEASE_TTL, 11000)

        kv_store_env.append(
            {
                C.NAME: C.ENV_KV_STORE_EVICTION_HIGH_WATERMARK_RATIO,
                C.VALUE: str(kv_store_config[C.KV_STORE_EVICTION_HIGH_WATERMARK_RATIO]),
            }
        )
        kv_store_env.append(
            {C.NAME: C.ENV_KV_STORE_EVICTION_RATIO, C.VALUE: str(kv_store_config[C.KV_STORE_EVICTION_RATIO])}
        )
        kv_store_env.append({C.NAME: C.ENV_DEFAULT_KV_LEASE_TTL, C.VALUE: str(lease_ttl)})

    elif backend == C.MMC_STORE_BACKEND:
        mmc_config_store_port = kv_store_config.get(C.MMC_CONFIG_STORE_PORT_KEY, C.DEFAULT_MMC_CONFIG_STORE_PORT)
        mmc_metrics_port = kv_store_config.get(C.MMC_METRICS_PORT_KEY, C.DEFAULT_MMC_METRICS_PORT)
        kv_store_env.append({C.NAME: C.ENV_MMC_CONFIG_STORE_URL, C.VALUE: f"tcp://0.0.0.0:{mmc_config_store_port}"})
        kv_store_env.append({C.NAME: C.ENV_MMC_METRICS_URL, C.VALUE: f"http://0.0.0.0:{mmc_metrics_port}"})

    return kv_store_env


def generate_yaml_kv_store(input_yaml, output_file, user_config, kv_store_config):
    logger.info(f"Generating YAML from {input_yaml} to {output_file}")
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    data = load_yaml(input_yaml, False)
    deployment_data = data[0]
    deployment_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]

    container = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC][C.CONTAINERS][0]
    container[C.IMAGE] = deploy_config[C.IMAGE_NAME]

    if C.ENV not in container:
        container[C.ENV] = []

    k8s_utils.apply_additional_labels_annotations(deployment_data, user_config.get(C.KV_CACHE_STORE_CONFIG, {}))

    pod_spec = deployment_data[C.SPEC][C.TEMPLATE][C.SPEC]
    apply_node_selector_override(pod_spec, deploy_config, C.KV_POOL_NODE_SELECTOR)

    service_port = kv_store_config.get(C.KV_CACHE_STORE_PORT)
    kv_store_env = gen_kv_store_env(kv_store_config)
    container[C.ENV].extend(kv_store_env)

    service_data = data[1]
    service_data[C.METADATA][C.NAMESPACE] = deploy_config[C.CONFIG_JOB_ID]
    ports = service_data.get(C.SPEC, {}).get(C.PORTS, [])
    if not ports:
        raise ValueError(
            "Missing required service ports in 'kv_cache_store_template.yaml'. "
            "Please configure spec.ports for KV pool service."
        )
    ports[0][C.PORT] = service_port
    ports[0][C.TARGET_PORT] = service_port

    # Sync memcache MetaService ports from config (indices 1,2 in template)
    backend = kv_store_config.get(C.KV_STORE_BACKEND, C.DEFAULT_KV_STORE_BACKEND)
    if backend == C.MMC_STORE_BACKEND:
        if len(ports) > 1:
            config_store_port = kv_store_config.get(C.MMC_CONFIG_STORE_PORT_KEY, C.DEFAULT_MMC_CONFIG_STORE_PORT)
            ports[1][C.PORT] = config_store_port
            ports[1][C.TARGET_PORT] = config_store_port
        if len(ports) > 2:
            metrics_port = kv_store_config.get(C.MMC_METRICS_PORT_KEY, C.DEFAULT_MMC_METRICS_PORT)
            ports[2][C.PORT] = metrics_port
            ports[2][C.TARGET_PORT] = metrics_port

    write_yaml(data, output_file, False)
    k8s_utils.g_generate_yaml_list.append(output_file)
