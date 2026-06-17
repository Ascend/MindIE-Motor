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
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import uuid
import yaml as ym

import lib.constant as C

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def read_json(file_path):
    """Read JSON file"""
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def write_json(file_path, data):
    """Write data to JSON file"""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_yaml(data, output_file, single_doc=True):
    """Write to YAML file"""
    logger.info(f"Writing YAML to {output_file}")
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        if single_doc:
            ym.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False, width=float("inf"))
        else:
            ym.dump_all(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False, width=float("inf"))


def load_yaml(input_yaml, single_doc):
    """Load YAML file"""
    with open(input_yaml, 'r', encoding="utf-8") as f:
        if single_doc:
            data = ym.safe_load(f)
        else:
            data = list(ym.safe_load_all(f))
    return data


def exec_cmd(command):
    """Execute command"""
    logger.info(f"Executing command: {command}")
    os.popen(command).read()


def safe_exec_cmd(command):
    """Safely execute command"""
    try:
        exec_cmd(command)
    except Exception as e:
        logger.warning(f"Command execution failed: {e}")
        raise


def shell_escape(value):
    if not isinstance(value, str):
        return str(value)
    
    value = value.replace('\\', '\\\\')
    value = value.replace('"', '\\"')
    value = value.replace('`', '\\`')
    value = value.replace('\n', '\\n')
    value = value.replace('\r', '\\r')
    value = value.replace('\t', '\\t')
    
    return value


def update_shell_safely(script_path, env_config, component_key="", function_name="set_common_env"):
    all_env_vars = {}
    all_env_vars.update(env_config[C.MOTOR_COMMON_ENV])
    if component_key and component_key in env_config:
        all_env_vars.update(env_config[component_key])

    with open(script_path, 'r') as f:
        lines = f.readlines()

    start_idx, end_idx = -1, -1
    for i, line in enumerate(lines):
        if line.strip().startswith(f"function {function_name}()"):
            start_idx = i
        elif start_idx != -1 and line.strip() == "}":
            end_idx = i
            break

    new_function_lines = [
        f"function {function_name}() {{\n",
        *[
            f'    export {key}="{shell_escape(value)}"\n' if isinstance(value, str) else f'    export {key}={value}\n'
            for key, value in all_env_vars.items()
        ],
        "}\n"
    ]

    if start_idx != -1 and end_idx != -1:
        new_lines = lines[:start_idx] + new_function_lines + lines[end_idx + 1:]
    else:
        new_lines = new_function_lines + lines

    with open(script_path, 'w') as f:
        f.writelines(new_lines)


def generate_unique_id():
    timestamp = str(int(time.time() * 1000))
    random_part = str(uuid.uuid4()).split('-')[0]
    return f"{timestamp}{random_part}"


def get_json_by_path(data, path, default=None):
    keys = path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
            if current is None:
                return default
        else:
            return default
    return current


def resolve_model_name(engine_section, default="Unknown"):
    """Resolve model_name from engine_config (native) or model_config (legacy).

    Checks engine_config first using engine-type-specific keys, then falls back
    to the deprecated model_config.
    """
    engine_config = engine_section.get("engine_config", {})
    engine_type = engine_section.get("engine_type", "vllm")
    if engine_type == "sglang":
        name = engine_config.get("served-model-name")
    else:
        name = engine_config.get("served_model_name")
    if name:
        return name
    model_config = engine_section.get("model_config", {})
    return model_config.get("model_name", default)


def obtain_engine_instance_total(deploy_config):
    if C.HYBRID_INSTANCES_NUM in deploy_config:
        try:
            hybrid_instances = int(deploy_config[C.HYBRID_INSTANCES_NUM])
        except (TypeError, ValueError) as e:
            raise ValueError(f"{C.HYBRID_INSTANCES_NUM} must be an integer") from e
        return hybrid_instances, 0

    if C.P_INSTANCES_NUM not in deploy_config:
        raise KeyError(f"{C.P_INSTANCES_NUM} is required in motor_deploy_config")
    if C.D_INSTANCES_NUM not in deploy_config:
        raise KeyError(f"{C.D_INSTANCES_NUM} is required in motor_deploy_config")
    try:
        p_instances = int(deploy_config[C.P_INSTANCES_NUM])
        d_instances = int(deploy_config[C.D_INSTANCES_NUM])
    except (TypeError, ValueError) as e:
        raise ValueError(f"{C.P_INSTANCES_NUM} and {C.D_INSTANCES_NUM} must be integers") from e
    return p_instances, d_instances


def obtain_engine_e_instance_total(deploy_config):
    if C.E_INSTANCES_NUM not in deploy_config:
        return 0
    try:
        e_instances = int(deploy_config[C.E_INSTANCES_NUM])
    except (TypeError, ValueError) as e:
        raise ValueError(f"{C.E_INSTANCES_NUM} must be integers") from e
    return e_instances


def modify_log_mount(deployment_data, user_config, app_type):
    host_log_dir = "/root/ascend/log"
    temp_app_config = None
    
    if app_type == "mindie-motor-controller":
        temp_app_config = get_json_by_path(user_config, C.MOTOR_CONTROLLER_CONFIG)
    elif app_type == "mindie-motor-coordinator":
        temp_app_config = get_json_by_path(user_config, C.MOTOR_COORDINATOR_CONFIG)
    else:
        temp_app_config = get_json_by_path(user_config, C.MOTOR_NODEMANAGER_CONFIG)

    if temp_app_config:
        host_log_dir = get_json_by_path(temp_app_config, "logging_config.host_log_dir", host_log_dir)
    
    for volume in deployment_data[C.SPEC][C.TEMPLATE][C.SPEC]["volumes"]:
        if volume["name"] == C.LOG_PATH:
            volume["hostPath"]["path"] = host_log_dir


def set_env_to_shell(user_config, env_config_path, deploy_mode):
    if not env_config_path or not os.path.exists(env_config_path):
        logger.error("env_config_path %s does not exist!", env_config_path)
        return

    env_config = read_json(env_config_path)

    prefill_section = user_config.get("motor_engine_prefill_config", {})
    union_section = user_config.get("motor_engine_union_config", {})
    engine_section = prefill_section if prefill_section else union_section

    engine_type = get_json_by_path(engine_section, "engine_type", "Unknown")
    model_name = resolve_model_name(engine_section)
    north_platform = get_json_by_path(user_config, "north_config.name")

    if C.MOTOR_COMMON_ENV not in env_config:
        env_config[C.MOTOR_COMMON_ENV] = {}

    env_config[C.MOTOR_COMMON_ENV][C.ENV_ENGINE_TYPE] = engine_type
    logger.info(f"Set {C.ENV_ENGINE_TYPE} environment variable to: {engine_type}")

    env_config[C.MOTOR_COMMON_ENV][C.ENV_MODEL_NAME] = model_name
    logger.info(f"Set {C.ENV_MODEL_NAME} environment variable to: {model_name}")

    env_config[C.MOTOR_COMMON_ENV][C.ENV_NORTH_PLATFORM] = north_platform
    logger.info(f"Set {C.ENV_NORTH_PLATFORM} environment variable to: {north_platform}")

    service_id = (
        f"{get_json_by_path(user_config, 'motor_deploy_config.job_id')}_"
        f"{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d%H%M%S')}"
    )
    env_config[C.MOTOR_COMMON_ENV][C.ENV_SERVICE_ID] = service_id
    logger.info(f"Set {C.ENV_SERVICE_ID} environment variable to: {service_id}")

    union_env_key = "motor_engine_union_env"
    if union_env_key not in env_config:
        # Backward compatibility: if union env is absent, reuse prefill env as union defaults.
        env_config[union_env_key] = dict(env_config.get("motor_engine_prefill_env", {}))

    update_shell_safely(C.COMMON_SHELL_PATH, env_config, C.MOTOR_COMMON_ENV, "set_common_env")

    if deploy_mode == C.DEPLOY_MODE_SINGLE_CONTAINER:
        update_shell_safely(C.SINGLE_CONTAINER_SHELL_PATH, env_config, "motor_controller_env", "set_controller_env")
        update_shell_safely(C.SINGLE_CONTAINER_SHELL_PATH, env_config, "motor_coordinator_env", "set_coordinator_env")
        update_shell_safely(C.SINGLE_CONTAINER_SHELL_PATH, env_config, "motor_engine_encode_env", "set_encode_env")
        update_shell_safely(C.SINGLE_CONTAINER_SHELL_PATH, env_config, "motor_engine_prefill_env", "set_prefill_env")
        update_shell_safely(C.SINGLE_CONTAINER_SHELL_PATH, env_config, "motor_engine_decode_env", "set_decode_env")
        update_shell_safely(C.SINGLE_CONTAINER_SHELL_PATH, env_config, union_env_key, "set_union_env")
        update_shell_safely(C.SINGLE_CONTAINER_SHELL_PATH, env_config, "motor_kv_cache_pool_env", "set_kv_pool_env")
        update_shell_safely(C.MF_STORE_SHELL_PATH, env_config, "motor_mf_store_env", "set_mf_store_env")
        update_shell_safely(
            C.SINGLE_CONTAINER_SHELL_PATH, env_config, "motor_kv_conductor_env", "set_kv_conductor_env"
        )
    else:
        update_shell_safely(C.CONTROLLER_SHELL_PATH, env_config, "motor_controller_env", "set_controller_env")
        update_shell_safely(C.COORDINATOR_SHELL_PATH, env_config, "motor_coordinator_env", "set_coordinator_env")
        update_shell_safely(C.ENGINE_SHELL_PATH, env_config, "motor_engine_encode_env", "set_encode_env")
        update_shell_safely(C.ENGINE_SHELL_PATH, env_config, "motor_engine_prefill_env", "set_prefill_env")
        update_shell_safely(C.ENGINE_SHELL_PATH, env_config, "motor_engine_decode_env", "set_decode_env")
        update_shell_safely(C.ENGINE_SHELL_PATH, env_config, union_env_key, "set_union_env")
        update_shell_safely(C.KV_POOL_SHELL_PATH, env_config, "motor_kv_cache_pool_env", "set_kv_pool_env")
        update_shell_safely(C.MF_STORE_SHELL_PATH, env_config, "motor_mf_store_env", "set_mf_store_env")
        update_shell_safely(
            C.KV_CONDUCTOR_SHELL_PATH, env_config, "motor_kv_conductor_env", "set_kv_conductor_env"
        )


def get_deploy_paths():
    from lib.generator import k8s_utils
    return {
        "controller_input_yaml": os.path.join(C.DEPLOY_YAML_ROOT_PATH, 'controller_template.yaml'),
        "controller_output_yaml": os.path.join(C.OUTPUT_ROOT_PATH, 'mindie_motor_controller.yaml'),
        "coordinator_input_yaml": os.path.join(C.DEPLOY_YAML_ROOT_PATH, 'coordinator_template.yaml'),
        "coordinator_output_yaml": os.path.join(C.OUTPUT_ROOT_PATH, 'mindie_motor_coordinator.yaml'),
        "engine_input_yaml": os.path.join(C.DEPLOY_YAML_ROOT_PATH, 'engine_template.yaml'),
        "engine_output_yaml": os.path.join(C.OUTPUT_ROOT_PATH, k8s_utils.g_engine_base_name),
        "kv_pool_input_yaml": os.path.join(C.DEPLOY_YAML_ROOT_PATH, 'kv_pool_template.yaml'),
        "kv_pool_output_yaml": os.path.join(C.OUTPUT_ROOT_PATH, 'mindie_motor_kv_pool.yaml'),
        "kv_conductor_input_yaml": os.path.join(C.DEPLOY_YAML_ROOT_PATH, 'kv_conductor_template.yaml'),
        "kv_conductor_output_yaml": os.path.join(C.OUTPUT_ROOT_PATH, 'mindie_motor_kv_conductor.yaml'),
        "infer_service_input_yaml": os.path.join(C.DEPLOY_YAML_ROOT_PATH, 'infer_service_template.yaml'),
        "infer_service_output_yaml": os.path.join(C.OUTPUT_ROOT_PATH, 'infer_service.yaml'),
        "single_container_input_yaml": os.path.join(C.DEPLOY_YAML_ROOT_PATH, 'single_container_template.yaml'),
        "single_container_output_yaml": os.path.join(C.OUTPUT_ROOT_PATH, 'mindie_motor_single_container.yaml'),
        "mf_store_input_yaml": os.path.join(C.DEPLOY_YAML_ROOT_PATH, 'mf_store_template.yaml'),
        "mf_store_output_yaml": os.path.join(C.OUTPUT_ROOT_PATH, 'mindie_motor_mf_store.yaml')
    }
