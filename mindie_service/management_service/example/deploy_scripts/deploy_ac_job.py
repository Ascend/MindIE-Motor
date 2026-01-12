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

import argparse
import os
import json
import subprocess
import logging
import sys
import shlex
import copy
from datetime import datetime
from zoneinfo import ZoneInfo
from ruamel.yaml import YAML, scalarstring
import yaml as ym
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
from ruamel.yaml.comments import CommentedMap, CommentedSeq

sys.path.append(os.getcwd())

from gen_ranktable_helper.gen_global_ranktable import generate_global_ranktable
from utils.file_utils import safe_open
from utils.validate_config import validate_user_config
from utils.validate_utils import validate_identifier, validate_path_part, validate_command_part

# 配置日志格式和级别
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # 同时输出到控制台
    ]
)

# 创建一个YAML处理器，使用RoundTripLoader和RoundTripDumper
yaml = YAML(typ='rt')  # RoundTrip模式
yaml.preserve_quotes = True  # 保留原有引号
MAX_P_INSTANCES_NUM = 1024
MAX_D_INSTANCES_NUM = 1024
MAX_SINGER_P_INSTANCES_NODE_NUM = 1024
MAX_SINGER_D_INSTANCES_NODE_NUM = 1024
WORKER_REPLICAS_OFFSET = 1
JOB_ID = "jobID"
LABELS = "labels"
METADATA = "metadata"
SPEC = "spec"
TEMPLATE = "template"
CONTAINERS = "containers"
IMAGE = "image"
NAME = "name"
MASTER = "Master"
WORKER = "Worker"
REPLICA_SPECS = "replicaSpecs"
RUN_POLICY = "runPolicy"
SCHEDULING_POLICY = "schedulingPolicy"
REPLICAS = "replicas"
MIN_AVAILABLE = "minAvailable"
P_INSTANCES_NUM = "p_instances_num"
D_INSTANCES_NUM = "d_instances_num"
CONFIG_JOB_ID = "job_id"
IMAGE_NAME = "image_name"
SINGER_P_INSTANCES_NUM = "single_p_instance_pod_num"
SINGER_D_INSTANCES_NUM = "single_d_instance_pod_num"
SERVER_GROUP_LIST = "server_group_list"
SERVER_ID = "server_id"
DEVICE = "device"
SERVER_COUNT = "server_count"
SERVER_IP = "server_ip"
GROUP_ID = "group_id"
SERVER_LIST = 'server_list'
GROUP_LIST = "group_list"
HARDWARE_TYPE = 'hardware_type'
CONTAINER_IP = 'container_ip'
DEVICE_LOGICAL_ID = "device_logical_id"
PATH = "path"
HOST_PATH = "hostPath"
MINDIE_HOST_LOG_PATH = "mindie_host_log_path"
MODEL_NAME = "model_name"
MODEL_ID = "model_id"
CONTAINER_LOG_PATH = "mindie_container_log_path"
VALUE = "value"
VOLUME_MOUNTS = "volumeMounts"
VOLUMES = "volumes"
ENV = "env"
MOUNT_PATH = "mountPath"
MINDIE_SERVER = "mindie-server-"
ASCEND_910_CONFIG = "ascend-910-config"
CONFIG_MAP = "configMap"
CONFIG = "-config"
RINGS_CONFIG_MINDIE_SERVER = "rings-config-mindie-server-"
ASCEND_910_NPU_NUM = "huawei.com/Ascend910"
POD_NPU_NUM = "_pod_npu_num"
REQUESTS = "requests"
RESOURCES = "resources"
LIMITS = "limits"
CONFIG_MODEL_NAME = ""
YAML = ".yaml"
DEPLOY_SERVER_NUM = "grt-group/deploy-server"
INIT_PORT = 10000
MIND_CLUSTER_SCALING_RULE = "mind-cluster/scaling-rule"
MIND_CLUSTER_GROUP_NAME = "mind-cluster/group-name"
PRIORITY_CLASS = "priorityClass"
LOW_PRIORITY = "low-priority"
HIGH_PRIORITY = "high-priority"
P_DEPLOY_SERVER = 'p_deploy_server'
D_DEPLOY_SERVER = 'd_deploy_server'
WEIGHT_MOUNT_PATH = "weight_mount_path"
NODE_SELECTOR = "nodeSelector"
MINDIE_ROLE = "mindie-role"
NAME_FLAG = " -n "
SERVER_CONFIG = "ServerConfig"
NAMESPACE = "namespace"
TP = "tp"
DP = "dp"
SP = "sp"
CP = "cp"
DIST_DP_SERVER_ENABLED = "distDPServerEnabled"
NAME_MOUNT = 'mount'
NAME_KEY = 'name'
MODEL_CONFIG = "ModelConfig"
MODEL_DEPLOY_CONFIG = "ModelDeployConfig"
BACKEND_CONFIG = "BackendConfig"
FROM_FILE_CONFIG_JSON_C = "-config --from-file=config.json="
APPLY_F_D = "kubectl apply -f "
TLS_ENABLE = "tls_enable"
TLS_CONFIG = "tls_config"
TLS_KEY = "tls_key"
TLS_PASSWD = "tls_passwd"
TLS_CRL = "tls_crl"
MANAGEMENT_TLS_ITEMS = "management_tls_items"
MANAGEMENT_TLS_ENABLE = "management_tls_enable"
CLUSTER_TLS_ENABLE = "cluster_tls_enable"
CCAE_TLS_ENABLE = "ccae_tls_enable"
ETCD_SERVER_TLS_ENABLE = "etcd_server_tls_enable"
INFER_TLS_ENABLE = "infer_tls_enable"
TLS_CERT = "tls_cert"
MANAGEMENT = "management"
TLS_PATH_SEPARATOR = "/"
CA_CERT = "ca_cert"
CCAE_TLS_ITEMS = "ccae_tls_items"
CONTROLLER_BACKUP_CFG = "controller_backup_cfg"
CONTROLLER_BACKUP_SW = "function_sw"
POD_NUM_ZERO = 0
DEPLOY_CONFIG = "deploy_config"
DELETE_F_D = "kubectl delete -f "
ELASTIC_P_CHANGE = "p_instances_scale_num"
ELASTIC_D_CHANGE = "d_instances_scale_num"
MIN_ELASTIC_NUM = -96
MAX_ELASTIC_NUM = 96
INSTANCE_NUM_ZERO = 0
ANNOTATIONS = "annotations"
SP_BLOCK = "sp-block"
P_POD_NPU_NUM = "p_pod_npu_num"
D_POD_NPU_NUM = "d_pod_npu_num"
BOOT_SHELL_PATH = "./boot_helper/boot.sh"
ETCD_TLS_ITEMS = "etcd_server_tls_items"
MAX_ITER_TIMES = "maxIterTimes"
PREFILL_DEPLOY = "2"
DECODE_DEPLOY = "1"
POD_RESCHEDULING = "pod-rescheduling"
FAULT_RECOVERY_FUNC = "fault_recovery_func_dict"
LINGQU_LINK = "lingqu_link"
context = dict()


def process_yaml(data):
    """
    遍历YAML数据，处理以0开头的数字，并修改指定内容。
    """
    if isinstance(data, dict):
        for key, value in data.items():
            # 处理以0开头的数字
            if isinstance(value, str) and value.startswith('0') and value.isdigit():
                # 将其转换为带双引号的字符串
                data[key] = scalarstring.DoubleQuotedScalarString(value)
            else:
                process_yaml(value)  # 递归处理子节点

            # 此处为示例代码：按需修改特定键的值
            if key == 'target_key':
                data[key] = 'new_value'

    elif isinstance(data, list):
        for item in data:
            process_yaml(item)


def read_json(json_path):
    real_path = os.path.abspath(os.path.realpath(json_path))
    if not os.path.exists(real_path):
        raise ValueError(f"Json file path: '{json_path}' is not exist!")
    with safe_open(real_path, "r", encoding='utf-8', permission_mode=0o640) as json_file:
        return json.load(json_file)


def check_config(config: dict):
    if config[P_INSTANCES_NUM] > MAX_P_INSTANCES_NUM or config[P_INSTANCES_NUM] < 1:
        msg = "p_instances_num must between 1 to " + str(MAX_P_INSTANCES_NUM)
        raise ValueError(msg)
    if config[D_INSTANCES_NUM] > MAX_D_INSTANCES_NUM or config[D_INSTANCES_NUM] < 1:
        msg = "d_instances_num must between 1 to " + str(MAX_D_INSTANCES_NUM)
        raise ValueError(msg)
    if config[SINGER_P_INSTANCES_NUM] > MAX_SINGER_P_INSTANCES_NODE_NUM or config[P_INSTANCES_NUM] < 1:
        msg = "singer_p_instances_node_num must between 1 to " + str(MAX_SINGER_P_INSTANCES_NODE_NUM)
        raise ValueError(msg)
    if config[SINGER_D_INSTANCES_NUM] > MAX_SINGER_D_INSTANCES_NODE_NUM or config[D_INSTANCES_NUM] < 1:
        msg = "singer_d_instances_node_num must between 1 to " + str(MAX_SINGER_D_INSTANCES_NODE_NUM)
        raise ValueError(msg)


def get_key_loc(data, namespace, key):
    loc = -1
    for item in data:
        loc = loc + 1
        if item[NAME] == key:
            context[namespace + "_" + key] = int(loc)
            return int(loc)
    return int(loc)


def write_yaml(data, output_file, single_doc=False):
    with safe_open(output_file, 'w', encoding="utf-8", permission_mode=0o640) as f:
        if single_doc:
            yaml.dump(data, f)
        else:
            yaml.dump_all(data, f)


def exec_cmd(command, print_log=True):
    cmd_args = shlex.split(command)
    child = subprocess.Popen(cmd_args, stderr=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True)
    stdout, stderr = child.communicate(timeout=60)

    if print_log:
        logging.info(f"Execute command: {command} \n The command result is: \n {stdout}, {stderr}")
    return stdout


def safe_exec_cmd(command_parts, print_log=True):
    """
    安全的命令执行函数，使用参数化调用防止命令注入
    :param command_parts: 命令参数列表
    :param print_log: 是否打印日志
    :return: 命令执行结果
    """
    if isinstance(command_parts, str):
        # 如果传入字符串，使用shlex.split解析
        command_parts = shlex.split(command_parts)

    # 验证命令的第一个参数（可执行文件）
    allowed_commands = ['kubectl', 'sed']
    if command_parts[0] not in allowed_commands:
        raise ValueError(f"Command not allowed: {command_parts[0]}")

    child = subprocess.Popen(command_parts, stderr=subprocess.PIPE,
                           stdout=subprocess.PIPE, universal_newlines=True)
    stdout, stderr = child.communicate(timeout=60)

    if print_log:
        logging.info(f"Execute command: {' '.join(command_parts)} \n The command result is: \n {stdout}, {stderr}")
    return stdout


def safe_kubectl_apply(file_path, namespace=None):
    """
    安全的kubectl apply调用
    :param file_path: YAML文件路径
    :param namespace: 命名空间
    :return: 命令执行结果
    """
    # 验证输入参数
    validated_file_path = validate_path_part(file_path, "file_path")
    cmd_parts = ['kubectl', 'apply', '-f', validated_file_path]

    if namespace:
        validated_namespace = validate_identifier(namespace, "namespace")
        cmd_parts.extend(['-n', validated_namespace])

    return safe_exec_cmd(cmd_parts)


def safe_kubectl_delete(file_path, namespace=None):
    """
    安全的kubectl delete调用
    :param file_path: YAML文件路径
    :param namespace: 命名空间
    :return: 命令执行结果
    """
    # 验证输入参数
    validated_file_path = validate_path_part(file_path, "file_path")
    cmd_parts = ['kubectl', 'delete', '-f', validated_file_path]

    if namespace:
        validated_namespace = validate_identifier(namespace, "namespace")
        cmd_parts.extend(['-n', validated_namespace])

    return safe_exec_cmd(cmd_parts)


def safe_kubectl_create_configmap(name, from_file=None, from_literal=None, namespace=None):
    """
    安全的kubectl create configmap调用
    :param name: configmap名称
    :param from_file: 文件路径
    :param from_literal: 字面值
    :param namespace: 命名空间
    :return: 命令执行结果
    """
    # 验证输入参数
    validated_name = validate_identifier(name, "configmap_name")
    cmd_parts = ['kubectl', 'create', 'configmap', validated_name]

    if from_file:
        validated_file = validate_path_part(from_file, "from_file")
        cmd_parts.extend(['--from-file', validated_file])
    elif from_literal:
        # 对literal值进行转义
        cmd_parts.extend(['--from-literal', shlex.quote(from_literal)])

    if namespace:
        validated_namespace = validate_identifier(namespace, "namespace")
        cmd_parts.extend(['-n', validated_namespace])

    return safe_exec_cmd(cmd_parts)


def is_valid_mount(mount_path):
    if len(mount_path) == 0 or mount_path is None or "$" in mount_path:
        return False
    
    forbidden_paths = ["/var/run/docker.sock"]
    if any(mount_path.startswith(fp) for fp in forbidden_paths):
        return False
    return True


def add_mount_dir(data, add_dict, server_flag=False):
    index = 0
    for key, value in add_dict.items():

        if not is_valid_mount(key) or not is_valid_mount(value):
            continue
        index = index + 1
        containers_volume = CommentedMap([
            (NAME_KEY, NAME_MOUNT + str(index)),
            ('mountPath', value),
        ])
        data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS].append(containers_volume)

        cont_volume_worker = CommentedMap([
            (NAME_KEY, NAME_MOUNT + str(index)),
            ('mountPath', value),
        ])
        if server_flag:
            data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS].append(cont_volume_worker)

        # 创建新的volume条目
        host_volume = CommentedMap([
            (NAME_KEY, NAME_MOUNT + str(index)),
            ('hostPath', CommentedMap([
                ('path', key)
            ]))
        ])
        data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES].append(host_volume)
        if server_flag:
            host_volume_worker = CommentedMap([
                (NAME_KEY, NAME_MOUNT + str(index)),
                ('hostPath', CommentedMap([
                    ('path', key)
                ]))
            ])
            data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][VOLUMES].append(host_volume_worker)


def shell_escape(value):
    """
    安全转义shell脚本中的字符串值，防止命令注入
    """
    if not isinstance(value, str):
        return str(value)

    # 转义特殊字符
    value = value.replace('\\', '\\\\')  # 反斜杠
    value = value.replace('"', '\\"')    # 双引号
    value = value.replace('$', '\\$')    # 美元符号
    value = value.replace('`', '\\`')    # 反引号
    value = value.replace('\n', '\\n')   # 换行符
    value = value.replace('\r', '\\r')   # 回车符
    value = value.replace('\t', '\\t')   # 制表符

    return value


def update_shell_script_safely(script_path, env_config, component_key="", function_name="set_common_env"):
    """
    安全地更新shell脚本中的环境变量函数
    如果函数不存在，则在文件开头添加
    如果函数存在，则替换整个函数
    """
    all_env_vars = {}
    all_env_vars.update(env_config["mindie_common_env"])
    all_env_vars["MODEL_ID"] = env_config["MODEL_ID"]
    all_env_vars["MODEL_NAME"] = CONFIG_MODEL_NAME
    if component_key and component_key in env_config:
        all_env_vars.update(env_config[component_key])

    with safe_open(script_path, 'r', permission_mode=0o640) as f:
        lines = f.readlines()

    # 查找函数开始和结束位置
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

    # 更新或添加函数
    if start_idx != -1 and end_idx != -1:
        new_lines = lines[:start_idx] + new_function_lines + lines[end_idx + 1:]
    else:
        new_lines = new_function_lines + ["\n"] + lines

    with safe_open(script_path, 'w', permission_mode=0o640) as f:
        f.writelines(new_lines)


def modify_controller_mount(data, config):
    add_mount_dir(data, config["deploy_mount_path"]["ms_controller_mount"], )


def modify_coordinator_mount(data, config):
    add_mount_dir(data, config["deploy_mount_path"]["ms_coordinator_mount"])


def modify_p_server_mount(data, config):
    add_mount_dir(data, config["deploy_mount_path"]["prefill_server_mount"], True)


def modify_d_server_mount(data, config):
    add_mount_dir(data, config["deploy_mount_path"]["decode_server_mount"], True)


def modify_controller_yaml(data, config, env_config):
    data[METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[METADATA][NAME] = config[CONFIG_JOB_ID] + "-controller"
    data[METADATA][NAMESPACE] = config[CONFIG_JOB_ID]
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][IMAGE] = config[IMAGE_NAME]
    if POD_RESCHEDULING in data[METADATA][LABELS]:
        data[METADATA][LABELS][POD_RESCHEDULING] = DoubleQuotedScalarString(
            str(data[METADATA][LABELS][POD_RESCHEDULING])
        )
    modify_controller_env(data, config)
    modify_controller_mount(data, config)


def modify_controller_replicas(data, config):
    if CONTROLLER_BACKUP_CFG in deploy_config and CONTROLLER_BACKUP_SW in deploy_config[CONTROLLER_BACKUP_CFG]:
        backup_switch = config[CONTROLLER_BACKUP_CFG][CONTROLLER_BACKUP_SW]
        master_cnt = data[SPEC][REPLICA_SPECS][MASTER][REPLICAS]
        worker_cnt = POD_NUM_ZERO
        if backup_switch is True or str(backup_switch).lower() == "true":
            data[SPEC][REPLICA_SPECS][WORKER] = copy.deepcopy(data[SPEC][REPLICA_SPECS][MASTER])
            worker_cnt = data[SPEC][REPLICA_SPECS][WORKER][REPLICAS]
        data[SPEC][RUN_POLICY][SCHEDULING_POLICY][MIN_AVAILABLE] = master_cnt + worker_cnt


def modify_coordinator_yaml_app_v1(data, config):
    data[METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[METADATA][NAME] = config[CONFIG_JOB_ID] + "-coordinator"
    data[METADATA][NAMESPACE] = config[CONFIG_JOB_ID]
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][IMAGE] = config[IMAGE_NAME]
    if POD_RESCHEDULING in data[METADATA][LABELS]:
        data[METADATA][LABELS][POD_RESCHEDULING] = DoubleQuotedScalarString(
            str(data[METADATA][LABELS][POD_RESCHEDULING])
        )
    modify_coordinator_env(data, config)
    modify_coordinator_mount(data, config)


def modify_coordinator_yaml_v1(data, config):
    data[METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[METADATA][NAMESPACE] = config[CONFIG_JOB_ID]


def modify_server_yaml_v1(data, config, index, pd_flag):
    data[METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[METADATA][NAMESPACE] = config[CONFIG_JOB_ID]
    data[METADATA][NAME] = RINGS_CONFIG_MINDIE_SERVER + pd_flag + str(index)
    data[METADATA][LABELS][MIND_CLUSTER_SCALING_RULE] = "scaling-rule"
    decode_distrubute = config["decode_distribute_enable"]
    prefill_distribute = config["prefill_distribute_enable"]
    if pd_flag == "p":
        data[METADATA][LABELS][MIND_CLUSTER_GROUP_NAME] = "group0"
        if prefill_distribute != decode_distrubute:
            data[METADATA][LABELS][DEPLOY_SERVER_NUM] = DoubleQuotedScalarString(str(prefill_distribute))
        else:
            data[METADATA][LABELS][DEPLOY_SERVER_NUM] = DoubleQuotedScalarString(PREFILL_DEPLOY)
    else:
        data[METADATA][LABELS][MIND_CLUSTER_GROUP_NAME] = "group1"
        if prefill_distribute != decode_distrubute:
            data[METADATA][LABELS][DEPLOY_SERVER_NUM] = DoubleQuotedScalarString(str(decode_distrubute))
        else:
            data[METADATA][LABELS][DEPLOY_SERVER_NUM] = DoubleQuotedScalarString(DECODE_DEPLOY)


def modify_server_yaml_priority(priority_data, data, pd_flag, deploy_config, index):
    job_id = deploy_config[CONFIG_JOB_ID]
    if pd_flag == "p":
        priority_data[METADATA][NAME] = f"{job_id}-{LOW_PRIORITY}-{pd_flag}{index}"
        priority_data[VALUE] = 1
        data[SPEC][RUN_POLICY][SCHEDULING_POLICY][PRIORITY_CLASS] = f"{job_id}-{LOW_PRIORITY}-{pd_flag}{index}"
    else:
        priority_data[METADATA][NAME] = f"{job_id}-{HIGH_PRIORITY}-{pd_flag}{index}"
        priority_data[VALUE] = 100
        data[SPEC][RUN_POLICY][SCHEDULING_POLICY][PRIORITY_CLASS] = f"{job_id}-{HIGH_PRIORITY}-{pd_flag}{index}"


def modify_server_yaml_mind_v1(data, config, index, pd_flag, ext):
    modify_server_yaml_common(config, data, ext["env_config"], pd_flag)
    modify_npu_num(data, config, pd_flag)
    modify_name_labels(config, data, index, pd_flag)
    modify_ascend_config(data, index, pd_flag)
    modify_server_config(data, index, pd_flag)
    modify_weight_mount_path(config, data)
    modify_replica_num(data, ext["single_instance_pod_num"])
    modify_sp_block_num(data, pd_flag, config)


def modify_sp_block_num(data, pd_flag, config):
    if HARDWARE_TYPE not in config or config[HARDWARE_TYPE] == "800I_A2":
        del data[METADATA][ANNOTATIONS]
        return
    if pd_flag == "d":
        single_d_instance_pod_num = int(config[SINGER_D_INSTANCES_NUM])
        d_pod_npu_num = int(config[D_POD_NPU_NUM])
        sp_block_num = single_d_instance_pod_num * d_pod_npu_num
        data[METADATA][ANNOTATIONS][SP_BLOCK] = DoubleQuotedScalarString(str(sp_block_num))
    else:
        single_p_instance_pod_num = int(config[SINGER_P_INSTANCES_NUM])
        p_pod_npu_num = int(config[P_POD_NPU_NUM])
        sp_block_num = single_p_instance_pod_num * p_pod_npu_num
        data[METADATA][ANNOTATIONS][SP_BLOCK] = DoubleQuotedScalarString(str(sp_block_num))


def add_mount_log(data, config, has_worker=False):
    add_mount_common(data, "mindie-log", config["mindie_host_log_path"], config["mindie_container_log_path"],
                     has_worker)


def add_mount_common(data, name, host_path, container_path, has_worker=False):
    log_volume_mounts = CommentedMap([
        (NAME_KEY, name),
        ('mountPath', container_path),
    ])
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS].append(log_volume_mounts)

    log_volume_mounts = CommentedMap([
        (NAME_KEY, name),
        ('mountPath', container_path),
    ])
    if has_worker:
        data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS].append(log_volume_mounts)

    # 创建新的volume条目
    log_volume = CommentedMap([
        (NAME_KEY, name),
        ('hostPath', CommentedMap([
            ('path', host_path)
        ]))
    ])
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES].append(log_volume)
    if has_worker:
        log_volume = CommentedMap([
            (NAME_KEY, name),
            ('hostPath', CommentedMap([
                ('path', host_path)
            ]))
        ])
        data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][VOLUMES].append(log_volume)


def modify_name_labels(config, data, index, pd_flag):
    data[METADATA][NAME] = MINDIE_SERVER + pd_flag + str(index)
    data[METADATA][LABELS][MIND_CLUSTER_SCALING_RULE] = "scaling-rule"
    decode_distrubute = config["decode_distribute_enable"]
    prefill_distribute = config["prefill_distribute_enable"]
    if pd_flag == "p":
        data[METADATA][LABELS][MIND_CLUSTER_GROUP_NAME] = "group0"
        if prefill_distribute != decode_distrubute:
            data[METADATA][LABELS][DEPLOY_SERVER_NUM] = DoubleQuotedScalarString(str(prefill_distribute))
        else:
            data[METADATA][LABELS][DEPLOY_SERVER_NUM] = DoubleQuotedScalarString(PREFILL_DEPLOY)
    else:
        data[METADATA][LABELS][MIND_CLUSTER_GROUP_NAME] = "group1"
        if prefill_distribute != decode_distrubute:
            data[METADATA][LABELS][DEPLOY_SERVER_NUM] = DoubleQuotedScalarString(str(decode_distrubute))
        else:
            data[METADATA][LABELS][DEPLOY_SERVER_NUM] = DoubleQuotedScalarString(DECODE_DEPLOY)


def modify_replica_num(data, singer_instances_node_num):
    data[SPEC][RUN_POLICY][SCHEDULING_POLICY][MIN_AVAILABLE] = singer_instances_node_num
    data[SPEC][REPLICA_SPECS][WORKER][REPLICAS] = singer_instances_node_num - WORKER_REPLICAS_OFFSET


def modify_ascend_config(data, index, pd_flag):
    config_loc = get_key_loc(data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS],
                             VOLUME_MOUNTS, ASCEND_910_CONFIG)
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS][config_loc][NAME] \
        = ASCEND_910_CONFIG + "-" + pd_flag + str(index)
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS][config_loc][NAME] \
        = ASCEND_910_CONFIG + "-" + pd_flag + str(index)
    volumes_config_loc = get_key_loc(data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES], VOLUMES,
                                     ASCEND_910_CONFIG)
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES][volumes_config_loc][NAME] \
        = ASCEND_910_CONFIG + "-" + pd_flag + str(index)
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][VOLUMES][volumes_config_loc][NAME] \
        = ASCEND_910_CONFIG + "-" + pd_flag + str(index)
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES][volumes_config_loc][CONFIG_MAP][NAME] \
        = RINGS_CONFIG_MINDIE_SERVER + pd_flag + str(index)
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][VOLUMES][volumes_config_loc][CONFIG_MAP][NAME] \
        = RINGS_CONFIG_MINDIE_SERVER + pd_flag + str(index)


def modify_server_config(data, index, pd_flag):
    server_config_loc = get_key_loc(data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS],
                                    VOLUME_MOUNTS, "mindie-server-config")
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS][server_config_loc][NAME] \
        = MINDIE_SERVER + pd_flag + str(index) + CONFIG
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS][server_config_loc][NAME] \
        = MINDIE_SERVER + pd_flag + str(index) + CONFIG
    volumes_server_config_loc = get_key_loc(data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES],
                                            VOLUMES, "mindie-server-config")
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES][volumes_server_config_loc][NAME] = (
            MINDIE_SERVER + pd_flag + str(index) + CONFIG)
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES][volumes_server_config_loc][CONFIG_MAP][NAME] = (
            MINDIE_SERVER + pd_flag + str(index) + CONFIG)
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][VOLUMES][volumes_server_config_loc][NAME] = (
            MINDIE_SERVER + pd_flag + str(index) + CONFIG)
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][VOLUMES][volumes_server_config_loc][CONFIG_MAP][NAME] = (
            MINDIE_SERVER + pd_flag + str(index) + CONFIG)


def modify_weight_mount_path(config, data):
    mindie_log_path_loc = get_key_loc(data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS],
                                      VOLUME_MOUNTS, "weight-mount-path")
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS][mindie_log_path_loc][MOUNT_PATH] \
        = config[WEIGHT_MOUNT_PATH]
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][CONTAINERS][0][VOLUME_MOUNTS][mindie_log_path_loc][MOUNT_PATH] \
        = config[WEIGHT_MOUNT_PATH]
    host_log_path_loc = get_key_loc(data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES],
                                    VOLUMES, "weight-mount-path")
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][VOLUMES][host_log_path_loc][HOST_PATH][PATH] = (
        config)[WEIGHT_MOUNT_PATH]
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][VOLUMES][host_log_path_loc][HOST_PATH][PATH] = (
        config)[WEIGHT_MOUNT_PATH]


def modify_server_yaml_common(config, data, env_config, pd_flag):
    modify_server_env(data, config, env_config, pd_flag)
    data[METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[METADATA][NAMESPACE] = config[CONFIG_JOB_ID]
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][IMAGE] = config[IMAGE_NAME]
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][CONTAINERS][0][IMAGE] = config[IMAGE_NAME]
    if HARDWARE_TYPE in config and config["hardware_type"] == "800I_A3":
        data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC]["nodeSelector"]["accelerator-type"] = "module-a3-16"
        data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC]["nodeSelector"]["accelerator-type"] = "module-a3-16"


def modify_server_yaml_singer_v1(data, config):
    data[METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]


def modify_server_yaml_singer_apps_v1(data, config):
    data[METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][IMAGE] = config[IMAGE_NAME]
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][METADATA][LABELS][JOB_ID] = config[CONFIG_JOB_ID]


def modify_npu_num(data, config, pd_flag):
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][RESOURCES][REQUESTS][ASCEND_910_NPU_NUM] \
        = config[pd_flag + POD_NPU_NUM]
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][RESOURCES][LIMITS][ASCEND_910_NPU_NUM] \
        = config[pd_flag + POD_NPU_NUM]
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][CONTAINERS][0][RESOURCES][REQUESTS][ASCEND_910_NPU_NUM] \
        = config[pd_flag + POD_NPU_NUM]
    data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][CONTAINERS][0][RESOURCES][LIMITS][ASCEND_910_NPU_NUM] \
        = config[pd_flag + POD_NPU_NUM]


def modify_controller_env(data, config):
    modify_env_common(data, "CONTROLLER_LOG_CONFIG_PATH", config[CONTAINER_LOG_PATH], False)
    if len(config["mindie_host_log_path"]) != 0:
        add_mount_log(data, config, False)


def modify_coordinator_env(data, config):
    modify_env_common(data, "COORDINATOR_LOG_CONFIG_PATH", config[CONTAINER_LOG_PATH], False)
    if len(config["mindie_host_log_path"]) != 0:
        add_mount_log(data, config, False)


def modify_coordinator_yaml_replicas(data, config):
    if "coordinator_backup_cfg" in config and config["coordinator_backup_cfg"]["function_enable"]:
        data = modify_coordinator_yaml_affinity(data)
        data[SPEC][REPLICA_SPECS][WORKER] = copy.deepcopy(data[SPEC][REPLICA_SPECS][MASTER])
        data[SPEC]["runPolicy"]["schedulingPolicy"]["minAvailable"] = 2


def modify_coordinator_yaml_affinity(data):
    template = data[SPEC]["replicaSpecs"]["Master"].setdefault("template", CommentedMap())
    pod_spec = template.setdefault('spec', CommentedMap())
    affinity = pod_spec.setdefault('affinity', CommentedMap())
    node_affinity = CommentedMap()
    required_node_terms = CommentedSeq()
    match_expressions = CommentedSeq()

    match_expressions.append(CommentedMap([
        ('key', 'accelerator'),
        ('operator', 'NotIn'),
        ('values', ['huawei-Ascend910'])
    ]))
    required_node_terms.append(CommentedMap([
        ('matchExpressions', match_expressions)
    ]))
    node_affinity['requiredDuringSchedulingIgnoredDuringExecution'] = CommentedMap([
        ('nodeSelectorTerms', required_node_terms)
    ])
    affinity['nodeAffinity'] = node_affinity
    pod_anti_affinity = CommentedMap()
    anti_affinity_terms = CommentedSeq()
    app_label = "mindie-ms-coordinator"
    label_selector = CommentedMap([
        ('matchExpressions', CommentedSeq([
            CommentedMap([
                ('key', 'app'),
                ('operator', 'In'),
                ('values', [app_label])
            ])
        ]))
    ])

    anti_affinity_terms.append(CommentedMap([
        ('labelSelector', label_selector),
        ('topologyKey', 'kubernetes.io/hostname')
    ]))
    pod_anti_affinity['requiredDuringSchedulingIgnoredDuringExecution'] = anti_affinity_terms
    affinity['podAntiAffinity'] = pod_anti_affinity
    return data


def modify_env_common(data, env_key, env_value, has_worker=False):
    env_loc = get_key_loc(data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][ENV],
                          ENV, env_key)
    data[SPEC][REPLICA_SPECS][MASTER][TEMPLATE][SPEC][CONTAINERS][0][ENV][env_loc][VALUE] = env_value
    if has_worker:
        data[SPEC][REPLICA_SPECS][WORKER][TEMPLATE][SPEC][CONTAINERS][0][ENV][env_loc][VALUE] = (
            env_value)


def modify_server_env(data, config, env_config, pd_flag):
    modify_env_common(data, "MINDIE_LOG_CONFIG_PATH", config[CONTAINER_LOG_PATH], True)
    modify_env_common(data, "INITIAL_DP_SERVER_PORT", DoubleQuotedScalarString(str(INIT_PORT)), True)
    
    # 根据pd_flag导入对应的环境变量
    if pd_flag == "p":
        modify_env_common(data, "ROLE", "p", True)
        update_shell_script_safely(BOOT_SHELL_PATH, env_config, "mindie_server_prefill_env", "set_prefill_env")
    elif pd_flag == "d":
        modify_env_common(data, "ROLE", "d", True)
        update_shell_script_safely(BOOT_SHELL_PATH, env_config, "mindie_server_decode_env", "set_decode_env")
    else:
        raise Exception("pd_flag is not p or d")

    if len(config["mindie_host_log_path"]) != 0:
        add_mount_log(data, config, True)


def generator_yaml(input_yaml, output_file, json_path, single_doc=False, env_config=None):
    """
    主函数，读取YAML文件，处理数据，然后写入新文件。
    """

    # 读取用户配置
    json_config = read_json(json_path)
    json_config = json_config["deploy_config"]
    check_config(json_config)
    ext = dict()
    if "controller" in input_yaml:
        data = load_yaml(input_yaml, single_doc)
        modify_controller_yaml(data, json_config, env_config)
        modify_controller_replicas(data, json_config)
        update_shell_script_safely(BOOT_SHELL_PATH, env_config, "mindie_ms_controller_env", "set_controller_env")
        write_yaml(data, output_file, single_doc)
    elif "coordinator" in input_yaml:
        data = load_yaml(input_yaml, single_doc)
        modify_coordinator_yaml_app_v1(data[0], json_config)
        modify_coordinator_yaml_v1(data[1], json_config)
        update_shell_script_safely(BOOT_SHELL_PATH, env_config, "mindie_ms_coordinator_env", "set_coordinator_env")
        modify_coordinator_yaml_replicas(data[0], json_config)
        write_yaml(data, output_file, single_doc)
    elif "server" in input_yaml:
        p_total, d_total = obtain_server_instance_total(json_config)
        p_base, d_base = obtain_server_instance_base_config(json_config)
        p_max = max(p_total, p_base)
        d_max = max(d_total, d_base)
        for p_index in range(p_max):
            data = load_yaml(input_yaml, single_doc)
            modify_server_yaml_v1(data[0], json_config, p_index, "p")

            ext["env_config"] = env_config
            ext["single_instance_pod_num"] = json_config[SINGER_P_INSTANCES_NUM]
            modify_server_yaml_mind_v1(data[2], json_config, p_index, "p", ext)
            modify_server_yaml_priority(data[1], data[2], "p", json_config, p_index)
            last_output_file = output_file + "_p" + str(p_index) + ".yaml"
            write_yaml(data, last_output_file)
        for d_index in range(d_max):
            data = load_yaml(input_yaml, single_doc)
            modify_server_yaml_v1(data[0], json_config, d_index, "d")
            ext["env_config"] = env_config
            ext["single_instance_pod_num"] = json_config[SINGER_D_INSTANCES_NUM]
            modify_server_yaml_mind_v1(data[2], json_config, d_index, "d", ext)
            modify_server_yaml_priority(data[1], data[2], "d", json_config, d_index)
            last_output_file = output_file + "_d" + str(d_index) + ".yaml"
            write_yaml(data, last_output_file)
    elif "single" in input_yaml:
        data = load_yaml(input_yaml, single_doc)
        modify_server_yaml_singer_v1(data[0], json_config)
        modify_server_yaml_singer_apps_v1(data[1], json_config)
        write_yaml(data, output_file, single_doc)

    update_shell_script_safely(BOOT_SHELL_PATH, env_config)


def load_yaml(input_yaml, single_doc):
    # 打开原始yaml文件
    with safe_open(input_yaml, 'r', encoding="utf-8", permission_mode=0o640) as f:
        if single_doc:
            data = ym.safe_load(f)
        else:
            data = list(ym.safe_load_all(f))
    process_yaml(data)
    return data


def update_json_value(data, path, new_value, delimiter="/"):
    """
    修改JSON数据中指定路径的值
    :param data: dict类型，原始JSON数据（字典格式）
    :param path: str类型，目标路径（例如："user/address/street"）
    :param new_value: 要设置的新值
    :param delimiter: 路径分隔符，默认为/
    :return: 修改后的字典
    """
    keys = path.split(delimiter)
    current = data
    # 逐层遍历到目标父节点
    for key in keys[:-1]:
        current = current[key]
    # 修改最终键的值
    current[keys[-1]] = new_value
    return data


def update_dict(original, modified):
    """
    递归更新原始字典，新增修改字典中存在但原始字典没有的字段
    :param original: 将被修改的原始字典
    :param modified: 包含修改内容的字典
    """
    for key in modified:
        # 处理已存在的键
        if key in original:
            # 递归处理嵌套字典
            if isinstance(modified[key], dict) and isinstance(original[key], dict):
                update_dict(original[key], modified[key])
            # 直接更新非字典值
            elif original[key] != modified[key]:
                original[key] = modified[key]
        # 添加新增键（包含嵌套结构）
        else:
            # 递归创建嵌套字典结构
            if isinstance(modified[key], dict):
                original[key] = {}
                update_dict(original[key], modified[key])
            # 直接添加普通值
            else:
                original[key] = modified[key]
    return original


def write_json_data(data, json_path):
    with safe_open(json_path, 'w', permission_mode=0o640) as r:
        json.dump(data, r, indent=4, ensure_ascii=False)


def exec_cm_create_kubectl_multi(deploy_config, out_path):
    job_id = deploy_config[CONFIG_JOB_ID]
    out_conf_path = os.path.join(out_path, 'conf')
    logging.info("Starting to execute kubectl create configmap multi")
    # 创建各种configmaps
    safe_kubectl_create_configmap("common-env", from_literal="MINDIE_USER_HOME_PATH=/usr/local", namespace=job_id)
    safe_kubectl_create_configmap("boot-bash-script", from_file="./boot_helper/boot.sh", namespace=job_id)
    safe_kubectl_create_configmap("server-prestop-bash-script",
                                  from_file="./boot_helper/server_prestop.sh", namespace=job_id)
    safe_kubectl_create_configmap("python-script-get-group-id",
                                  from_file="./boot_helper/get_group_id.py", namespace=job_id)
    safe_kubectl_create_configmap("python-script-update-server-conf",
                                 from_file="./boot_helper/update_mindie_server_config.py", namespace=job_id)
    safe_kubectl_create_configmap("global-ranktable",
                                  from_file="./gen_ranktable_helper/global_ranktable.json", namespace=job_id)
    safe_kubectl_create_configmap("python-file-utils",
                                  from_file="./utils/file_utils.py", namespace=job_id)
    
    # 创建配置相关的configmaps
    safe_kubectl_create_configmap("mindie-ms-coordinator-config",
                                 from_file=os.path.join(out_conf_path, "ms_coordinator.json"), namespace=job_id)
    safe_kubectl_create_configmap("mindie-ms-controller-config",
                                 from_file=os.path.join(out_conf_path, "ms_controller.json"), namespace=job_id)
    safe_kubectl_create_configmap("mindie-ms-node-manager-config",
                                  from_file=os.path.join(out_conf_path, "node_manager.json"), namespace=job_id)
    safe_kubectl_create_configmap("mindie-http-client-ctl-config",
                                 from_file=os.path.join(out_conf_path, "http_client_ctl.json"), namespace=job_id)
    safe_kubectl_create_configmap("scaling-rule",
                                 from_file=os.path.join(str(out_path), "elastic_scaling.json"), namespace=job_id)
    safe_kubectl_create_configmap("python-script-gen-config-single-container",
                                 from_file="./boot_helper/gen_config_single_container.py", namespace=job_id)


def exec_cm_elastic_kubectl(deploy_config, out_path):
    job_id = deploy_config[CONFIG_JOB_ID]
    logging.info("Starting to execute kubectl update configmap elastic")
    exec_cmd("kubectl delete configmap scaling-rule" + NAME_FLAG + deploy_config[CONFIG_JOB_ID])
    exec_cmd("kubectl create configmap scaling-rule --from-file=" +
             os.path.join(str(out_path), "elastic_scaling.json" + NAME_FLAG + deploy_config[CONFIG_JOB_ID]))
    # 利用k8s的informer机制，给controller的pod打一个annotation，使得configmap的变化pod可以马上可以感知到
    pods_output = exec_cmd("kubectl get pods -l app=mindie-ms-controller -n " + deploy_config[CONFIG_JOB_ID] +
                           " -o name")
    pods = pods_output.strip().splitlines()

    current_time = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d%H%M%S')
    for pod in pods:
        # 去除前缀"pod/"
        pod = pod[4:]
        annotate_cmd = (f"kubectl annotate pod {pod} -n " + deploy_config[CONFIG_JOB_ID] +
                        " config-update=update_scaling_rule-" + current_time + " --overwrite")
        exec_cmd(annotate_cmd)
    logging.info("End to execute kubectl update configmap elastic")


def elastic_distributed_server_deploy(deploy_config, out_conf_path, out_deploy_yaml_path):
    elastic_distributed_server_deploy_p(deploy_config, out_conf_path, out_deploy_yaml_path)
    elastic_distributed_server_deploy_d(deploy_config, out_conf_path, out_deploy_yaml_path)


def elastic_distributed_server_deploy_p(deploy_config, out_conf_path, out_deploy_yaml_path):
    job_id = deploy_config[CONFIG_JOB_ID]
    p_total, _ = obtain_server_instance_total(deploy_config)
    p_base, _ = obtain_server_instance_base_config(deploy_config)
    if p_total < p_base:
        logging.info(f"Scale-in p instance, {p_base} -> {p_total}")
        for index in range(p_total, p_base):
            configmap_name = f"mindie-server-p{index}-config"
            # 删除旧的configmap
            exec_cmd(f"kubectl delete configmap {configmap_name} -n {job_id}")
            # 删除旧的YAML
            safe_kubectl_delete(os.path.join(out_deploy_yaml_path, f"mindie_server_p{index}{YAML}"), job_id)
    if p_total > p_base:
        logging.info(f"Scale-out p instance, {p_base} -> {p_total}")
        for index in range(p_base, p_total):
            # 删除旧的configmap和YAML
            configmap_name = f"mindie-server-p{index}-config"
            exec_cmd(f"kubectl delete configmap {configmap_name} -n {job_id}")
            safe_kubectl_delete(os.path.join(out_deploy_yaml_path, f"mindie_server_p{index}{YAML}"), job_id)
            # 创建新的configmap
            config_file = os.path.join(out_conf_path, "config_p.json")
            safe_kubectl_create_configmap(configmap_name, from_file=f"config.json={config_file}", namespace=job_id)
            # 应用YAML文件
            safe_kubectl_apply(os.path.join(out_deploy_yaml_path, f"mindie_server_p{index}{YAML}"), job_id)


def elastic_distributed_server_deploy_d(deploy_config, out_conf_path, out_deploy_yaml_path):
    job_id = deploy_config[CONFIG_JOB_ID]
    _, d_total = obtain_server_instance_total(deploy_config)
    _, d_base = obtain_server_instance_base_config(deploy_config)
    if d_total < d_base:
        logging.info(f"Scale-in d instance, {d_base} -> {d_total}")
        for index in range(d_total, d_base):
            configmap_name = f"mindie-server-d{index}-config"
            # 删除旧的configmap
            exec_cmd(f"kubectl delete configmap {configmap_name} -n {job_id}")
            # 删除旧的YAML
            safe_kubectl_delete(os.path.join(out_deploy_yaml_path, f"mindie_server_d{index}{YAML}"), job_id)
    if d_total > d_base:
        logging.info(f"Scale-out d instance, {d_base} -> {d_total}")
        for index in range(d_base, d_total):
            # 删除旧的configmap和YAML
            configmap_name = f"mindie-server-d{index}-config"
            exec_cmd(f"kubectl delete configmap {configmap_name} -n {job_id}")
            safe_kubectl_delete(os.path.join(out_deploy_yaml_path, f"mindie_server_d{index}{YAML}"), job_id)
            # 创建新的configmap
            config_file = os.path.join(out_conf_path, "config_d.json")
            safe_kubectl_create_configmap(configmap_name, from_file=f"config.json={config_file}", namespace=job_id)
            # 应用YAML文件
            safe_kubectl_apply(os.path.join(out_deploy_yaml_path, f"mindie_server_d{index}{YAML}"), job_id)


def exec_all_kubectl_multi(deploy_config, out_path, user_config_path, model_id):
    out_conf_path = os.path.join(out_path, 'conf')
    out_deploy_yaml_path = os.path.join(out_path, 'deployment')
    if is_first_run(deploy_config):
        exec_cm_create_kubectl_multi(deploy_config, out_path)
        logging.info("Starting to execute kubectl create controller and coordinator")
        safe_kubectl_apply(os.path.join(out_deploy_yaml_path,
                                        "mindie_ms_coordinator.yaml"), deploy_config[CONFIG_JOB_ID])
        safe_kubectl_apply(os.path.join(out_deploy_yaml_path,
                                        "mindie_ms_controller.yaml"), deploy_config[CONFIG_JOB_ID])
        logging.info("Starting to execute kubectl create server")
        distributed_server_deploy(deploy_config, out_conf_path, out_deploy_yaml_path)
    else:
        exec_cm_elastic_kubectl(deploy_config, out_path)
        logging.info("Starting to execute kubectl elastic server")
        elastic_distributed_server_deploy(deploy_config, out_conf_path, out_deploy_yaml_path)
    refresh_user_config_json(user_config_path, model_id)
    logging.info("Starting to generate global ranktable")
    ext = dict()
    ext['is_acjob'] = 'true'
    if deploy_config["decode_distribute_enable"] != deploy_config["prefill_distribute_enable"]:
        ext[P_DEPLOY_SERVER] = str(deploy_config["prefill_distribute_enable"])
        ext[D_DEPLOY_SERVER] = str(deploy_config["decode_distribute_enable"])
    else:
        ext[P_DEPLOY_SERVER] = PREFILL_DEPLOY
        ext[D_DEPLOY_SERVER] = DECODE_DEPLOY
    ext['namespace'] = deploy_config[CONFIG_JOB_ID]
    p_instance_total, d_instance_total = obtain_server_instance_total(deploy_config)
    ext["coordinator_backup_enable"] = deploy_config.get("coordinator_backup_cfg", {}).get("function_enable", False)
    ext["controller_backup_enable"] = deploy_config.get("controller_backup_cfg", {}).get("function_sw", False)
    generate_global_ranktable(False, p_instance_total, d_instance_total, True, ext)

    annotate_cmd = (f"kubectl annotate pods --all -n {deploy_config[CONFIG_JOB_ID]} "
                    "config-update=update-global-ranktable --overwrite --all")
    if not safe_exec_cmd(annotate_cmd, False):
        logging.warning("update annotate failed, which may increase the delay for ranktable update")
    logging.info("End to generate global ranktable")


def distributed_server_deploy(config_dict, out_conf_path, out_deploy_yaml_path):
    job_id = config_dict[CONFIG_JOB_ID]
    for index in range(config_dict[P_INSTANCES_NUM]):
        # 使用安全的函数替代直接拼接命令
        safe_kubectl_delete(os.path.join(out_deploy_yaml_path, f"mindie_server_p{index}{YAML}"), job_id)
        # 删除旧的configmap
        configmap_name = f"mindie-server-p{index}-config"
        exec_cmd(f"kubectl delete configmap {configmap_name} -n {job_id}")
        # 创建configmap
        config_file = os.path.join(out_conf_path, "config_p.json")
        safe_kubectl_create_configmap(configmap_name, from_file=f"config.json={config_file}", namespace=job_id)
        # 应用YAML文件
        safe_kubectl_apply(os.path.join(out_deploy_yaml_path, f"mindie_server_p{index}{YAML}"), job_id)
    for index in range(config_dict[D_INSTANCES_NUM]):
        # 使用安全的函数替代直接拼接命令
        safe_kubectl_delete(os.path.join(out_deploy_yaml_path, f"mindie_server_d{index}{YAML}"), job_id)
        # 删除旧的configmap
        configmap_name = f"mindie-server-d{index}-config"
        exec_cmd(f"kubectl delete configmap {configmap_name} -n {job_id}")
        # 创建configmap
        config_file = os.path.join(out_conf_path, "config_d.json")
        safe_kubectl_create_configmap(configmap_name, from_file=f"config.json={config_file}", namespace=job_id)
        # 应用YAML文件
        safe_kubectl_apply(os.path.join(out_deploy_yaml_path, f"mindie_server_d{index}{YAML}"), job_id)


def exec_all_kubectl_singer(config_dict, out_path):
    job_id = config_dict[CONFIG_JOB_ID]
    out_conf_path = os.path.join(out_path, 'conf')
    # 创建configmaps
    safe_kubectl_create_configmap("common-env", from_literal="MINDIE_USER_HOME_PATH=/usr/local", namespace=job_id)
    safe_kubectl_create_configmap("boot-bash-script", from_file="./boot_helper/boot.sh", namespace=job_id)
    safe_kubectl_create_configmap("config-file-path", from_file=out_conf_path, namespace=job_id)
    safe_kubectl_create_configmap("python-script-gen-config-single-container",
                                 from_file="./boot_helper/gen_config_single_container.py", namespace=job_id)
    # 应用YAML文件
    yaml_file = os.path.join(out_conf_path, "deployment", "mindie_service_single_container.yaml")
    safe_kubectl_apply(yaml_file, job_id)


def assign_cert_files(ms_tls_config, deploy_config_tls_config, cert_type, is_controller=True):

    """
    assign cert file config by deploy_config
    :param ms_tls_config: ms config dict
    :param deploy_config_tls_config:  deploy_config
    :param cert_type: "infer"/"management"
    :return: none
    """
    enable = deploy_config_tls_config[TLS_ENABLE]
    if cert_type not in ['infer', 'management', 'ccae', 'cluster', 'etcd_server']:
        logging.info("Unsupported cert type, only 'infer', 'management', 'ccae', 'clusterd', 'etcd_server' supported")
    type_key = cert_type + '_tls_items'
    if enable is not False:
        ms_tls_config[CA_CERT] = deploy_config_tls_config[type_key][CA_CERT]
        ms_tls_config[TLS_CERT] = deploy_config_tls_config[type_key][TLS_CERT]
        ms_tls_config[TLS_KEY] = deploy_config_tls_config[type_key][TLS_KEY]
        ms_tls_config[TLS_PASSWD] = deploy_config_tls_config[type_key][TLS_PASSWD]
        ms_tls_config[TLS_CRL] = deploy_config_tls_config[type_key][TLS_CRL]


def update_controller_tls_info(modify_result_dict, deploy_config):
    if deploy_config[TLS_CONFIG][TLS_ENABLE] is None:
        modify_result_dict[TLS_CONFIG]["request_coordinator_tls_enable"] = \
            deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["request_server_tls_enable"] = deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["http_server_tls_enable"] = deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG][CLUSTER_TLS_ENABLE] = deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG][CCAE_TLS_ENABLE] = deploy_config[TLS_CONFIG][CCAE_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG][ETCD_SERVER_TLS_ENABLE] = deploy_config[TLS_CONFIG][ETCD_SERVER_TLS_ENABLE]
    else:
        modify_result_dict[TLS_CONFIG]["request_coordinator_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["request_server_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["http_server_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG][CLUSTER_TLS_ENABLE] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG][CCAE_TLS_ENABLE] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG][ETCD_SERVER_TLS_ENABLE] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["alarm_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]

    # assign request coordinator TLS
    assign_cert_files(modify_result_dict[TLS_CONFIG]["request_coordinator_tls_items"],
                      deploy_config[TLS_CONFIG], MANAGEMENT)
    # assign request server TLS
    assign_cert_files(modify_result_dict[TLS_CONFIG]["request_server_tls_items"],
                      deploy_config[TLS_CONFIG], MANAGEMENT)
    # assign request http
    assign_cert_files(modify_result_dict[TLS_CONFIG]["http_server_tls_items"],
                      deploy_config[TLS_CONFIG], MANAGEMENT)
    # assign clusterD tls
    assign_cert_files(modify_result_dict[TLS_CONFIG]["cluster_tls_items"],
                      deploy_config[TLS_CONFIG], "cluster")
    # assign ccae tls
    if CCAE_TLS_ITEMS in deploy_config[TLS_CONFIG]:
        modify_result_dict[TLS_CONFIG][CCAE_TLS_ITEMS] = deploy_config[TLS_CONFIG][CCAE_TLS_ITEMS]
        assign_cert_files(modify_result_dict[TLS_CONFIG][CCAE_TLS_ITEMS],
                          deploy_config[TLS_CONFIG], "ccae")
    if ETCD_TLS_ITEMS in deploy_config[TLS_CONFIG]:
        assign_cert_files(modify_result_dict[TLS_CONFIG][ETCD_TLS_ITEMS],
                          deploy_config[TLS_CONFIG], "etcd_server")

    return modify_result_dict


def modify_controller_json(modify_config, ms_controller_json, out_json, deploy_config):
    original_config = read_json(ms_controller_json)
    modify_result_dict = update_dict(original_config, modify_config)
    modify_result_dict = update_controller_tls_info(modify_result_dict, deploy_config)
    write_json_data(modify_result_dict, out_json)


def update_coordinator_tls_info(modify_result_dict, deploy_config):
    if deploy_config[TLS_CONFIG][TLS_ENABLE] is None:
        modify_result_dict[TLS_CONFIG]["controller_server_tls_enable"] = deploy_config[TLS_CONFIG][INFER_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["request_server_tls_enable"] = deploy_config[TLS_CONFIG][INFER_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["mindie_client_tls_enable"] = deploy_config[TLS_CONFIG][INFER_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["mindie_mangment_tls_enable"] = deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG][ETCD_SERVER_TLS_ENABLE] = deploy_config[TLS_CONFIG][ETCD_SERVER_TLS_ENABLE]
    else:
        modify_result_dict[TLS_CONFIG]["controller_server_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["request_server_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["mindie_client_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["mindie_mangment_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["alarm_client_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["status_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["external_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG][ETCD_SERVER_TLS_ENABLE] = deploy_config[TLS_CONFIG][TLS_ENABLE]

    # assign request controller TLS
    assign_cert_files(modify_result_dict[TLS_CONFIG]["controller_server_tls_items"],
                      deploy_config[TLS_CONFIG], "management", is_controller=False)
    # assign request server TLS
    assign_cert_files(modify_result_dict[TLS_CONFIG]["request_server_tls_items"],
                      deploy_config[TLS_CONFIG], "infer", is_controller=False)
    # assign client tls
    assign_cert_files(modify_result_dict[TLS_CONFIG]["mindie_client_tls_items"],
                      deploy_config[TLS_CONFIG], "infer", is_controller=False)
    # assign management tls
    assign_cert_files(modify_result_dict[TLS_CONFIG]["mindie_mangment_tls_items"],
                      deploy_config[TLS_CONFIG], "management", is_controller=False)

    if ETCD_TLS_ITEMS in deploy_config[TLS_CONFIG]:
        assign_cert_files(modify_result_dict[TLS_CONFIG][ETCD_TLS_ITEMS],
                          deploy_config[TLS_CONFIG], "etcd_server", is_controller=False)
    return modify_result_dict


def modify_coordinator_json(modify_config, ms_coordinator_json, out_json, deploy_config):
    original_config = read_json(ms_coordinator_json)
    modify_result_dict = update_dict(original_config, modify_config)
    modify_result_dict = update_coordinator_tls_info(modify_result_dict, deploy_config)
    if "coordinator_backup_cfg" in deploy_config:
        modify_result_dict["backup_config"]["function_enable"] =\
            deploy_config["coordinator_backup_cfg"]["function_enable"]
    write_json_data(modify_result_dict, out_json)


def update_node_manager_tls_info(modify_result_dict, deploy_config):
    assign_cert_files(modify_result_dict[TLS_CONFIG]["server_tls_items"],
                      deploy_config[TLS_CONFIG], "management", is_controller=False)
    assign_cert_files(modify_result_dict[TLS_CONFIG]["client_tls_items"],
                      deploy_config[TLS_CONFIG], "management", is_controller=False)
    if deploy_config[TLS_CONFIG][TLS_ENABLE] is None:
        modify_result_dict[TLS_CONFIG]["server_tls_enable"] = deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["client_tls_enable"] = deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]
    else:
        modify_result_dict[TLS_CONFIG]["server_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
        modify_result_dict[TLS_CONFIG]["client_tls_enable"] = deploy_config[TLS_CONFIG][TLS_ENABLE]
    return modify_result_dict


def modify_node_manager_json(ms_node_manager_json, out_json, deploy_config):
    original_config = read_json(ms_node_manager_json)
    modify_result_dict = update_node_manager_tls_info(original_config, deploy_config)
    write_json_data(modify_result_dict, out_json)


def get_path_with_separator(path):
    dirname = os.path.dirname(path)
    return dirname + TLS_PATH_SEPARATOR if dirname else ""


def get_crl_files(path):
    return [os.path.basename(path)] if path else []


def update_server_tls_info(modify_result_dict, deploy_config):
    # assign infer TLS
    key = "infer_tls_items"
    modify_result_dict[SERVER_CONFIG]["tlsCaPath"] = get_path_with_separator(deploy_config[TLS_CONFIG][key][CA_CERT])
    modify_result_dict[SERVER_CONFIG]["tlsCaFile"] = [os.path.basename(deploy_config[TLS_CONFIG][key][CA_CERT])]
    modify_result_dict[SERVER_CONFIG]["tlsCert"] = deploy_config[TLS_CONFIG][key][TLS_CERT]
    modify_result_dict[SERVER_CONFIG]["tlsPk"] = deploy_config[TLS_CONFIG][key][TLS_KEY]
    modify_result_dict[SERVER_CONFIG]["tlsPkPwd"] = deploy_config[TLS_CONFIG][key][TLS_PASSWD]
    modify_result_dict[SERVER_CONFIG]["tlsCrlPath"] = get_path_with_separator(deploy_config[TLS_CONFIG][key][TLS_CRL])
    modify_result_dict[SERVER_CONFIG]["tlsCrlFiles"] = get_crl_files(deploy_config[TLS_CONFIG][key][TLS_CRL])
    modify_result_dict[SERVER_CONFIG]["httpsEnabled"] = deploy_config[TLS_CONFIG][TLS_ENABLE] \
        if deploy_config[TLS_CONFIG][TLS_ENABLE] is not None else deploy_config[TLS_CONFIG][INFER_TLS_ENABLE]
    # assign management TLS
    key = MANAGEMENT_TLS_ITEMS
    modify_result_dict[SERVER_CONFIG]["managementTlsCaPath"] = get_path_with_separator(
        deploy_config[TLS_CONFIG][key][CA_CERT])
    modify_result_dict[SERVER_CONFIG]["managementTlsCaFile"] = [
        os.path.basename(deploy_config[TLS_CONFIG][key][CA_CERT])]
    modify_result_dict[SERVER_CONFIG]["managementTlsCert"] = deploy_config[TLS_CONFIG][key][TLS_CERT]
    modify_result_dict[SERVER_CONFIG]["managementTlsPk"] = deploy_config[TLS_CONFIG][key][TLS_KEY]
    modify_result_dict[SERVER_CONFIG]["managementTlsPkPwd"] = deploy_config[TLS_CONFIG][key][TLS_PASSWD]
    modify_result_dict[SERVER_CONFIG]["managementTlsCrlPath"] = get_path_with_separator(
        deploy_config[TLS_CONFIG][key][TLS_CRL])
    modify_result_dict[SERVER_CONFIG]["managementTlsCrlFiles"] = get_crl_files(deploy_config[TLS_CONFIG][key][TLS_CRL])
    # assign metrics TLS
    key = "metrics_tls_items"
    modify_result_dict[SERVER_CONFIG]["metricsTlsCaPath"] = get_path_with_separator(
        deploy_config[TLS_CONFIG][key][CA_CERT])
    modify_result_dict[SERVER_CONFIG]["metricsTlsCaFile"] = [
        os.path.basename(deploy_config[TLS_CONFIG][key][CA_CERT])]
    modify_result_dict[SERVER_CONFIG]["metricsTlsCert"] = deploy_config[TLS_CONFIG][key][TLS_CERT]
    modify_result_dict[SERVER_CONFIG]["metricsTlsPk"] = deploy_config[TLS_CONFIG][key][TLS_KEY]
    modify_result_dict[SERVER_CONFIG]["metricsTlsPkPwd"] = deploy_config[TLS_CONFIG][key][TLS_PASSWD]
    modify_result_dict[SERVER_CONFIG]["metricsTlsCrlPath"] = get_path_with_separator(
        deploy_config[TLS_CONFIG][key][TLS_CRL])
    modify_result_dict[SERVER_CONFIG]["metricsTlsCrlFiles"] = get_crl_files(deploy_config[TLS_CONFIG][key][TLS_CRL])
    # internal tls
    key = MANAGEMENT_TLS_ITEMS
    modify_result_dict[SERVER_CONFIG]["interCommTlsCaPath"] = get_path_with_separator(
        deploy_config[TLS_CONFIG][key][CA_CERT])
    modify_result_dict[SERVER_CONFIG]["interCommTlsCaFiles"] = [
        os.path.basename(deploy_config[TLS_CONFIG][key][CA_CERT])]
    modify_result_dict[SERVER_CONFIG]["interCommTlsCert"] = deploy_config[TLS_CONFIG][key][TLS_CERT]
    modify_result_dict[SERVER_CONFIG]["interCommPk"] = deploy_config[TLS_CONFIG][key][TLS_KEY]
    modify_result_dict[SERVER_CONFIG]["interCommPkPwd"] = deploy_config[TLS_CONFIG][key][TLS_PASSWD]
    modify_result_dict[SERVER_CONFIG]["interCommTlsCrlPath"] = get_path_with_separator(
        deploy_config[TLS_CONFIG][key][TLS_CRL])
    modify_result_dict[SERVER_CONFIG]["interCommTlsCrlFiles"] = get_crl_files(deploy_config[TLS_CONFIG][key][TLS_CRL])
    modify_result_dict[SERVER_CONFIG]["interCommTLSEnabled"] = deploy_config[TLS_CONFIG][TLS_ENABLE] \
        if deploy_config[TLS_CONFIG][TLS_ENABLE] is not None else deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]
    # grpc
    key = MANAGEMENT_TLS_ITEMS
    modify_result_dict[BACKEND_CONFIG]["interNodeTlsCaPath"] = get_path_with_separator(
        deploy_config[TLS_CONFIG][key][CA_CERT])
    modify_result_dict[BACKEND_CONFIG]["interNodeTlsCaFiles"] = [
        os.path.basename(deploy_config[TLS_CONFIG][key][CA_CERT])]
    modify_result_dict[BACKEND_CONFIG]["interNodeTlsCert"] = deploy_config[TLS_CONFIG][key][TLS_CERT]
    modify_result_dict[BACKEND_CONFIG]["interNodeTlsPk"] = deploy_config[TLS_CONFIG][key][TLS_KEY]
    modify_result_dict[BACKEND_CONFIG]["interNodeTlsPkPwd"] = deploy_config[TLS_CONFIG][key][TLS_PASSWD]
    modify_result_dict[BACKEND_CONFIG]["interNodeTlsCrlPath"] = get_path_with_separator(
        deploy_config[TLS_CONFIG][key][TLS_CRL])
    modify_result_dict[BACKEND_CONFIG]["interNodeTlsCrlFiles"] = get_crl_files(deploy_config[TLS_CONFIG][key][TLS_CRL])
    modify_result_dict[BACKEND_CONFIG]["interNodeTLSEnabled"] = deploy_config[TLS_CONFIG][TLS_ENABLE] \
        if deploy_config[TLS_CONFIG][TLS_ENABLE] is not None else deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]


def set_max_iter_times(ms_config_p_json, ms_config_d_json):
    d_backend_config = ms_config_d_json.get("BackendConfig", {})
    d_schedule_config = d_backend_config.get("ScheduleConfig", {})

    p_backend_config = ms_config_p_json.get("BackendConfig", {})
    p_schedule_config = p_backend_config.get("ScheduleConfig", {})

    if MAX_ITER_TIMES in p_schedule_config:
        logging.warning("The value of maxIterTimes in node P will be overwritten by that of node D!")

    if MAX_ITER_TIMES in d_schedule_config:
        p_schedule_config[MAX_ITER_TIMES] = d_schedule_config[MAX_ITER_TIMES]
    else:
        logging.warning("Using default value of maxIterTimes")


def modify_config_p_json(modify_config, ms_config_p_json, ms_config_d_json, out_json, deploy_config):
    original_config = read_json(ms_config_p_json)
    modify_result_dict = update_dict(original_config, modify_config)
    update_server_tls_info(modify_result_dict, deploy_config)

    # Synchronize the maxIterTimes of Node D to Node P.
    set_max_iter_times(modify_result_dict, ms_config_d_json)
    
    write_json_data(modify_result_dict, out_json)


def modify_config_d_json(modify_config, ms_config_d_json, out_json, deploy_config):
    original_config = read_json(ms_config_d_json)
    modify_result_dict = update_dict(original_config, modify_config)
    update_server_tls_info(modify_result_dict, deploy_config)
    write_json_data(modify_result_dict, out_json)


def modify_http_client_json(modify_config, ms_client_ctl_json, out_json, deploy_config):
    original_config = read_json(ms_client_ctl_json)
    modify_result_dict = update_dict(original_config, modify_config)
    assign_cert_files(modify_result_dict["cert"],
                      deploy_config[TLS_CONFIG], "management")
    modify_result_dict[TLS_ENABLE] = deploy_config[TLS_CONFIG][TLS_ENABLE] \
        if deploy_config[TLS_CONFIG][TLS_ENABLE] is not None else deploy_config[TLS_CONFIG][MANAGEMENT_TLS_ENABLE]
    write_json_data(modify_result_dict, out_json)


def get_config_model_name(server_config_path, singer_server_config_path):
    try:
        server_config = read_json(server_config_path)
    except OSError as reason:
        logging.info(str(reason))
        server_config = read_json(singer_server_config_path)
    return server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0]["modelName"]


def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--user_config_path",
        type=str,
        default="./user_config.json",
        help="Path of user config"
    )
    parser.add_argument('--conf_path', type=str, default='./conf', help="Path of conf")
    parser.add_argument("--deploy_yaml_path", type=str, default='./deployment', help="Path of yaml")
    parser.add_argument("--output_path", type=str, default="./output", help="Path of output")
    return parser.parse_args()


def update_ms_controller_config(controller_config, p_server_config, d_server_config, deploy_config):
    temp_updated = update_json_value(controller_config, "multi_node_infer_config/p_node_config/tp_size",
                                     p_server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0][TP])
    temp_updated = update_json_value(temp_updated, "multi_node_infer_config/p_node_config/dp_size",
                                     p_server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0][DP])
    temp_updated = update_json_value(controller_config, "multi_node_infer_config/p_node_config/sp_size",
                                     p_server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0][SP])
    temp_updated = update_json_value(controller_config, "multi_node_infer_config/p_node_config/cp_size",
                                     p_server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0][CP])
    temp_updated = update_json_value(temp_updated, "multi_node_infer_config/p_node_config/node_machine_num",
                                     deploy_config["single_p_instance_pod_num"])
    temp_updated = update_json_value(temp_updated, "multi_node_infer_config/p_node_config/enable_dist_dp_server",
                                     p_server_config[SERVER_CONFIG][DIST_DP_SERVER_ENABLED])

    temp_updated = update_json_value(temp_updated, "multi_node_infer_config/d_node_config/tp_size",
                                     d_server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0][TP])
    temp_updated = update_json_value(temp_updated, "multi_node_infer_config/d_node_config/dp_size",
                                     d_server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0][DP])
    temp_updated = update_json_value(controller_config, "multi_node_infer_config/d_node_config/sp_size",
                                     d_server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0][SP])
    temp_updated = update_json_value(controller_config, "multi_node_infer_config/d_node_config/cp_size",
                                     d_server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0][CP])
    temp_updated = update_json_value(temp_updated, "multi_node_infer_config/d_node_config/enable_dist_dp_server",
                                     d_server_config[SERVER_CONFIG][DIST_DP_SERVER_ENABLED])
    if d_server_config[SERVER_CONFIG][DIST_DP_SERVER_ENABLED]:
        temp_updated = update_json_value(temp_updated, "multi_node_infer_config/d_node_config/node_machine_num",
                                         d_server_config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0][DP])
    else:
        temp_updated = update_json_value(temp_updated, "multi_node_infer_config/d_node_config/node_machine_num",
                                         deploy_config["single_d_instance_pod_num"])
    if CONTROLLER_BACKUP_CFG in deploy_config and CONTROLLER_BACKUP_SW in deploy_config[CONTROLLER_BACKUP_CFG]:
        temp_updated = update_json_value(temp_updated, "controller_backup_cfg/function_sw",
                                         deploy_config[CONTROLLER_BACKUP_CFG][CONTROLLER_BACKUP_SW])
    if FAULT_RECOVERY_FUNC in deploy_config and LINGQU_LINK in deploy_config[FAULT_RECOVERY_FUNC]:
        temp_updated = update_json_value(temp_updated, "fault_recovery_func_dict/lingqu_link",
                                         deploy_config[FAULT_RECOVERY_FUNC][LINGQU_LINK])
    return temp_updated


def modify_controller_config(out_controller_config):
    out_p_server_config = read_json(ms_config_p_json)
    out_d_server_config = read_json(ms_config_d_json)
    updated = update_ms_controller_config(out_controller_config, out_p_server_config, out_d_server_config,
                                          deploy_config)
    write_json_data(updated, ms_controller_json)


def save_elastic_scaling_config(config, output):
    p_instances_num, d_instances_num = obtain_server_instance_total(config)
    p_server_num = config.get("single_p_instance_pod_num")
    d_server_num = config.get("single_d_instance_pod_num")
    elastic_scaling = dict()
    elastic_scaling["version"] = "1.0"
    elastic_scaling["status"] = "completed"
    elastic_scaling_list = list()
    for i in range(p_instances_num):
        group_dict = dict()
        group_dict[GROUP_LIST] = list()
        server_count = (p_instances_num - i) * p_server_num + d_instances_num * d_server_num
        group_dict["server_count"] = str(server_count)
        group0 = {
            "group_name": "group0",
            "group_num": str(p_instances_num - i),
            "server_num_per_group": str(p_server_num)
        }
        group_dict[GROUP_LIST].append(group0)
        group1 = {
            "group_name": "group1",
            "group_num": str(d_instances_num),
            "server_num_per_group": str(d_server_num)
        }
        group_dict[GROUP_LIST].append(group1)
        elastic_scaling_list.append(group_dict)
    elastic_scaling["elastic_scaling_list"] = elastic_scaling_list
    with safe_open(os.path.join(output, 'elastic_scaling.json'), 'w', permission_mode=0o640) as file:
        json.dump(elastic_scaling, file, indent=4)


def check_elastic_config(deploy_config):
    p_base, d_base = obtain_server_instance_base_config(deploy_config)
    p_change = obtain_server_elastic_config_p(deploy_config)
    d_change = obtain_server_elastic_config_d(deploy_config)
    p_total = p_base + p_change
    d_total = d_base + d_change
    if p_total <= INSTANCE_NUM_ZERO:
        raise ValueError(f"after elastic scaling, instance(p) must be greater than {INSTANCE_NUM_ZERO}")
    if d_total <= INSTANCE_NUM_ZERO:
        raise ValueError(f"after elastic scaling, instance(d) must be greater than {INSTANCE_NUM_ZERO}")


def is_first_run(deploy_config):
    p_change = obtain_server_elastic_config_p(deploy_config)
    d_change = obtain_server_elastic_config_d(deploy_config)
    return p_change == INSTANCE_NUM_ZERO and d_change == INSTANCE_NUM_ZERO


def obtain_server_instance_total(deploy_config):
    p_base, d_base = obtain_server_instance_base_config(deploy_config)
    p_change = obtain_server_elastic_config_p(deploy_config)
    d_change = obtain_server_elastic_config_d(deploy_config)
    return p_base + p_change, d_base + d_change


def obtain_server_instance_base_config(deploy_config):
    p_base = int(deploy_config[P_INSTANCES_NUM])
    d_base = int(deploy_config[D_INSTANCES_NUM])
    if p_base <= INSTANCE_NUM_ZERO or p_base > MAX_P_INSTANCES_NUM:
        raise ValueError(f"{P_INSTANCES_NUM} must between ({INSTANCE_NUM_ZERO}, {MAX_P_INSTANCES_NUM}]")
    if d_base <= INSTANCE_NUM_ZERO or d_base > MAX_D_INSTANCES_NUM:
        raise ValueError(f"{D_INSTANCES_NUM} must between ({INSTANCE_NUM_ZERO}, {MAX_D_INSTANCES_NUM}]")
    return p_base, d_base


def obtain_server_elastic_config_p(deploy_config):
    p_change = int(deploy_config[ELASTIC_P_CHANGE])
    if p_change < MIN_ELASTIC_NUM or p_change > MAX_ELASTIC_NUM:
        raise ValueError(f"{ELASTIC_P_CHANGE} must between [{MIN_ELASTIC_NUM}, {MAX_ELASTIC_NUM}]")
    return p_change


def obtain_server_elastic_config_d(deploy_config):
    d_change = int(deploy_config[ELASTIC_D_CHANGE])
    if d_change < MIN_ELASTIC_NUM or d_change > MAX_ELASTIC_NUM:
        raise ValueError(f"{ELASTIC_D_CHANGE} must between [{MIN_ELASTIC_NUM}, {MAX_ELASTIC_NUM}]")
    return d_change


def refresh_user_config_json(user_config_path, model_id):
    json_config = read_json(user_config_path)
    p_total, d_total = obtain_server_instance_total(json_config[DEPLOY_CONFIG])
    json_config[DEPLOY_CONFIG][P_INSTANCES_NUM] = p_total
    json_config[DEPLOY_CONFIG][D_INSTANCES_NUM] = d_total
    json_config[DEPLOY_CONFIG][MODEL_ID] = model_id
    json_config[DEPLOY_CONFIG][ELASTIC_P_CHANGE] = INSTANCE_NUM_ZERO
    json_config[DEPLOY_CONFIG][ELASTIC_D_CHANGE] = INSTANCE_NUM_ZERO
    with safe_open(user_config_path, 'w', permission_mode=0o640) as file:
        json.dump(json_config, file, indent=2)
    logging.info("Refresh user_config success.")


def obtain_model_id(deploy_config):
    config_model_id = deploy_config[MODEL_ID].strip()
    if is_first_run(deploy_config):
        if config_model_id:
            raise ValueError(f"first execution, {MODEL_ID} must be empty")
        return f"{deploy_config[CONFIG_JOB_ID]}_{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d%H%M%S')}"
    else:
        if not config_model_id:
            raise ValueError(f"non first execution, {MODEL_ID} cannot be empty")
        return config_model_id


if __name__ == '__main__':

    args = parse_arguments()

    input_conf_root_path = args.conf_path
    deploy_yaml_root_path = args.deploy_yaml_path
    output_root_path = args.output_path
    user_config_path = args.user_config_path
    if not os.path.exists(output_root_path):
        os.makedirs(output_root_path, mode=0o750)
    if not os.path.exists(os.path.join(output_root_path, "conf")):
        os.makedirs(os.path.join(output_root_path, "conf"), mode=0o750)
    if not os.path.exists(os.path.join(output_root_path, "deployment")):
        os.makedirs(os.path.join(output_root_path, "deployment"), mode=0o750)
    if not os.path.exists(user_config_path):
        raise FileNotFoundError(f"Configuration file not found at: '{user_config_path}'."
                                f"Please verify the path or provide a valid config file.")
    else:
        logging.info(f"Starting service deployment using config file path: {user_config_path}.")

    controller_input_yaml = os.path.join(deploy_yaml_root_path, 'controller_init.yaml')
    controller_output_yaml = os.path.join(output_root_path, 'deployment', 'mindie_ms_controller.yaml')
    coordinator_input_yaml = os.path.join(deploy_yaml_root_path, 'coordinator_init.yaml')
    coordinator_output_yaml = os.path.join(output_root_path, 'deployment', 'mindie_ms_coordinator.yaml')
    server_input_yaml = os.path.join(deploy_yaml_root_path, 'server_init.yaml')
    server_output_yaml = os.path.join(output_root_path, 'deployment', 'mindie_server')
    singer_container_input_yaml = os.path.join(deploy_yaml_root_path, 'single_container_init.yaml')
    singer_container_output_yaml = os.path.join(output_root_path, 'deployment', 'mindie_service_single_container.yaml')
    ms_controller_json = os.path.join(output_root_path, 'conf', 'ms_controller.json')
    ms_coordinator_json = os.path.join(output_root_path, 'conf', 'ms_coordinator.json')
    ms_config_p_json = os.path.join(output_root_path, 'conf', 'config_p.json')
    ms_config_d_json = os.path.join(output_root_path, 'conf', 'config_d.json')
    ms_client_ctl_json = os.path.join(output_root_path, 'conf', 'http_client_ctl.json')
    server_config_path = os.path.join(output_root_path, 'conf', 'config.json')
    ms_node_manager_json = os.path.join(output_root_path, 'conf', 'node_manager.json')

    init_ms_controller_json = os.path.join(input_conf_root_path, 'ms_controller.json')
    init_ms_coordinator_json = os.path.join(input_conf_root_path, 'ms_coordinator.json')
    init_ms_config_p_json = os.path.join(input_conf_root_path, 'config_p.json')
    init_ms_config_d_json = os.path.join(input_conf_root_path, 'config_d.json')
    init_ms_client_ctl_json = os.path.join(input_conf_root_path, 'http_client_ctl.json')
    init_server_config_path = os.path.join(input_conf_root_path, 'config.json')
    init_ms_node_manager_json = os.path.join(input_conf_root_path, 'node_manager.json')
    env_config_path = os.path.join(input_conf_root_path, 'mindie_env.json')

    json_config = read_json(user_config_path)
    validate_user_config(json_config)
    deploy_config = json_config["deploy_config"]

    # 验证关键配置参数的安全性
    validate_identifier(deploy_config[CONFIG_JOB_ID], CONFIG_JOB_ID)

    if "mindie_env_path" in deploy_config:
        env_config_path = validate_path_part(deploy_config["mindie_env_path"], "mindie_env_path")

    if not os.path.exists(os.path.join(os.getcwd(), "boot_helper", "server_prestop.sh")):
        raise FileNotFoundError("server_prestop.sh not Found!")

    if json_config["mindie_ms_controller_config"]["deploy_mode"] == "pd_disaggregation_single_container":
        generator_yaml(singer_container_input_yaml, singer_container_output_yaml, user_config_path, False)
        exec_all_kubectl_singer(deploy_config, output_root_path)
    else:
        check_elastic_config(deploy_config)
        env_config = read_json(env_config_path)
        env_config["MODEL_ID"] = obtain_model_id(deploy_config)
        save_elastic_scaling_config(deploy_config, output_root_path)
        modify_controller_json(json_config["mindie_ms_controller_config"], init_ms_controller_json, ms_controller_json,
                               deploy_config)
        modify_coordinator_json(json_config["mindie_ms_coordinator_config"], init_ms_coordinator_json,
                                ms_coordinator_json, deploy_config)
        modify_config_p_json(json_config["mindie_server_prefill_config"], init_ms_config_p_json,
                             json_config["mindie_server_decode_config"], ms_config_p_json, deploy_config)
        modify_config_d_json(json_config["mindie_server_decode_config"], init_ms_config_d_json, ms_config_d_json,
                             deploy_config)
        modify_node_manager_json(init_ms_node_manager_json, ms_node_manager_json, deploy_config)

        http_json = json_config["http_client_ctl_config"] if "http_client_ctl_config" in json_config else dict()
        modify_http_client_json(http_json, init_ms_client_ctl_json, ms_client_ctl_json,
                                deploy_config)
        # 非跨机不区分
        modify_config_p_json(json_config["mindie_server_prefill_config"], init_server_config_path,
                             json_config["mindie_server_decode_config"], server_config_path, deploy_config)
        CONFIG_MODEL_NAME = get_config_model_name(ms_config_p_json, server_config_path)
        out_controller_config = read_json(ms_controller_json)
        INIT_PORT = out_controller_config["initial_dist_server_port"]
        modify_controller_config(out_controller_config)
        generator_yaml(controller_input_yaml, controller_output_yaml, user_config_path, True, env_config)
        generator_yaml(coordinator_input_yaml, coordinator_output_yaml, user_config_path, False, env_config)
        generator_yaml(server_input_yaml, server_output_yaml, user_config_path, False, env_config)
        exec_all_kubectl_multi(json_config["deploy_config"], output_root_path, user_config_path, env_config["MODEL_ID"])

    logging.info("all deploy end.")
