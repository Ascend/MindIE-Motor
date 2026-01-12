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

# 配置日志格式和级别
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # 同时输出到控制台
    ]
)


@dataclass
class CheckParams:
    with_cert: bool
    enable_acc_check: bool
    model_name: str
    input_content: str
    golden_result: str
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


def fetch_ip_with_name(name: str) -> str:
    pods_info = kubectl_get_pods_info()
    pod_info_lines = pods_info.split("\n")
    ip_idx = pod_info_lines[0].find("IP")
    for line in pod_info_lines:
        if name in line:
            return line[ip_idx:].split(" ")[0].strip()
    return ""


def fetch_server_ip_and_port(params: CheckParams) -> str:
    def fetch_port(file: str, port_name: str):
        with safe_open(file, permission_mode=0o640) as f:
            for line in f:
                line = line.strip()
                if line.startswith(port_name):
                    return line.split(port_name)[-1].strip()
        return ""

    haproxy_ip = fetch_ip_with_name("haproxy")
    if haproxy_ip:
        return f"{haproxy_ip}:{fetch_port(os.path.join(params.deployment_dir, 'haproxy_init.yaml'), 'port:')}"
    return f"{fetch_ip_with_name(f'{params.namespace}-coordinator')}:{params.coordinator_port}"


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

    for idx in range(len(boot_args)):
        cur_arg = boot_args[idx]
        match_arg = match_boot_arg(cur_arg)
        if match_arg:
            if idx + 1 >= len(boot_args):
                raise ValueError(f"Invalid input args, please check boot arg {cur_arg}!")
            default_boot_args[match_arg] = boot_args[idx + 1]
    logging.info(f"boot args: {default_boot_args}")
    return default_boot_args


def fetch_config(config_path: str) -> dict:
    with safe_open(config_path, permission_mode=0o640) as f:
        return json.load(f)


def check_service_status(http_pool_manager, params: CheckParams) -> bool:
    try:
        ip_and_port = fetch_server_ip_and_port(params)
        logging.info(f"Fetch server ip and port successfully: {ip_and_port}")
        http_prefix = "https" if params.with_cert else "http"
        response = http_pool_manager.request(
            "POST",
            f"{http_prefix}://{ip_and_port}/v1/completions",
            headers={"Content-Type": "application/json"},
            body=json.dumps({
                "model": params.model_name,
                "prompt": params.input_content,
                "temperature": 0,
                "max_tokens": 10,
                "stream": False,
            }).encode())
        if response.status >= 400:
            logging.info(f"Response from Coordinator failed, status is {response.status}, "
                         f"content is {response.data.decode()}")
            return False
        resp_text = response.data.decode(errors="ignore")
        if params.enable_acc_check and params.golden_result not in resp_text:
            logging.info(f"Accuracy error: golden_result '{params.golden_result}' not in response: {resp_text}")
            return False
    except Exception as e:
        logging.info(f"Failed to connect to coordinator because {e}")
        return False
    logging.info("MindIE MS Coordinator is ready!!!")
    return True


def get_request_token_sum_from_metrics(http_pool_manager, params: CheckParams) -> int:
    try:
        coordinator_ip = fetch_ip_with_name(f"{params.namespace}-coordinator")
        logging.info(f"Fetch coordinator ip successfully: {coordinator_ip}")
        http_prefix = "https" if params.with_cert else "http"
        response = http_pool_manager.request(
            "GET",
            f"{http_prefix}://{coordinator_ip}:{params.coordinator_manage_port}/metrics"
        )
        if response.status >= 400:
            logging.info(f"Response from Coordinator failed, status is {response.status}, "
                         f"content is {response.data.decode()}")
            return -1
        resp_text = response.data.decode(errors="ignore")
        for line in resp_text.split('\n'):
            if "request_generation_tokens_sum" in line:
                request_tokens_sum = int(line.split(" ")[-1])
                logging.info(f"Successfully get metrics info from coordinator, request_generation_tokens_sum: "
                             f"{request_tokens_sum}")
                return request_tokens_sum
    except Exception as e:
        logging.info(f"Failed to connect to coordinator because {e}")
    return -1


def is_mindie_service_detected(params: CheckParams) -> bool:
    pod_status_info_list = kubectl_get_pods_info().split('\n')
    namespace_idx = pod_status_info_list[0].find("NAMESPACE")
    for line in pod_status_info_list:
        if line[namespace_idx:].split(" ")[0].strip() == params.namespace:
            if (f"{params.namespace}-controller" in line or f"{params.namespace}-coordinator" in line or
                    "mindie-server" in line):
                return True
    return False


def graceful_exit(params: CheckParams):
    logging.info("Start to retain logs and restart service")
    subprocess.run(["bash", "collect_pd_cluster_logs.sh"])
    if not os.path.exists(os.path.join(os.getcwd(), "delete.sh")):
        raise RuntimeError("delete.sh not found, couldn't exit gracefully!!!")
    subprocess.run(["bash", "delete.sh", params.namespace])
    while True:
        if not is_mindie_service_detected(params):
            logging.info("Delete mindie subprocess successfully!")
            return
        logging.info("Waiting for mindie subprocess to terminate!!!")
        time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description="MindIE RAS Starter")
    parser.add_argument('--attach', action='store_true', help='是否是附加启动，如果是则不会删除原有服务')
    parser.add_argument('--enable-acc-check', action='store_true', help='是否启动精度检测')
    args, boot_args = parser.parse_known_args()

    logging.info(f"启动参数: {vars(args)}, {boot_args}")

    is_attach = args.attach
    enable_acc_check = args.enable_acc_check
    probe_interval = 10
    input_content = "不要思考直接回答，相对论的发明者是谁？" # 精度探测问题
    golden_result = "爱因斯坦" # 精度探测结果
    max_unavailable_time = 1200 # 最大服务不可用时长，默认1200秒
    http_timeout = 60 # urllib3请求超时时间，默认60秒
    boot_config = parse_boot_args(boot_args)
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
        enable_acc_check=enable_acc_check,
        model_name=model_name,
        input_content=input_content,
        golden_result=golden_result,
        deployment_dir=boot_config["--deploy_yaml_path"],
        coordinator_port=coordinator_http_config["predict_port"],
        coordinator_manage_port=metric_port,
        namespace=user_config["deploy_config"]["job_id"]
    )
    logging.info(f"Starting service with namespace: {params.namespace}, model_name: {model_name}, coordinator_port: "
                 f"{params.coordinator_port}, coordinator_manage_port: {params.coordinator_manage_port}")
    # Only start service in start up mode
    if not is_attach and is_mindie_service_detected(params):
        graceful_exit(params)

    max_retry_time = 10240
    while max_retry_time > 0:
        max_retry_time -= 1
        if not is_mindie_service_detected(params):
            deploy_ac_job_res = subprocess.run(["python3", "deploy_ac_job.py"] + boot_args)
            if deploy_ac_job_res.returncode:
                logging.error(f"Start service failed! please check boot_args: {boot_args}")
                exit(-1)
            else:
                logging.info(f"Start service successfully!")
        last_generation_token_sum_num = 0
        last_success_timepoint = time.time()
        while True:
            time.sleep(probe_interval)
            cur_success_num = get_request_token_sum_from_metrics(http_pool_manager, params)
            if cur_success_num > last_generation_token_sum_num:
                last_generation_token_sum_num = cur_success_num
                last_success_timepoint = time.time()
                continue
            if cur_success_num > 0:
                last_generation_token_sum_num = cur_success_num
            if check_service_status(http_pool_manager, params):
                last_success_timepoint = time.time()
                continue
            if time.time() - last_success_timepoint > max_unavailable_time:
                logging.info(f"Service unavailable time is over {max_unavailable_time}!")
                break
        graceful_exit(params)


if __name__ == '__main__':
    main()