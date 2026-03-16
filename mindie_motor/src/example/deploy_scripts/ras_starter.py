#!/usr/bin/env python3
# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

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
from utils.file_utils import safe_open

# Configure logging format and level
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Output to console
    ]
)


@dataclass
class CheckParams:
    with_cert: bool
    model_name: str
    input_content: str
    deployment_dir: str
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


def parse_boot_args(boot_args: list) -> dict:
    default_boot_args = {
        "--user_config_path": "./user_config.json",
        "--conf_path": "./conf",
        "--deploy_yaml_path": "./deployment"
    }

    def match_boot_arg(arg: str) -> str:
        candidate_list = []
        for key in default_boot_args.keys():
            if arg == key:
                return arg
            if key.startswith(arg):
                candidate_list.append(key)
        if not candidate_list:
            return ""
        if len(candidate_list) > 1:
            raise ValueError(f"boot_arg {arg} mismatch, candidates are: {candidate_list}")
        logging.info(f"boot_arg {arg} match {candidate_list[0]}")
        return candidate_list[0]

    iterator = enumerate(boot_args)
    for _, cur_arg in iterator:
        match_arg = match_boot_arg(cur_arg)
        if match_arg:
            try:
                _, next_value = next(iterator)
                default_boot_args[match_arg] = next_value
            except StopIteration:
                raise ValueError(f"Invalid input args, please check boot arg {cur_arg}!") from None
        elif cur_arg.startswith("--"):
            # Unknown parameter, throw error
            valid_args = ", ".join(default_boot_args.keys())
            raise ValueError(
                f"Unknown boot argument: {cur_arg}. "
                f"Please check your arguments. Valid boot arguments are: {valid_args}"
            )

    logging.info(f"boot args: {default_boot_args}")
    return default_boot_args


def fetch_config(config_path: str) -> dict:
    with safe_open(config_path, permission_mode=0o640) as f:
        return json.load(f)


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
    return True


def infer_with_retry(http_pool_manager, params: CheckParams, max_retries: int = 5, interval_seconds: int = 180):
    for i in range(max_retries):
        logging.info(f"retry round {i+1}")
        if check_service_status(http_pool_manager, params):
            return True
        time.sleep(interval_seconds)
    return False


def is_mindie_service_detected(namespace: str) -> bool:
    pod_status_info_list = kubectl_get_pods_info().split('\n')
    namespace_idx = pod_status_info_list[0].find("NAMESPACE")
    for line in pod_status_info_list:
        if not line or len(line) <= namespace_idx:
            continue
        if line[namespace_idx:].split()[0].strip() == namespace:
            if (f"{namespace}-controller" in line or f"{namespace}-coordinator" in line or
                    "mindie-server" in line):
                return True
    return False


def is_metrics_mode_enabled() -> bool:
    # Check if metrics is enabled
    script_dir = os.path.dirname(os.path.abspath(__file__))
    boot_sh_path = os.path.join(script_dir, "boot_helper", "boot.sh")
    if os.path.isfile(boot_sh_path):
        with open(boot_sh_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if (line.strip().startswith("export") and "MIES_SERVICE_MONITOR_MODE" in line and
                        line.split("=")[1].strip() == "1"):
                    logging.info("Metrics mode is enabled!!!")
                    return True
    logging.info("Metrics mode is disabled!!!")
    return False


def restart_service(namespace: str, boot_args):
    # graceful exit
    logging.info("Start to retain logs and restart service")
    subprocess.run(["bash", "collect_pd_cluster_logs.sh"])
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
    deploy_ac_job_res = subprocess.run(["python3", "deploy_ac_job.py"] + boot_args)
    if is_mindie_service_detected(namespace):
        logging.info(f"Restart service successfully!")


def get_metrics_from_metrics_api(http_pool_manager, params: CheckParams) -> str:
    try:
        coordinator_ip = fetch_ip_with_namespace_and_name(params.namespace, "coordinator")
        if not coordinator_ip:
            return ""
        logging.info(f"Fetch coordinator ip successfully: {coordinator_ip}")
        http_prefix = "https" if params.with_cert else "http"
        response = http_pool_manager.request(
            "GET",
            f"{http_prefix}://{coordinator_ip}:{params.coordinator_manage_port}/metrics"
        )
        if response.status >= 400:
            logging.info(f"Response from Coordinator metrics failed, status is {response.status}, "
                             f"content is {response.data.decode()}")
            return ""
        resp_text = response.data.decode(errors="ignore")
        return resp_text
    except Exception as e:
        logging.info(f"Failed to connect to coordinator because {e}")
    return ""


def find_metric_values(resp_text: str, metric_name: str) -> int:
    try:
        for line in resp_text.split('\n'):
            stripped = line.strip()
            if metric_name in line and not (stripped.startswith('#')):
                metric_value = int(float(line.split(" ")[-1]))
                logging.info(f"Successfully get metrics info from coordinator, {metric_name}: {metric_value}")
                return metric_value
    except Exception as e:
        logging.warning(f"Metric value for {metric_name} in response is not found: {e}")
    return -1


def main():
    parser = argparse.ArgumentParser(description="MindIE RAS Starter")
    _, boot_args = parser.parse_known_args()
    logging.info(f"Boot arguments: {boot_args}")
    boot_config = parse_boot_args(boot_args)

    probe_interval = 300
    do_inference_retries = 5
    do_inference_interval = 180
    input_content = "相对论的提出者是谁？"    # Probe prompt
    max_unavailable_time = 1200             # Maximum service unavailable time
    http_timeout = 60                       # urllib3 request timeout
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
    user_config = fetch_config(boot_config["--user_config_path"])
    model_name = \
        user_config["mindie_server_prefill_config"]["BackendConfig"]["ModelDeployConfig"]["ModelConfig"][0]["modelName"]
    coordinator_http_config = user_config["mindie_ms_coordinator_config"]["http_config"]
    try:
        ms_coordinator_config = fetch_config(os.path.join(boot_config["--conf_path"], "ms_coordinator.json"))
        metric_port = ms_coordinator_config["http_config"]["external_port"]
    except Exception as e:
        metric_port = coordinator_http_config["manage_port"]

    params = CheckParams(
        with_cert=(cert_context is not None),
        model_name=model_name,
        input_content=input_content,
        deployment_dir=boot_config["--deploy_yaml_path"],
        coordinator_port=coordinator_http_config["predict_port"],
        coordinator_manage_port=metric_port,
        namespace=user_config["deploy_config"]["job_id"]
    )

    # Check if service is deployed
    while True:
        if is_mindie_service_detected(params.namespace):
            break
        logging.info(f"Waiting for service {params.namespace} to be deployed...")
        time.sleep(10)
    logging.info(f"Service {params.namespace} is deployed!!!")
    
    # Check if metrics is enabled
    if not is_metrics_mode_enabled():
        raise RuntimeError(f"Metrics mode is disabled, please check and set MIES_SERVICE_MONITOR_MODE=1.")

    logging.info(f"Start monitoring service with namespace: {params.namespace}, model_name: {params.model_name}, "
                 f"coordinator_port: {params.coordinator_port}, "
                 f"coordinator_manage_port: {params.coordinator_manage_port}")


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
            resp_text = get_metrics_from_metrics_api(http_pool_manager, params)
            last_success_count = find_metric_values(resp_text, "request_success_total")
            last_failed_count = find_metric_values(resp_text, "request_failed_total")
            last_running_count = find_metric_values(resp_text, "num_requests_running")

            time.sleep(probe_interval)
            
            logging.info(f"Start to examine service status...")
            resp_text = get_metrics_from_metrics_api(http_pool_manager, params)
            cur_success_count = find_metric_values(resp_text, "request_success_total")
            cur_failed_count = find_metric_values(resp_text, "request_failed_total")
            cur_running_count = find_metric_values(resp_text, "num_requests_running")
            
            delta_success = (cur_success_count - last_success_count
                             if cur_success_count >= 0 and last_success_count >= 0 else -1)
            delta_failed = (cur_failed_count - last_failed_count
                            if cur_failed_count >= 0 and last_failed_count >= 0 else -1)

            if delta_success < 0 or delta_failed < 0:
                logging.info(f"Metrics values decreased, continue to monitor...")
                continue

            # Fault detection logic
            if delta_success > 0:
                logging.info(f"Success inference request count increased, continue to monitor...")
                continue
            elif delta_success == 0:
                if delta_failed > 0:               
                    logging.info(f"Doing virtual inference in failure increase state, "
                                 f"start to retry {do_inference_retries} times "
                                 f"with interval {do_inference_interval}s")
                    if infer_with_retry(http_pool_manager, params, do_inference_retries, do_inference_interval):
                        continue
                    logging.info(f"Virtual inference failed in failure increase state, restart service!")    
                    break
                elif delta_failed == 0:
                    if cur_running_count == 0:       # No request, idle state
                        logging.info(f"Doing virtual inference in idle state, "
                                     f"start to retry {do_inference_retries} times "
                                     f"with interval {do_inference_interval}s")
                        if infer_with_retry(http_pool_manager, params, do_inference_retries, do_inference_interval):
                            continue
                        logging.info(f"Virtual inference failed in idle state, restart service!")    
                        break
                    elif cur_running_count > 0:       # Running state, e.g. long sequence inference
                        logging.info(f"Doing virtual inference in running state, "
                                     f"start to retry {do_inference_retries} times "
                                     f"with interval {do_inference_interval}s")
                        if infer_with_retry(http_pool_manager, params, do_inference_retries, do_inference_interval):
                            continue
                        logging.info(f"Virtual inference failed in running state, restart service!")
                        break
                
        restart_service(params.namespace, boot_args)

if __name__ == '__main__':
    main()
