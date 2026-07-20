# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

# pylint: disable=cyclic-import
from __future__ import annotations

import argparse
import configparser
import os
import subprocess
import sys

import lib.constant as C
from lib.tui.step import start_monitor
from lib.utils import logger, read_json, set_env_to_shell, get_deploy_paths
from lib.update_config_whitelist import apply_whitelist_update
from lib.generator import k8s_utils
from lib.generator.k8s_utils import (
    get_baseline_config_from_configmap,
    exec_all_kubectl_multi,
    exec_all_kubectl_singer,
    create_motor_config_configmap,
    init_service_domain_name,
    get_deploy_mode_from_config,
    update_kv_store_enabled_flag,
    update_kv_conductor_enabled_flag,
    update_engine_type_flag,
    set_user_config_path,
)
from lib.generator.controller import generate_yaml_controller
from lib.generator.coordinator import generate_yaml_coordinator
from lib.generator.engine import generate_yaml_engine, update_engine_base_name, validate_instance_nums
from lib.generator.kv_cache_store import generate_yaml_kv_store, normalize_kv_cache_store_config
from lib.generator.storage import generate_yaml_storage_pvc, get_storage_entries
from lib.generator.kv_conductor import generate_yaml_kv_conductor, normalize_kv_conductor_config
from lib.generator.single_container import generate_yaml_single_container
from lib.generator.infer_service import (
    generate_yaml_infer_service_set,
    init_infer_service_domain_name,
    update_infer_service_replicas_only,
)
from lib.generator.mf_store import generate_yaml_mf_store
from lib.config_validator import (
    validate_deploy_mode_consistency,
    validate_deploy_mode_value,
    validate_only_instance_changed,
    resolve_config_paths,
    validate_pd_hybrid_config,
    validate_pd_hybrid_infer_service_template,
    validate_node_selectors,
)


def handle_update_config(user_config):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    baseline_config = get_baseline_config_from_configmap(deploy_config[C.CONFIG_JOB_ID])
    if baseline_config is None:
        raise FileNotFoundError(
            "ConfigMap motor-config not found or has no user_config in cluster. "
            "Please deploy once before updating configmap."
        )

    baseline_deploy = baseline_config[C.MOTOR_DEPLOY_CONFIG]
    if deploy_config.get(C.HYBRID_INSTANCES_NUM) != baseline_deploy.get(C.HYBRID_INSTANCES_NUM):
        raise ValueError(
            f"{C.HYBRID_INSTANCES_NUM} in user_config differs from the deployed baseline. "
            "Use --update_instance_num to scale instances instead of --update_config."
        )
    if deploy_config.get(C.P_INSTANCES_NUM) != baseline_deploy.get(C.P_INSTANCES_NUM) or deploy_config.get(
        C.D_INSTANCES_NUM
    ) != baseline_deploy.get(C.D_INSTANCES_NUM):
        raise ValueError(
            "P/D instance count in user_config differs from the deployed baseline. "
            "Use --update_instance_num to scale instances instead of --update_config."
        )

    validate_deploy_mode_consistency(deploy_config, baseline_deploy)
    user_config = apply_whitelist_update(user_config, baseline_config)

    effective_mode = resolve_deploy_mode_for_services(baseline_deploy)
    create_motor_config_configmap(
        deploy_config[C.CONFIG_JOB_ID],
        user_config=user_config,
        effective_deploy_mode=effective_mode,
    )
    logger.info("Configmap refreshed.")


def handle_update_instance_num(user_config):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    baseline_config = get_baseline_config_from_configmap(deploy_config[C.CONFIG_JOB_ID])
    if baseline_config is None:
        raise FileNotFoundError("ConfigMap motor-config not found. Please deploy once before scaling.")
    validate_only_instance_changed(user_config, baseline_config)

    baseline_deploy = baseline_config.get(C.MOTOR_DEPLOY_CONFIG, {})
    deploy_mode_arg = resolve_deploy_mode_for_services(baseline_deploy)
    current_deploy = deploy_config
    current_deploy_mode = resolve_deploy_mode_for_services(current_deploy)
    if current_deploy_mode != deploy_mode_arg:
        raise ValueError(
            f"Resolved deploy_mode from user_config ({current_deploy_mode}) differs from "
            f"cluster baseline ({deploy_mode_arg}). Only instance counts may change for scaling."
        )
    validate_deploy_mode_value(deploy_mode_arg)

    update_kv_store_enabled_flag(user_config)
    update_engine_base_name(user_config)

    k8s_utils.g_generate_yaml_list = []
    paths = get_deploy_paths()

    if deploy_mode_arg == C.DEPLOY_MODE_INFER_SERVICE_SET:
        infer_input = paths["infer_service_input_yaml"]
        infer_output = paths["infer_service_output_yaml"]
        if os.path.exists(infer_output):
            update_infer_service_replicas_only(infer_output, deploy_config)
        else:
            init_service_domain_name(paths, deploy_config)
            if not os.path.exists(infer_input):
                raise FileNotFoundError(f"InferServiceSet template yaml not found: {infer_input}.")
            init_infer_service_domain_name(infer_input, deploy_config)
            generate_yaml_infer_service_set(infer_input, infer_output, user_config)
    else:
        if k8s_utils.g_kv_store_enabled:
            normalize_kv_cache_store_config(user_config)
        generate_yaml_engine(paths["engine_input_yaml"], paths["engine_output_yaml"], user_config)

    exec_all_kubectl_multi(deploy_config, baseline_config, deploy_mode_arg, user_config=user_config)
    logger.info("instance num update end.")


def deploy_services_multi_yaml(paths, user_config, dry_run=False):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    init_service_domain_name(paths, deploy_config)
    generate_yaml_controller(paths["controller_input_yaml"], paths["controller_output_yaml"], user_config)
    generate_yaml_coordinator(paths["coordinator_input_yaml"], paths["coordinator_output_yaml"], user_config)
    # normalize_kv_cache_store_config must be called before generate_yaml_engine
    # so g_mmc_local_service_mode is set when build_engine_env_items() reads it
    kv_store_config = None
    if k8s_utils.g_kv_store_enabled:
        kv_store_config = normalize_kv_cache_store_config(user_config)
    generate_yaml_engine(paths["engine_input_yaml"], paths["engine_output_yaml"], user_config)
    storage_entries = get_storage_entries(user_config)
    if storage_entries:
        generate_yaml_storage_pvc(
            paths["storage_pvc_input_yaml"], paths["storage_pvc_output_yaml"], user_config, storage_entries
        )
    if kv_store_config is not None:
        generate_yaml_kv_store(
            paths["kv_store_input_yaml"], paths["kv_store_output_yaml"], user_config, kv_store_config
        )
    if k8s_utils.g_kv_conductor_enabled:
        kv_conductor_config = normalize_kv_conductor_config(user_config)
        generate_yaml_kv_conductor(
            paths["kv_conductor_input_yaml"], paths["kv_conductor_output_yaml"], user_config, kv_conductor_config
        )
    if k8s_utils.g_mf_store_enabled:
        generate_yaml_mf_store(paths["mf_store_input_yaml"], paths["mf_store_output_yaml"], user_config)
    if not dry_run:
        exec_all_kubectl_multi(deploy_config, None, C.DEPLOY_MODE_MULTI_DEPLOYMENT_YAML, user_config=user_config)


def deploy_services_infer_service_set(paths, user_config, dry_run=False):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    init_service_domain_name(paths, deploy_config)
    infer_input = paths["infer_service_input_yaml"]
    if not os.path.exists(infer_input):
        raise FileNotFoundError(
            f"InferServiceSet template yaml not found: {infer_input}. "
            "Please ensure infer_service_template.yaml exists in yaml_template folder."
        )
    init_infer_service_domain_name(infer_input, deploy_config)
    generate_yaml_infer_service_set(infer_input, paths["infer_service_output_yaml"], user_config)
    storage_entries = get_storage_entries(user_config)
    if storage_entries:
        generate_yaml_storage_pvc(
            paths["storage_pvc_input_yaml"], paths["storage_pvc_output_yaml"], user_config, storage_entries
        )
    if not dry_run:
        deploy_mode_arg = resolve_deploy_mode_for_services(deploy_config)
        exec_all_kubectl_multi(deploy_config, None, deploy_mode_arg, user_config=user_config)


def deploy_services_single_container(paths, user_config, dry_run=False):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    update_kv_store_enabled_flag(user_config)
    generate_yaml_single_container(
        paths["single_container_input_yaml"], paths["single_container_output_yaml"], user_config
    )
    if not dry_run:
        exec_all_kubectl_singer(deploy_config, paths["single_container_output_yaml"])


def resolve_deploy_mode_for_services(deploy_config):
    return get_deploy_mode_from_config(deploy_config)


def deploy_services(user_config, env_config_path, dry_run=False, auto_log_collect=False):
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    update_kv_store_enabled_flag(user_config)
    update_kv_conductor_enabled_flag(user_config)
    update_engine_type_flag(user_config)

    update_engine_base_name(user_config)

    deploy_mode_arg = resolve_deploy_mode_for_services(deploy_config)
    if not dry_run:
        set_env_to_shell(user_config, env_config_path, deploy_mode_arg)
    else:
        logger.info("dry-run: skip set_env_to_shell")

    if deploy_mode_arg != C.DEPLOY_MODE_SINGLE_CONTAINER and not dry_run:
        validate_node_selectors(deploy_config)

    k8s_utils.g_generate_yaml_list = []
    paths = get_deploy_paths()

    if deploy_mode_arg == C.DEPLOY_MODE_SINGLE_CONTAINER:
        deploy_services_single_container(paths, user_config, dry_run=dry_run)
    elif deploy_mode_arg == C.DEPLOY_MODE_INFER_SERVICE_SET:
        deploy_services_infer_service_set(paths, user_config, dry_run=dry_run)
    else:
        deploy_services_multi_yaml(paths, user_config, dry_run=dry_run)

    if dry_run:
        logger.info("all deploy end (dry-run: kubectl apply skipped).")
    else:
        if auto_log_collect:
            _start_log_collection(deploy_config)
        logger.info("all deploy end.")


def _start_log_collection(deploy_config):
    job_id = deploy_config.get(C.CONFIG_JOB_ID, "")
    if not job_id:
        logger.warning("job_id not found in deploy config, skip log collection.")
        return

    deployer_dir = os.path.dirname(os.path.abspath(__file__))
    ini_path = os.path.join(deployer_dir, "log_collect", "log_config.ini")
    if not os.path.exists(ini_path):
        logger.warning("log_config.ini not found at %s, skip log collection.", ini_path)
        return

    config = configparser.ConfigParser()
    config.read(ini_path)
    config.set("LogSetting", "name_space", job_id)
    with open(ini_path, "w", encoding='utf-8') as f:
        config.write(f)
    logger.info("Updated log_config.ini: name_space = %s", job_id)

    show_log_path = os.path.join(deployer_dir, "show_log.sh")
    result = subprocess.run(
        ["/bin/bash", show_log_path],
        cwd=deployer_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "show_log.sh exited with code %s: %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
        return
    logger.info("Log collection started via show_log.sh")


def handle_general_config(args):
    config_tool_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_tool")
    script = os.path.join(config_tool_dir, "vllm_to_motor.py")
    cmd = [
        sys.executable,
        script,
        "--deploy-scenario",
        args.deploy_scenario,
        "--hardware-type",
        args.hardware_type,
    ]
    if args.weight_path:
        cmd.extend(["--weight-path", args.weight_path])
    if args.image_name:
        cmd.extend(["--image-name", args.image_name])
    subprocess.run(cmd, cwd=config_tool_dir, check=True)


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["deploy", "general_config"],
        default="deploy",
        help="deploy: deploy service; general_config: generate user_config.json/env.json from config_tool",
    )
    parser.add_argument(
        "--deploy-scenario",
        choices=["hybrid", "separate"],
        help="Required for general_config mode",
    )
    parser.add_argument("--hardware-type", type=str, help="Required for general_config mode: A2 or A3")
    parser.add_argument("--weight-path", type=str, help="Optional for general_config mode: weight mount path")
    parser.add_argument("--image-name", type=str, help="Optional for general_config mode: container image name")
    parser.add_argument(
        "--config_dir",
        "--dir",
        type=str,
        help="Directory containing user_config.json and env.json, "
        "select from examples/infer_engines/ based on your engine and model requirements",
    )
    parser.add_argument(
        "--user_config_path",
        "--config",
        type=str,
        help="Path of user config, takes precedence over config_dir if specified",
    )
    parser.add_argument(
        "--env_config_path", "--env", type=str, help="Path of env config, takes precedence over config_dir if specified"
    )
    parser.add_argument(
        "--update_config", action="store_true", help="Only refresh configmap without applying deployments"
    )
    parser.add_argument(
        "--update_instance_num",
        action="store_true",
        help="Scale instances by comparing ConfigMap baseline with current user_config",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate YAML only: skip set_env_to_shell and kubectl apply (normal deploy path only)",
    )
    parser.add_argument(
        "--auto_log_collect",
        action="store_true",
        help="Automatically start log collection after deployment",
    )
    parser.add_argument(
        "--nostep",
        action="store_true",
        help="Do not display the service startup progress bar after deployment",
    )
    return parser.parse_args()


def calculate_pod_count(deploy_config):
    deploy_mode = resolve_deploy_mode_for_services(deploy_config)

    # Single-container mode: one Deployment, one pod (unless hybrid count is set)
    if deploy_mode == C.DEPLOY_MODE_SINGLE_CONTAINER:
        if C.HYBRID_INSTANCES_NUM in deploy_config and C.SINGLE_HYBRID_INSTANCE_POD_NUM in deploy_config:
            return deploy_config[C.HYBRID_INSTANCES_NUM] * deploy_config[C.SINGLE_HYBRID_INSTANCE_POD_NUM]
        return 1

    if C.P_INSTANCES_NUM in deploy_config and C.SINGER_P_INSTANCES_NUM in deploy_config:
        p_pod_cnt = deploy_config[C.P_INSTANCES_NUM] * deploy_config[C.SINGER_P_INSTANCES_NUM]
    else:
        p_pod_cnt = 0

    if C.D_INSTANCES_NUM in deploy_config and C.SINGER_D_INSTANCES_NUM in deploy_config:
        d_pod_cnt = deploy_config[C.D_INSTANCES_NUM] * deploy_config[C.SINGER_D_INSTANCES_NUM]
    else:
        d_pod_cnt = 0

    if C.E_INSTANCES_NUM in deploy_config and C.SINGER_E_INSTANCES_NUM in deploy_config:
        e_pod_cnt = deploy_config[C.E_INSTANCES_NUM] * deploy_config[C.SINGER_E_INSTANCES_NUM]
    else:
        e_pod_cnt = 0
    if C.HYBRID_INSTANCES_NUM in deploy_config and C.SINGLE_HYBRID_INSTANCE_POD_NUM in deploy_config:
        u_pod_cnt = deploy_config[C.HYBRID_INSTANCES_NUM] * deploy_config[C.SINGLE_HYBRID_INSTANCE_POD_NUM]
    else:
        u_pod_cnt = 0

    return e_pod_cnt + p_pod_cnt + d_pod_cnt + u_pod_cnt


def _launch_tui(user_config: dict, log_running: bool = False, deployed: bool | None = None) -> None:
    """Launch the interactive TUI, deriving state from *user_config*."""
    from lib.tui import run_interactive_session

    deploy_config = user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
    name_space = deploy_config.get(C.CONFIG_JOB_ID, "")
    pod_cnt = calculate_pod_count(deploy_config) if deploy_config else 0
    if deployed is None:
        deployed = bool(deploy_config)

    run_interactive_session(name_space, pod_cnt, user_config, log_running=log_running, deployed=deployed)


def start_monitoring(user_config):
    """Start tqdm-based pod startup progress monitoring (non-TUI path)."""
    deploy_config = user_config[C.MOTOR_DEPLOY_CONFIG]
    name_space = deploy_config[C.CONFIG_JOB_ID]
    pod_cnt = calculate_pod_count(deploy_config)
    start_monitor(name_space, pod_cnt)


def main():
    args = parse_arguments()

    if args.mode == "general_config":
        if not args.deploy_scenario or not args.hardware_type:
            logger.error("In general_config mode, the --deploy-scenario and --hardware-type parameters are required.")
            sys.exit(2)
        handle_general_config(args)
        return

    # No configuration at all → launch TUI directly (undeployed mode)
    no_config = not (args.config_dir or args.user_config_path or args.env_config_path)
    if no_config:
        if sys.stdout.isatty():
            _launch_tui({}, log_running=False, deployed=False)
            return
        logger.error("No configuration provided. Use --config_dir <dir> or run in a terminal for interactive mode.")
        sys.exit(1)

    user_config_path, env_config_path = resolve_config_paths(
        args.config_dir, args.user_config_path, args.env_config_path
    )

    set_user_config_path(user_config_path)
    os.makedirs(C.OUTPUT_ROOT_PATH, exist_ok=True)
    user_config = read_json(user_config_path)
    if C.HYBRID_INSTANCES_NUM in user_config.get(C.MOTOR_DEPLOY_CONFIG, {}):
        validate_pd_hybrid_config(user_config)
        paths = get_deploy_paths()
        validate_pd_hybrid_infer_service_template(user_config, paths["infer_service_input_yaml"])
    validate_instance_nums(user_config)

    if args.update_config:
        handle_update_config(user_config)
        return
    if args.update_instance_num:
        handle_update_instance_num(user_config)
        return

    deploy_services(user_config, env_config_path, dry_run=args.dry_run, auto_log_collect=args.auto_log_collect)
    logger.info("Deploy complete.")

    deploy_config = user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
    deploy_mode_arg = resolve_deploy_mode_for_services(deploy_config)
    if not args.dry_run and not args.nostep and deploy_mode_arg != C.DEPLOY_MODE_SINGLE_CONTAINER:
        start_monitoring(user_config)


if __name__ == "__main__":
    main()
