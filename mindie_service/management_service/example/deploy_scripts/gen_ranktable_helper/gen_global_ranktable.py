# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import time
import logging
import os
import argparse

P_DEPLOY_SERVER = 'p_deploy_server'

D_DEPLOY_SERVER = 'd_deploy_server'

SERVER_LIST = "server_list"

RINGS_CONFIG_D = "rings-config-mindie-server-d"

RINGS_CONFIG_P = "rings-config-mindie-server-p"
SUPER_POD_LIST = 'super_pod_list'

logging.basicConfig(
    level=logging.INFO,
    format=(
        f'[%(asctime)s:%(msecs)04d+08:00] '
        f'[%(process)d] [{os.getppid()}] '
        f'[%(levelname)s] [%(filename)s:%(lineno)d] : %(message)s'
    ),
    datefmt='%Y-%m-%d %H:%M:%S'
)

SERVER_LIST_KEY = 'server_list'
SERVER_ID_KEY = 'server_id'
DEVICE_KEY = 'device'
SERVER_GROUP_LIST_KEY = 'server_group_list'
DEFAULT_MINDIE_NAMESPACE = "mindie"

TIMEOUT_SECONDS = 120


def wait_hccl_ranktable_finish(configmap_name, namespace="mindie"):
    hccl_ranktable_str = output_from_kubectl(
        "kubectl get configmap %s -n %s -o jsonpath='{.data}'" % (configmap_name, namespace))
    try:
        hccl_ranktable_obj = json.loads(json.loads(hccl_ranktable_str)['hccl.json'])
        hardware_type = None
        if 'hardware_type' in json.loads(hccl_ranktable_str):
            hardware_type = json.loads(hccl_ranktable_str)['hardware_type']
        if hccl_ranktable_obj["status"] == 'completed':
            return True, hccl_ranktable_obj, hardware_type
        else:
            return False, None, None
    except Exception:
        logging.error("failed to get ranktable configmap")
        return False, None, None


def output_from_kubectl(command, print_log=True):
    import subprocess
    import shlex

    cmd_args = shlex.split(command)
    child = subprocess.Popen(cmd_args, stderr=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True)

    stdout, stderr = child.communicate(timeout=60)

    if print_log:
        logging.info(f"Execute command: {command} \n The command result is: \n {stdout}, {stderr}")
    return stdout


def generate_server_ranktable(namespace):
    deployment_label = output_from_kubectl(
        "kubectl get deployment mindie-server -n " + namespace + " -o jsonpath='{.metadata.labels}'")
    try:
        deployment_label_json_obj = json.loads(deployment_label)
        if deployment_label_json_obj["ring-controller.atlas"] == "ascend-310p":
            replicas = output_from_kubectl(
                "kubectl get deployment mindie-server -n " + namespace + " -o jsonpath='{.spec.replicas}'")
            while True:
                pod_ips = output_from_kubectl(
                    "kubectl get pods -l ring-controller.atlas=ascend-310p,"
                    "app=mindie-server -o jsonpath='{.items[*].status.podIP}' -n " + namespace).split(" ")
                if len(pod_ips) != int(replicas) or not pod_ips or not pod_ips[0]:
                    logging.info("Getting pod ips with label ring-controller.atlas=ascend-310p")
                    time.sleep(2)
                    continue
                else:
                    break
            hccl_rank_table = {}

            hccl_rank_table['version'] = "1.0"
            hccl_rank_table[SERVER_LIST_KEY] = []
            hccl_rank_table['server_count'] = str(int(replicas))
            hccl_rank_table['status'] = "completed"
            for i in range(int(replicas)):
                hccl_rank_table[SERVER_LIST_KEY].append({"server_id": pod_ips[i], "container_ip": pod_ips[i]})
            patch_data = f'{{"data": {{"hccl.json": {json.dumps(json.dumps(hccl_rank_table, indent=4))}}}}}'
            prefill_str = "kubectl patch configmap rings-config-mindie-server -n " + namespace + " --type merge -p "
            ret = output_from_kubectl(f"{prefill_str} '{patch_data}'", False)
            if ret is None:
                logging.info('return is None')
    except Exception:
        logging.warning("failed to get deployment info, please wait...")
        return -1
    return 0


def add_mindie_server_group(heter, deployment_name, group_id, server_num=1, namespace="mindie"):
    ret = generate_server_ranktable(namespace)
    if ret != 0:
        logging.info('generate_server_ranktable not ok, please wait for generating...')
    start_time = time.time()
    while True:
        elapsed_time = time.time() - start_time
        if elapsed_time > TIMEOUT_SECONDS:
            return None
        finish, hccl_ranktable, hardware_type = wait_hccl_ranktable_finish(deployment_name, namespace)
        if finish:
            break
        else:
            time.sleep(2)
            continue

    server_group = {}
    server_group['group_id'] = str(group_id)
    if int(group_id) >= 2 and int(server_num) >= 0:
        server_group['deploy_server'] = str(server_num)
    server_group['server_count'] = str(len(hccl_ranktable[SERVER_LIST_KEY]))
    server_group[SERVER_LIST_KEY] = []
    if SUPER_POD_LIST in hccl_ranktable.keys():
        server_group[SUPER_POD_LIST] = hccl_ranktable[SUPER_POD_LIST]
    for item in hccl_ranktable[SERVER_LIST_KEY]:
        server = {}
        server[SERVER_ID_KEY] = item[SERVER_ID_KEY]
        server['server_ip'] = item['container_ip']
        if DEVICE_KEY in item:
            server[DEVICE_KEY] = item[DEVICE_KEY]
            if hardware_type:
                server['hardware_type'] = hardware_type
            for i in range(len(server[DEVICE_KEY])):
                server[DEVICE_KEY][i]["device_logical_id"] = str(i)
        server_group[SERVER_LIST_KEY].append(server)
    if heter:
        while True:
            finish, hccl_ranktable_heter, hardware_type = wait_hccl_ranktable_finish(
                "rings-config-mindie-server-heterogeneous", namespace)
            if finish:
                break
            else:
                time.sleep(2)
                continue
        server_group['server_count'] += len(hccl_ranktable_heter[SERVER_LIST_KEY])
        for item in hccl_ranktable_heter[SERVER_LIST_KEY]:
            server = {}
            server[SERVER_ID_KEY] = item[SERVER_ID_KEY]
            server['server_ip'] = item['container_ip']
            if DEVICE_KEY in item:
                server[DEVICE_KEY] = item[DEVICE_KEY]
                if hardware_type:
                    server['hardware_type'] = hardware_type
                for i in range(len(server[DEVICE_KEY])):
                    server[DEVICE_KEY][i]["device_logical_id"] = str(i)
                server_group[SERVER_LIST_KEY].append(server)
    return server_group


def add_mindie_ms_group(namespace: str, module_name: str, backup_enable: bool = False) -> dict:
    while True:
        pod_ip = output_from_kubectl("kubectl get pods -l app=mindie-ms-" + module_name + " -n " + namespace +
                                     " -o jsonpath='{.items[*].status.podIP}'")
        if pod_ip:
            pod_ip_list = pod_ip.strip().split(" ")
            if not backup_enable or len(pod_ip_list) > 1:
                break
        logging.info(f"Waiting for all MindIE MS {module_name} to initialize!")
        time.sleep(1)
    return {
        'group_id': '0' if module_name == 'coordinator' else '1',
        'server_count': str(len(pod_ip_list)),
        SERVER_LIST_KEY: [{"server_ip": ip} for ip in pod_ip_list]
    }


def ac_job_state_log(namespace):
    ac_job_state_data = output_from_kubectl(f"kubectl get acjob -n {namespace}", False)
    if ac_job_state_data == "":
        logging.error(f"Failed to get acjob info, namespace: {namespace}")
        return
        
    ac_job_state_data = ac_job_state_data.strip().split('\n')[1:]

    ac_job_state_log_lines = ["The status of each instance:"]
    is_depoly_success = True
    for line in ac_job_state_data:
        parts = line.split()
        instance_name = parts[0]
        state = parts[1]
        if state == "Running":
            state = "Ready"
        else:
            state = "Not Ready"
            is_depoly_success = False

        ac_job_state_log_lines.append(f"Namespace: {namespace}, Instance Name: {instance_name}, State: {state};")

    depoly_status_msg = "Deploy success!" if is_depoly_success else "Deploy failed!"
    ac_job_state_log_lines.insert(0, depoly_status_msg)
    ac_job_state_log_message = "\n".join(ac_job_state_log_lines)
    logging.info(ac_job_state_log_message)


def generate_global_ranktable(heter=False, p_node_num=1, d_node_num=1, is_multi_node=False, ext=None):
    global_rank_table = {'version': "1.0", SERVER_GROUP_LIST_KEY: []}
    group_start_num = 2
    if ext is not None and "namespace" in ext:
        namespace = ext["namespace"]
    else:
        namespace = DEFAULT_MINDIE_NAMESPACE

    if is_multi_node:
        for i in range(p_node_num):
            server_group = add_mindie_server_group(heter,
                RINGS_CONFIG_P + str(i), group_start_num, ext[P_DEPLOY_SERVER], namespace)
            if server_group is not None:
                global_rank_table[SERVER_GROUP_LIST_KEY].append(server_group)
            group_start_num = group_start_num + 1
        for i in range(d_node_num):
            server_group = add_mindie_server_group(heter,
                RINGS_CONFIG_D + str(i), group_start_num, ext[D_DEPLOY_SERVER], namespace)
            if server_group is not None:
                global_rank_table[SERVER_GROUP_LIST_KEY].append(server_group)
            group_start_num = group_start_num + 1
    else:
        if ext is not None and ext['is_acjob'] == 'true':
            group_map = build_group_map(d_node_num, ext, group_start_num, heter, p_node_num)
            group_map["server_count"] = str(len(group_map[SERVER_LIST]))
            global_rank_table[SERVER_GROUP_LIST_KEY].append(group_map)
        else:
            server_group = add_mindie_server_group(heter,
                "rings-config-mindie-server", group_start_num, -1, namespace)
            if server_group is not None:
                global_rank_table[SERVER_GROUP_LIST_KEY].append(server_group)
    controller_backup_enable = ext.get("controller_backup_enable", False) if ext else False
    coordinator_enable = ext.get("coordinator_backup_enable", False) if ext else False
    global_rank_table[SERVER_GROUP_LIST_KEY].append(
        add_mindie_ms_group(namespace, "controller", controller_backup_enable))
    global_rank_table[SERVER_GROUP_LIST_KEY].append(
        add_mindie_ms_group(namespace, "coordinator", coordinator_enable))
    global_rank_table['status'] = "completed"
    logging.info("global ranktable:\n")
    logging.info(json.dumps(global_rank_table, indent=4))
    patch_data = f'{{"data":{{"global_ranktable.json":{json.dumps(json.dumps(global_rank_table))}}}}}'
    prefill_str = "kubectl patch configmap global-ranktable -n " + namespace + " --type merge -p "
    ret = output_from_kubectl(f"{prefill_str} '{patch_data}'", False)
    if ret is None:
        logging.info('return is None')
    if is_multi_node:
        ac_job_state_log(namespace)
    return global_rank_table


def build_group_map(d_node_num, ext, group_start_num, heter, p_node_num):
    group_map = {}
    for i in range(p_node_num):
        if not group_map:
            group_map = add_mindie_server_group(heter, RINGS_CONFIG_P + str(i), group_start_num,
                                                ext[P_DEPLOY_SERVER])
        else:
            server_list = add_mindie_server_group(heter, RINGS_CONFIG_P + str(i),
                                                  group_start_num, ext[P_DEPLOY_SERVER])[SERVER_LIST]
            if SERVER_LIST in group_map:
                group_map[SERVER_LIST].extend(server_list)
            else:
                group_map[SERVER_LIST] = server_list
    for i in range(d_node_num):
        if not group_map:
            group_map = add_mindie_server_group(heter, RINGS_CONFIG_D + str(i), group_start_num,
                                                ext[D_DEPLOY_SERVER])
        else:
            server_list = add_mindie_server_group(heter, RINGS_CONFIG_D + str(i),
                                                  group_start_num, ext[D_DEPLOY_SERVER])[SERVER_LIST]
            if SERVER_LIST in group_map:
                group_map[SERVER_LIST].extend(server_list)
            else:
                group_map[SERVER_LIST] = server_list
    return group_map


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--heter', type=bool, default=False, help='Enable or disable heterogeneous deployment')
    parser.add_argument('--is_multi_node', type=bool, default=False, help='Enable or disable multi node feature')
    parser.add_argument('--p_node_num', type=int, default=1, help='p node num')
    parser.add_argument('--d_node_num', type=int, default=1, help='d node num')
    args = parser.parse_args()
    ret_val = generate_global_ranktable(args.heter)
    if ret_val is None:
        logging.info('return is None')
