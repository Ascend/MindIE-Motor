# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
import json
import os
import shutil
import subprocess

import lib.constant as C
from lib.utils import logger, load_yaml
from lib.generator.infer_service import get_infer_role, _find_infer_service_set_doc
from lib.generator.k8s_utils import get_deploy_mode_from_config


PD_SEPARATION_DEPLOY_KEYS = {
    C.P_INSTANCES_NUM,
    C.D_INSTANCES_NUM,
    C.SINGER_P_INSTANCES_NUM,
    C.SINGER_D_INSTANCES_NUM,
    C.P_POD_NPU_NUM,
    C.D_POD_NPU_NUM,
}

PD_HYBRID_REQUIRED_DEPLOY_KEYS = {
    C.HYBRID_INSTANCES_NUM,
    C.SINGLE_HYBRID_INSTANCE_POD_NUM,
    C.HYBRID_POD_NPU_NUM,
}


def resolve_config_paths(config_dir, user_config_path, env_config_path):
    if not config_dir and not user_config_path and not env_config_path:
        logger.error("No configuration provided. Please use one of the following options:")
        logger.error("  --config_dir <dir>     : Directory containing user_config.json and env.json")
        logger.error("  --config <file>        : Path to user_config.json (requires --env)")
        logger.error("  --env <file>           : Path to env.json (requires --config)")
        logger.error("Example:")
        logger.error("  python deploy.py --config_dir ../infer_engines/vllm")
        logger.error(
            "  python deploy.py --config ../infer_engines/vllm/user_config.json --env ../infer_engines/vllm/env.json"
        )
        raise ValueError("Missing required configuration. Use --config_dir or both --config and --env.")

    if config_dir:
        dir_user_config = os.path.join(config_dir, "user_config.json")
        dir_env_config = os.path.join(config_dir, "env.json")

        if not user_config_path:
            if os.path.exists(dir_user_config):
                user_config_path = dir_user_config
                logger.info(f"Using user_config.json from config_dir: {user_config_path}")
            else:
                logger.error(f"user_config.json not found in {config_dir}")
                raise FileNotFoundError(f"user_config.json not found in {config_dir}")

        if not env_config_path:
            if os.path.exists(dir_env_config):
                env_config_path = dir_env_config
                logger.info(f"Using env.json from config_dir: {env_config_path}")
            else:
                logger.error(f"env.json not found in {config_dir}")
                raise FileNotFoundError(f"env.json not found in {config_dir}")

    if user_config_path and not env_config_path:
        logger.error("--config is specified but --env is missing")
        raise ValueError("Both --config and --env must be specified together, or use --config_dir")

    if env_config_path and not user_config_path:
        logger.error("--env is specified but --config is missing")
        raise ValueError("Both --config and --env must be specified together, or use --config_dir")

    logger.info(f"{C.GREEN}User config path: {user_config_path}{C.RESET}")
    logger.info(f"{C.GREEN}Env config path: {env_config_path}{C.RESET}")

    return user_config_path, env_config_path


def strip_instance_nums(config_dict):
    cleaned = json.loads(json.dumps(config_dict))
    cleaned["motor_deploy_config"].pop(C.E_INSTANCES_NUM, None)
    cleaned["motor_deploy_config"].pop(C.P_INSTANCES_NUM, None)
    cleaned["motor_deploy_config"].pop(C.D_INSTANCES_NUM, None)
    cleaned["motor_deploy_config"].pop(C.HYBRID_INSTANCES_NUM, None)
    cleaned["motor_deploy_config"].pop(C.DEPLOY_MODE_CONFIG_KEY, None)
    return cleaned


def validate_only_instance_changed(current_config, baseline_config):
    if strip_instance_nums(current_config) != strip_instance_nums(baseline_config):
        raise ValueError(
            "user_config changes detected beyond instance numbers. "
            "Only e_instances_num/p_instances_num/d_instances_num/hybrid_instances_num "
            "can be modified for scaling."
        )


def validate_deploy_mode_consistency(deploy_config, baseline_config):
    """Validate that deploy_mode hasn't changed when updating config."""
    baseline_mode = get_deploy_mode_from_config(baseline_config)
    current_mode = get_deploy_mode_from_config(deploy_config)
    if baseline_mode != current_mode:
        raise ValueError(
            f"motor_deploy_config.{C.DEPLOY_MODE_CONFIG_KEY} cannot be changed when updating config. "
            f"Current deployment uses '{baseline_mode}', user_config has '{current_mode}'."
        )


def validate_deploy_mode_value(deploy_mode_arg):
    """Validate deploy_mode value is valid."""
    if deploy_mode_arg not in C.VALID_DEPLOY_MODES:
        raise ValueError(
            f"Baseline config has invalid {C.DEPLOY_MODE_CONFIG_KEY}: {deploy_mode_arg}. "
            f"Must be one of {list(C.VALID_DEPLOY_MODES)}."
        )


def validate_pd_hybrid_config(user_config):
    deploy_config = user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
    if not isinstance(deploy_config, dict):
        raise ValueError("motor_deploy_config is required for PD hybrid.")

    missing_keys = PD_HYBRID_REQUIRED_DEPLOY_KEYS - deploy_config.keys()
    if missing_keys:
        raise ValueError(f"PD hybrid config missing required keys: {sorted(missing_keys)}")

    mixed_deploy_keys = PD_SEPARATION_DEPLOY_KEYS & deploy_config.keys()
    if mixed_deploy_keys:
        raise ValueError(f"PD hybrid config cannot include separation keys: {sorted(mixed_deploy_keys)}")

    if "engine_topology" in deploy_config:
        raise ValueError("PD hybrid config must not include motor_deploy_config.engine_topology.")

    if C.MOTOR_ENGINE_UNION_CONFIG not in user_config:
        raise ValueError("PD hybrid config requires motor_engine_union_config.")
    if C.MOTOR_ENGINE_PREFILL_CONFIG in user_config or "motor_engine_decode_config" in user_config:
        raise ValueError("PD hybrid config cannot include prefill/decode engine config sections.")


def validate_pd_hybrid_infer_service_template(user_config, infer_service_template_path):
    """Require union role in InferServiceSet template when PD hybrid uses CRD deploy mode."""
    deploy_config = user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
    deploy_mode = deploy_config.get(C.DEPLOY_MODE_CONFIG_KEY, C.DEPLOY_MODE_INFER_SERVICE_SET)
    if deploy_mode == C.DEPLOY_MODE_MULTI_DEPLOYMENT_YAML:
        return
    if not os.path.exists(infer_service_template_path):
        raise FileNotFoundError(
            f"InferServiceSet template yaml not found for PD hybrid CRD validation: {infer_service_template_path}"
        )
    all_docs = load_yaml(infer_service_template_path, False)
    if not isinstance(all_docs, list):
        all_docs = [all_docs]
    infer_doc = _find_infer_service_set_doc(all_docs)
    if not get_infer_role(infer_doc, C.ROLE_UNION):
        raise ValueError("PD hybrid with infer_service_set requires a 'union' role in infer_service_template.yaml.")


def _get_pd_heterogeneous_config(deploy_config):
    """Extract PD heterogeneous config from deploy_config, returns None if disabled."""
    if deploy_config.get(C.ENABLE_PD_HETEROGENEOUS) is not True:
        return None
    label_key = deploy_config.get(C.PD_HETEROGENEOUS_LABEL_KEY, C.DEFAULT_PD_HETEROGENEOUS_LABEL_KEY)
    prefill_value = deploy_config.get(C.PD_HETEROGENEOUS_PREFILL_LABEL_VALUE, C.DEFAULT_PD_HETEROGENEOUS_PREFILL_VALUE)
    decode_value = deploy_config.get(C.PD_HETEROGENEOUS_DECODE_LABEL_VALUE, C.DEFAULT_PD_HETEROGENEOUS_DECODE_VALUE)
    return {
        "label_key": label_key,
        "prefill_value": prefill_value,
        "decode_value": decode_value,
    }


def _get_hardware_node_labels(hardware_type):
    """Extract nodeSelector labels determined by hardware_type.

    Returns dict of label key-value pairs. Raises ValueError for unknown types.
    """
    if hardware_type in C.HARDWARE_TYPE_A2:
        return {C.ACCELERATOR: C.ACCELERATOR_910, C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_910B}
    if hardware_type in C.HARDWARE_TYPE_A3:
        return {C.ACCELERATOR: C.ACCELERATOR_910, C.ACCELERATOR_TYPE: C.ACCELERATOR_TYPE_A3}
    if hardware_type in C.HARDWARE_TYPE_950I_A5:
        return {C.ACCELERATOR: C.ACCELERATOR_A5, C.ACCELERATOR_TYPE: hardware_type}
    known = [*sorted(C.HARDWARE_TYPE_A2), *sorted(C.HARDWARE_TYPE_A3), *C.HARDWARE_TYPE_950I_A5]
    raise ValueError(f"Unknown hardware_type '{hardware_type}'. Supported values: {known}")


def _validate_node_labels_exist(labels, node_desc):
    """Assert that at least one node in the cluster matches ALL given labels (AND).

    Args:
        labels: dict of label key -> value that must all be present on a single node.
        node_desc: human-readable description for error messages (e.g. "prefill(P)").
    """
    if not labels:
        return
    label_selector = ",".join(f"{k}={v}" for k, v in labels.items())
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        raise RuntimeError("kubectl not found in PATH")
    try:
        result = subprocess.run(
            [kubectl, "get", "nodes", "-l", label_selector, "-o", "name"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to query cluster nodes for {node_desc} with labels {labels}: {e}")

    if result.returncode != 0:
        raise RuntimeError(
            f"kubectl get nodes failed for {node_desc} with labels {labels}. stderr: {result.stderr.strip()}"
        )

    nodes = [line for line in result.stdout.strip().split("\n") if line]
    if not nodes:
        raise RuntimeError(
            f"No node in cluster matches nodeSelector for {node_desc}: {labels}. "
            f"Please ensure suitable nodes are labeled correctly."
        )

    logger.info(f"Node selector validated for {node_desc}: {labels} -> {len(nodes)} node(s) found")


def validate_node_selectors(deploy_config):
    """Validate that cluster nodes exist for every nodeSelector combination to be used.

    Always validates base hardware labels (accelerator-type, accelerator).
    When PD heterogeneous deployment is enabled, additionally validates the
    combined prefill/decode labels per node type.
    """
    hardware_type = deploy_config.get(C.HARDWARE_TYPE)
    base_labels = _get_hardware_node_labels(hardware_type)

    pd_config = _get_pd_heterogeneous_config(deploy_config)

    if pd_config is not None:
        label_key = pd_config["label_key"]
        prefill_labels = {**base_labels, label_key: pd_config["prefill_value"]}
        decode_labels = {**base_labels, label_key: pd_config["decode_value"]}
        _validate_node_labels_exist(prefill_labels, "prefill(P)")
        _validate_node_labels_exist(decode_labels, "decode(D)")
        logger.info(
            f"PD heterogeneous node selectors validated: prefill -> {prefill_labels}, decode -> {decode_labels}"
        )
    else:
        _validate_node_labels_exist(base_labels, "engine")
