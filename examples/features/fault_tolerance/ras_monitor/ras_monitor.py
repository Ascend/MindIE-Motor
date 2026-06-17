#!/usr/bin/env python3
# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
import getpass
import json
import subprocess
import time
import logging
import os
import argparse
import stat
import ctypes
import sys
from ssl import create_default_context, Purpose
from dataclasses import dataclass
import urllib3

# Configure log format and level
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Output to console
    ]
)

TEST_METRIC_NAME = "request_success_total"


@dataclass
class CheckParams:
    with_cert: bool
    model_name: str
    input_content: str
    coordinator_port: str
    coordinator_manage_port: str
    namespace: str


def kubectl_get_pods_info():
    return subprocess.run(
        ["kubectl", "get", "pods", "-A", "-owide"],
        capture_output=True,
        text=True,
        check=True
    ).stdout


def load_cert():
    context = create_default_context(Purpose.SERVER_AUTH)
    cert_file_map = {
        "ca_cert": "./security/ca.pem",
        "tls_cert": "./security/cert.pem",
        "tls_key": "./security/cert.key.pem",
    }
    for _, cert_files in cert_file_map.items():
        if not os.path.exists(cert_files):
            return None
    for _, cert_files in cert_file_map.items():
        try:
            file_stat = os.stat(cert_files)
            file_mode = file_stat.st_mode

            if file_mode & (stat.S_IRWXG | stat.S_IRWXO | stat.S_IXUSR):
                logging.error(f"{cert_files} has overly permissive permissions"
                              f" (current: {oct(file_mode & 0o777)[-3:]}, required: 600 or less)")
                return None

        except OSError as e:
            logging.error(f"Error checking permissions for {cert_files}: {e}")
            return None

    password = getpass.getpass("Please enter the coordinator cert password: ")
    context.load_verify_locations(cafile=cert_file_map["ca_cert"])
    context.load_cert_chain(
        certfile=cert_file_map["tls_cert"],
        keyfile=cert_file_map["tls_key"],
        password=password
    )
    password_len = len(password)
    password_offset = sys.getsizeof(password) - password_len - 1
    ctypes.memset(id(password) + password_offset, 0, password_len)
    return context


def fetch_ip_with_namespace_and_name(namespace: str, name: str) -> str:
    pods_info = kubectl_get_pods_info()
    pod_info_lines = pods_info.split("\n")
    ip_idx = pod_info_lines[0].find("IP")
    namespace_idx = pod_info_lines[0].find("NAMESPACE")
    ready_idx = pod_info_lines[0].find("READY")
    for line in pod_info_lines:
        if not line or len(line) <= namespace_idx:
            continue
        if line[namespace_idx:].split()[0].strip() == namespace and name in line:
            # Check if READY status is 1/1
            if ready_idx >= 0 and len(line) > ready_idx:
                ready_status = line[ready_idx:].split()[0].strip()
                if ready_status == "1/1":
                    return line[ip_idx:].split()[0].strip()
    return ""


def resolve_user_config_path(boot_args: list) -> str:
    """Resolve user_config.json path from deploy-style argv tokens."""
    file_key, dir_key = "user_config_path", "config_dir"
    _opt_value_key = {
        "--user_config_path": file_key,
        "--config": file_key,
        "--config_dir": dir_key,
        "--dir": dir_key,
    }
    parsed = {file_key: None, dir_key: None}
    it = iter(boot_args)
    for token in it:
        key = _opt_value_key.get(token)
        if key is None:
            continue
        try:
            value = next(it)
        except StopIteration:
            raise ValueError(f"Invalid input args: {token} requires a value") from None
        if value.startswith("--"):
            raise ValueError(f"Invalid input args: {token} requires a value")
        parsed[key] = value
    
    user_config_path = parsed[file_key]
    config_dir = parsed[dir_key]
    if user_config_path is None and config_dir is None:
        raise ValueError(
            "Missing required configuration. Please check the boot arguments."
        )

    resolved_user_config_path = None
    if config_dir:
        dir_user_config = os.path.join(config_dir, "user_config.json")
        if not user_config_path:
            if os.path.exists(dir_user_config):
                resolved_user_config_path = dir_user_config
                logging.info(
                    f"Using user_config.json from config_dir: {resolved_user_config_path}"
                )
            else:
                logging.error(f"user_config.json not found in {config_dir}")
                raise FileNotFoundError(
                    f"user_config.json not found in {config_dir}"
                )
        else:
            resolved_user_config_path = user_config_path
            logging.info(f"User config path: {resolved_user_config_path}")
    else:
        resolved_user_config_path = user_config_path

    logging.info(f"resolved user_config_path: {resolved_user_config_path}")
    return resolved_user_config_path


def fetch_user_config(user_config_path: str) -> dict:
    try:
        with open(user_config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, PermissionError) as e:
        logging.error(f"Failed to read user config file {user_config_path}: {e}")
        return None


def check_service_status(http_pool_manager, params: CheckParams) -> bool:
    try:
        ip = fetch_ip_with_namespace_and_name(params.namespace, "coordinator")
        if not ip:
            return False
        port = params.coordinator_port
        logging.info(f"Fetch server ip and port successfully: {ip}:{port}")
        http_prefix = "https" if params.with_cert else "http"
        response = http_pool_manager.request(
            "POST",
            f"{http_prefix}://{ip}:{port}/v1/completions",
            headers={"Content-Type": "application/json"},
            body=json.dumps({
                "model": params.model_name,
                "prompt": params.input_content,
                "temperature": 0,
                "max_tokens": 2,
                "stream": False,
            }).encode())
        if response.status >= 400:
            logging.info(f"Response from Coordinator failed, status is {response.status}, "
                         f"content is {response.data.decode()}")
            return False
    except Exception as e:
        logging.info(f"Failed to connect to coordinator because {e}")
        return False
    logging.info("MindIE MS service status is OK.")
    return True


def infer_with_retry(http_pool_manager, params: CheckParams, max_retries: int, interval_seconds: int):
    logging.info(f"Start to do virtual inference with metrics monitoring...")
    last_success_count, = get_metrics_values(http_pool_manager, params, TEST_METRIC_NAME)
    for i in range(max_retries):
        logging.info(f"Infer request for testing round {i+1}")
        if check_service_status(http_pool_manager, params):
            logging.info(f"Coordinator is ready, infer successfully!")
            return True
        cur_success_count, = get_metrics_values(http_pool_manager, params, TEST_METRIC_NAME)
        logging.info(f"current success count: {cur_success_count}, last success count: {last_success_count}")
        if cur_success_count > last_success_count:
            logging.info(f"Metrics {TEST_METRIC_NAME} increased, infer successfully!")
            return True
        if cur_success_count > 0:
            last_success_count = cur_success_count
        time.sleep(interval_seconds)
    return False


def is_mindie_service_detected(namespace: str) -> bool:
    pod_status_info_list = kubectl_get_pods_info().split('\n')
    namespace_idx = pod_status_info_list[0].find("NAMESPACE")
    for line in pod_status_info_list:
        if not line or len(line) <= namespace_idx:
            continue
        if line[namespace_idx:].split()[0].strip() == namespace:
            return True
    return False


def get_metrics_values(http_pool_manager, params: CheckParams, *metric_names) -> tuple:
    """
    Get multiple metric values from metrics API.
    Args:
        http_pool_manager: HTTP pool manager for making requests
        params: CheckParams object containing configuration
        *metric_names: Variable number of metric names to retrieve
    
    Returns:
        Tuple of metric values in the same order as metric_names.
        Returns tuple of -1 if metrics API call fails or metric not found.
    """
    # Fetch metrics from API
    try:
        coordinator_ip = fetch_ip_with_namespace_and_name(params.namespace, "coordinator")
        if not coordinator_ip:
            return tuple(-1 for _ in metric_names)
        logging.info(f"Fetch coordinator ip successfully: {coordinator_ip}")
        http_prefix = "https" if params.with_cert else "http"
        response = http_pool_manager.request(
            "GET",
            f"{http_prefix}://{coordinator_ip}:{params.coordinator_manage_port}/metrics"
        )
        if response.status >= 400:
            logging.info(f"Response from Coordinator metrics failed, status is {response.status}, "
                         f"content is {response.data.decode()}")
            return tuple(-1 for _ in metric_names)
        resp_text = response.data.decode(errors="ignore")
    except Exception as e:
        logging.info(f"Failed to connect to coordinator because {e}")
        return tuple(-1 for _ in metric_names)
    
    # Parse metric values from response text
    def find_metric_value(metric_name: str) -> int:
        try:
            for line in resp_text.split('\n'):
                stripped = line.strip()
                if metric_name in line and not (stripped.startswith('#')):
                    if metric_name == "request_success_total" and "length" not in line:
                        continue
                    metric_value = int(float(line.split(" ")[-1]))
                    logging.info(f"Successfully get metrics info from coordinator, {metric_name}: {metric_value}")
                    return metric_value
        except Exception as e:
            logging.warning(f"Metric value for {metric_name} in response is not found: {e}")
        return -1
    
    return tuple(find_metric_value(metric_name) for metric_name in metric_names)


def restart_service(namespace: str, boot_args):
    # graceful exit
    logging.info("Start to retain logs and restart service")
    subprocess.run(["bash", "show_log.sh"])
    if not os.path.exists(os.path.join(os.getcwd(), "delete.sh")):
        raise RuntimeError("delete.sh not found, couldn't exit gracefully!!!")
    subprocess.run(["bash", "delete.sh", namespace])
    while True:
        if not is_mindie_service_detected(namespace):
            logging.info("Delete mindie subprocess successfully!")
            break
        logging.info("Waiting for mindie subprocess to terminate!!!")
        time.sleep(10)

    # restart service
    deploy_res = subprocess.run(["python3", "deploy.py"] + boot_args)
    if is_mindie_service_detected(namespace):
        logging.info("Restart service successfully!")


def main():
    parser = argparse.ArgumentParser(description="MindIE RAS Starter")
    _, boot_args = parser.parse_known_args()
    user_config_path = resolve_user_config_path(boot_args)

    logging.info(f"Boot arguments: {boot_args}")

    probe_interval = 300
    do_inference_retries = 5
    do_inference_interval = 180
    input_content = "相对论的提出者是谁？" # probing prompt
    http_timeout = 60                    # urllib3 request timeout
    cert_context = load_cert()
    if cert_context:
        logging.info("Sending requests to Coordinator with ssl!")
        http_pool_manager = urllib3.PoolManager(
            ssl_context=cert_context,
            assert_hostname=False,
            timeout=http_timeout,
            retries=False
        )
    else:
        logging.info("Sending requests to Coordinator without ssl!")
        http_pool_manager = urllib3.PoolManager(
            cert_reqs="CERT_NONE",
            timeout=http_timeout,
            retries=False
        )
    user_config = fetch_user_config(user_config_path)
    from lib.utils import resolve_model_name
    prefill_section = user_config.get("motor_engine_prefill_config", {})
    model_name = resolve_model_name(prefill_section)
    
    try:
        coordinator_api_config = user_config["motor_coordinator_config"]["api_config"]
        metric_port = coordinator_api_config["coordinator_api_mgmt_port"]
        infer_port = coordinator_api_config["coordinator_api_infer_port"]
    except Exception as e:
        metric_port = 1026
        infer_port = 1025

    params = CheckParams(
        with_cert=(cert_context is not None),
        model_name=model_name,
        input_content=input_content,
        coordinator_port=str(infer_port),
        coordinator_manage_port=str(metric_port),
        namespace=user_config["motor_deploy_config"]["job_id"]
    )

    # Check if service is deployed
    while True:
        if is_mindie_service_detected(params.namespace):
            break
        logging.info(f"Waiting for service {params.namespace} to be deployed...")
        time.sleep(10)
    logging.info(f"Service {params.namespace} is deployed!!!")

    logging.info(
        f"Starting monitoring service with namespace: {params.namespace}, "
        f"model_name: {params.model_name}, coordinator_port: "
        f"{params.coordinator_port}, coordinator_manage_port: "
        f"{params.coordinator_manage_port}"
    )

    test_deploy = subprocess.run(["python3", "deploy.py"] + boot_args + ["--dry-run"])
    if test_deploy.returncode:
        logging.error(f"Deploy config failed! Please check boot_args: {boot_args}")
        sys.exit(1)


    max_retry_time = 10240
    while max_retry_time > 0:
        # Check if service is ready
        while True:
            if check_service_status(http_pool_manager, params):
                logging.info("MindIE MS Coordinator is ready!!!")
                break
            logging.info("MindIE MS Coordinator is not ready...")
            time.sleep(10)
        max_retry_time -= 1
        while True:
            time.sleep(10)
            logging.info(f"Start to monitor service, getting metrics with interval {probe_interval}s...")
            last_success_count, last_failed_count, last_running_count = get_metrics_values(
                http_pool_manager, params,
                "request_success_total",
                "request_failed_total",
                "num_requests_running"
            )

            time.sleep(probe_interval)
            
            logging.info(f"Start to examine service status...")
            # Check if metrics are available by trying to get one metric value
            test_metric, = get_metrics_values(http_pool_manager, params, TEST_METRIC_NAME)
            if test_metric == -1:
                logging.info(f"Metrics not available, doing virtual inference...")
                if not infer_with_retry(http_pool_manager, params, do_inference_retries, do_inference_interval):
                    logging.info(f"Virtual inference failed, restart service!")
                    break
                logging.info(f"Virtual inference succeeded, continue to monitor...")
            
            cur_success_count, cur_failed_count, cur_running_count = get_metrics_values(
                http_pool_manager, params,
                "request_success_total",
                "request_failed_total",
                "num_requests_running"
            )

            delta_success = (cur_success_count - last_success_count
                             if cur_success_count >= 0 and last_success_count >= 0 else -1)
            delta_failed = (cur_failed_count - last_failed_count
                             if cur_failed_count >= 0 and last_failed_count >= 0 else -1)

            if delta_success < 0:
                logging.info(f"Metrics values decreased, continue to monitor...")
                continue

            # Fault detection logic
            if delta_success > 0:
                logging.info(f"Success inference request count increased, continue to monitor...")
                continue
            elif delta_success == 0:
                if delta_failed > 0:
                    logging.info(
                        f"Doing virtual inference in failure increase state, "
                        f"start to retry {do_inference_retries} times with "
                        f"interval {do_inference_interval}s"
                    )
                    if infer_with_retry(http_pool_manager, params, do_inference_retries, do_inference_interval):
                        continue
                    logging.info(f"Virtual inference failed in failure increase state, restart service!")    
                    break
                elif delta_failed == 0 or cur_failed_count == -1:
                    if cur_running_count == 0:        # No requests, idle state
                        logging.info(
                            f"Doing virtual inference in idle state, "
                            f"start to retry {do_inference_retries} times with "
                            f"interval {do_inference_interval}s"
                        )
                        if infer_with_retry(http_pool_manager, params, do_inference_retries, do_inference_interval):
                            continue
                        logging.info(f"Virtual inference failed in idle state, restart service!")    
                        break
                    elif cur_running_count > 0:
                        # running state, e.g. long sequence request
                        logging.info(f"System is busy, continue to monitor...")
                        continue
                
        restart_service(params.namespace, boot_args)

if __name__ == '__main__':
    main()