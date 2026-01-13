# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import sys
import json
import logging
import shutil
import socket
import re
import subprocess
import time
from typing import List
from file_utils import safe_open

MODEL_CONFIG = "ModelConfig"
MODEL_DEPLOY_CONFIG = "ModelDeployConfig"
BACKEND_CONFIG = "BackendConfig"
ALL_ENVS = {k.upper(): v for k, v in os.environ.items()}
A2_TYPE = '800i_a2'
A3_TYPE = '800i_a3'
MAX_FILE_SIZE = (100 * 1024 * 1024)

g_used_ports_set = set()
g_ports_allocated = dict()
g_config_dir = ""

# multi port
gen_multi_port_config_json_for_distance = False


def setup_logger():
    standard_logger = logging.getLogger('Deployment Script')
    standard_logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        '[%(asctime)s.%(msecs)03d][ms][%(levelname)s][%(filename)s:%(lineno)d] : [%(name)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler.setFormatter(formatter)

    standard_logger.addHandler(handler)
    return standard_logger


logger = setup_logger()


def check_file_path(path: str):
    if not (os.path.exists(path) and os.path.isfile(path)):
        raise RuntimeError(f"Path not exist or not a file: {path}")
    if os.path.islink(path):
        raise RuntimeError(f"Path is symlink: {path}")


def check_file_size(file_path: str):
    file_size = os.path.getsize(file_path)
    if file_size > MAX_FILE_SIZE:
        raise ValueError(f"File size of {file_path} is {file_size/1024/1024:.4f} M, " \
                         f"which exceeds MAX_FILE_SIZE(100 M)")


class PortHelper:
    _base_port = 2000
    # store allocated port
    _allocated_port = {}

    @staticmethod
    def is_port_available(port, host='127.0.0.1', timeout=1):
        available = False
        try:
            port = int(port)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect((host, port))
        except socket.error:
            available = True
        return available

    @classmethod
    def get_new_port(cls):
        new_port = cls._base_port
        while not cls.is_port_available(new_port):
            new_port += 1
        cls._base_port = new_port + 1
        return new_port

    @classmethod
    def cache_port(cls, key: str, port):
        if key in cls._allocated_port and isinstance(cls._allocated_port[key], list):
            cls._allocated_port[key].append(port)
        else:
            cls._allocated_port[key] = port

    @classmethod
    def get_cached_port(cls, key: str):
        return cls._allocated_port[key]

    @classmethod
    def init_base_port(cls):
        if gen_multi_port_config_json_for_distance is False:
            return
        try:
            initial_dp_server_port = os.getenv('INITIAL_DP_SERVER_PORT')
            if initial_dp_server_port is None or initial_dp_server_port == "" or initial_dp_server_port == "0":
                cls._base_port = 10000
                logger.warning("Init base port failed, default port value will be used")

            else:
                cls._base_port = int(initial_dp_server_port)
        except Exception:
            cls._base_port = 10000
            logger.warning("Init base port failed, default port value will be used")


def get_tp_num():
    try:
        is_gen_server_port = get_bool_env("MINDIE_MS_GEN_SERVER_PORT")
        if is_gen_server_port:
            config_path = os.path.join(g_config_dir, "config.json")
        else:
            config_path = os.path.join(g_config_dir, "config1.json")
        config = load_json_file(config_path)
        if "tp" not in config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0]:
            tp_num = int(config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0]["worldSize"])
        else:
            tp_num = int(config[BACKEND_CONFIG][MODEL_DEPLOY_CONFIG][MODEL_CONFIG][0]["tp"])
        if tp_num < 1:
            logger.error("tp num must be greater than 0.")
            raise ValueError("tp num must be greater than 0.")
        return tp_num
    except Exception:
        logger.error("Get tp num failed.")
        raise


def get_server_num():
    try:
        _, device_num = GenRankTable.parse_npu_info()
        tp_num = get_tp_num()
    except Exception:
        logger.error("Get server num failed.")
        raise

    if (device_num % tp_num) != 0:
        logger.error(f"device_num % tp_num must equal to 0.")
        raise ValueError("device_num % tp_num must equal to 0.")
    server_num = int(device_num / tp_num)
    if server_num < 1:
        logger.error(f"server_num must be greater than 0.")
        raise ValueError(f"server_num must be greater than 0.")
    return server_num


def get_specified_env(env_key: str):
    env_key = env_key.upper()
    if env_key not in ALL_ENVS:
        logger.error(f"env {env_key} not found.")
        raise ValueError(f"env {env_key} not found.")

    env_value = ALL_ENVS[env_key].strip()
    return str(env_value)


def get_bool_env(env_key: str):
    env_key = env_key.upper()
    if env_key not in ALL_ENVS:
        logger.error(f"Environment variable {env_key} is not found.")
        raise ValueError(f"Environment variable {env_key} is not found.")

    env_value = ALL_ENVS[env_key].strip()
    if env_value == "false":
        return False
    else:
        return True


def path_verify(path: str, safe_start: str):
    real_path = os.path.realpath(path)
    if real_path.startswith(safe_start):
        return real_path
    logger.error(f"Invalid path: {path}")
    raise ValueError(f"Invalid path: {path}")


def prepare_json_files(json_dir: str, server_num: int, is_gen_server_port: bool):
    dest_files = ["ms_coordinator.json", "ms_controller.json"] + [
        f"config{i}.json" for i in range(1, server_num + 1)]
    server_config_file = os.path.join(json_dir, "config.json")

    if not is_gen_server_port:
        for dst_name in dest_files[2:]:
            dest = os.path.join(json_dir, dst_name)
            if not os.path.exists(dest):
                logger.error(f"Source file {dst_name} not found.")
                raise FileNotFoundError(f"Source file {dst_name} not found.")
        return dest_files

    for dst_name in dest_files[2:]:
        dest = os.path.join(json_dir, dst_name)
        try:
            server_config_file = path_verify(server_config_file, "/usr/local")
            dest = path_verify(dest, "/usr/local")
            shutil.copy2(server_config_file, dest)
        except FileNotFoundError:
            logger.error(f"Source file {server_config_file} not found.")
            raise
        except PermissionError:
            logger.error(f"Permission denied when accessing {server_config_file} or {dest}.")
            raise
        except Exception as e:
            logger.error(f"An error occurred during JSON file preparation: {e}")
            raise

        os.chmod(dest, 0o640)

    return dest_files


def update_port_value(file_path, port_name, port_key, port_value):
    try:
        if port_name in g_ports_allocated:
            if port_value != g_ports_allocated[port_name]:
                logger.warning(f"The port number of '{port_key}' in {os.path.basename(file_path)} has"
                                f" been allocated, which is {g_ports_allocated[port_name]}.")
                update_json_with_key_value(file_path, port_key, str(g_ports_allocated[port_name]))
        elif port_value in g_used_ports_set:
            port_new = PortHelper.get_new_port()
            g_used_ports_set.add(port_new)
            g_ports_allocated[port_name] = port_new
            update_json_with_key_value(file_path, port_key, str(port_new))
            logger.warning(f"The port number of '{port_key}' in {os.path.basename(file_path)} has"
                            f" been updated to {g_ports_allocated[port_name]},"
                            f" due to {port_value} had been allocated.")
        else:
            g_used_ports_set.add(port_value)
            g_ports_allocated[port_name] = port_value
    except Exception:
        logger.error(f"Update {port_key} in {file_path} failed.")
        raise


def load_json_file(file_path: str):
    try:
        file_path = os.path.realpath(file_path)
        check_file_path(file_path)
        check_file_size(file_path)
        with safe_open(file_path, 'r', encoding="utf-8", permission_mode=0o640) as f:
            content = json.load(f)
            return content
    except FileNotFoundError:
        logger.error(f"File {file_path} not found.")
        raise
    except PermissionError:
        logger.error(f"Permission denied when accessing {file_path}.")
        raise
    except Exception as e:
        logger.error(f"An error occurred while loading json file: {e}")
        raise


def load_file(file_path: str):
    try:
        file_path = os.path.realpath(file_path)
        check_file_path(file_path)
        check_file_size(file_path)
        with safe_open(file_path, 'r', encoding="utf-8", permission_mode=0o640) as f:
            content = f.read()
            return content
    except FileNotFoundError:
        logger.error(f"File {file_path} not found.")
        raise
    except PermissionError:
        logger.error(f"Permission denied when accessing {file_path}.")
        raise
    except Exception as e:
        logger.error(f"An error occurred while loading file: {e}")
        raise


def check_server_config(config_file_path: str, server_id: int):
    try:
        config = load_json_file(config_file_path)
    except Exception:
        logger.error(f"Load {config_file_path} failed.")
        raise
    server_port = ["port", "managementPort", "metricsPort", "interCommPort"]
    for port_name in server_port:
        port = int(config["ServerConfig"][port_name])
        try:
            update_port_value(config_file_path, f"server{server_id}_{port_name}", port_name, port)
        except Exception:
            logger.error(f"Update port value failed.")
            raise
        if server_id == 1:
            PortHelper.cache_port(port_name, [g_ports_allocated[f"server{server_id}_{port_name}"]])
        else:
            PortHelper.cache_port(port_name, g_ports_allocated[f"server{server_id}_{port_name}"])


def get_coordinator_ip_from_config(config_path: str):
    """从ms_coordinator.json配置文件中获取coordinator IP地址"""
    try:
        ms_coordinator_path = os.path.join(config_path, "ms_coordinator.json")
        ms_coordinator = load_json_file(ms_coordinator_path)
        return ms_coordinator["http_config"]["manage_ip"]
    except Exception:
        logger.error(f"Get coordinator IP from config failed.")
        raise


def check_json_files(config_path: str, config_json_files: List[str], is_gen_server_port: bool):
    if gen_multi_port_config_json_for_distance:
        return
    try:
        # check and update ms_coordinator.json
        ms_coordinator_path = os.path.join(config_path, "ms_coordinator.json")
        ms_coordinator = load_json_file(ms_coordinator_path)
        predict_port = int(ms_coordinator["http_config"]["predict_port"])
        update_port_value(ms_coordinator_path, "coordinator_predict_port", "predict_port", predict_port)
        manage_port = int(ms_coordinator["http_config"]["manage_port"])
        update_port_value(ms_coordinator_path, "coordinator_manage_port", "manage_port", manage_port)
    except Exception:
        logger.error(f"Check and update ms_coordinator.json failed.")
        raise

    try:
        # check and update ms_controller.json
        ms_controller_path = os.path.join(config_path, "ms_controller.json")
        ms_controller = load_json_file(ms_controller_path)
        manage_port = int(ms_controller["mindie_ms_coordinator_port"])
        update_port_value(ms_controller_path, "coordinator_manage_port", "mindie_ms_coordinator_port", manage_port)
        http_server_port = int(ms_controller["http_server"]["port"])
        update_port_value(ms_controller_path, "controller_http_server_port", "port", http_server_port)
    except Exception:
        logger.error(f"Check and update ms_controller.json failed.")
        raise

    # check and update server json files
    if not is_gen_server_port:
        for i in range(2, len(config_json_files)):
            file_name = config_json_files[i]
            server_json_path = os.path.join(config_path, file_name)
            try:
                check_server_config(server_json_path, i - 1)
            except Exception:
                logger.error(f"Check and update {file_name} failed.")
                raise


def infer_type(value):
    """
    Infer the type of string value from an environment variable.
    :param value: The string value to analyze.
    :return: The inferred Python type (str, int, float, bool, list).
    """
    if value.lower() in ('true', 'false'):  # Check for boolean
        return bool
    if value.isdigit():  # Check for integer (strictly digits, no decimal point)
        return int
    try:
        float(value)  # Check for float
        return float
    except ValueError:
        pass  # value is not a float, so continue checking
    if value.startswith('[') and value.endswith(']'):  # Check for list
        return list
    return str


must_string_key = ["predict_port", "manage_port", "cache_size", "slots_thresh", "block_thresh", "max_schedule_count",
                   "reordering_type", "max_res_num", "res_limit_rate"]


def update_keys_in_json(config, config_key, config_value):
    """
    Find all keys in a single JSON file.
    """
    if isinstance(config, dict):
        for key, value in config.items():
            if str(key).upper() == config_key.upper():
                config_value = str(config_value)
                if config_key.lower() in must_string_key:
                    config[key] = config_value
                else:
                    config[key] = json.loads(config_value) if infer_type(config_value) != str else config_value
                return (config, True)

            new_config, is_changed = update_keys_in_json(value, config_key, config_value)
            config[key] = new_config
            if is_changed:
                return (config, is_changed)

    elif isinstance(config, list):
        for index, item in enumerate(config):
            new_config, is_changed = update_keys_in_json(item, config_key, config_value)
            config[index] = new_config
            if is_changed:
                return (config, is_changed)

    return (config, False)


def update_json_with_key_value(json_file_path, key, value):
    try:
        config = load_json_file(json_file_path)
    except Exception:
        logger.error(f"Load {json_file_path} failed.")
        raise
    config, is_changed = update_keys_in_json(config, key, value)
    if not is_changed:
        logger.error(f"Can't find {key} in {json_file_path}")
        raise ValueError(f"can't find {key} in {json_file_path}")

    try:
        with safe_open(json_file_path, "w", encoding="utf-8", permission_mode=0o640) as f:
            json.dump(config, f, indent=4)
    except PermissionError:
        logger.error(f"Permission denied when accessing {json_file_path}.")
        raise
    except Exception as e:
        logger.error(f"An error occurred while updating json file: {e}")
        raise


class UpdateJsonAfterEnv:
    @staticmethod
    def __update_server_env(config_path: str, server_json_files: List[str], is_gen_server_port: bool):
        try:
            for index, file_name in enumerate(server_json_files):
                server_json_path = os.path.join(config_path, file_name)

                update_json_with_key_value(server_json_path, "ipAddress", "127.0.0.1")
                update_json_with_key_value(server_json_path, "managementIpAddress", "127.0.0.1")

                if not is_gen_server_port:
                    continue

                # process port info
                server_port = ["port", "managementPort", "metricsPort", "interCommPort"]
                for port_name in server_port:
                    port = PortHelper.get_new_port()
                    update_json_with_key_value(server_json_path, port_name, port)
                    logger.info(f"Port allocate, server{index + 1} {port_name}: {port}")
                    if index == 0:
                        PortHelper.cache_port(port_name, [port])
                    else:
                        PortHelper.cache_port(port_name, port)
        except Exception:
            logger.error("Update server json files failed.")
            raise

    @classmethod
    def update_all(cls, config_path: str, config_json_files: List[str], is_gen_server_port: bool):
        try:
            # update n server json files
            cls.__update_server_env(config_path, config_json_files[2:], is_gen_server_port)
        except Exception:
            logger.error("update server json files failed.")
            raise


class GenRankTable:
    @staticmethod
    def gen_global_ranktable(tp_num, device_type, coordinator_ip: str = "127.0.0.1"):
        try:
            tp_num = int(tp_num)
        except ValueError:
            logger.error("tp_num must be integer.")
            raise
        server_group_list_str = 'server_group_list'
        global_rank_table = {'version': "1.0", server_group_list_str: []}
        try:
            global_rank_table[server_group_list_str].append(GenRankTable.add_mindie_server_group(tp_num, device_type))
            global_rank_table[server_group_list_str].append(GenRankTable.add_mindie_ms_controller_group())
            global_rank_table[server_group_list_str].append(
                GenRankTable.add_mindie_ms_coordinator_group(coordinator_ip))
        except Exception:
            logger.error("Genarate global_ranktable failed.")
            raise
        global_rank_table['status'] = "completed"
        logger.info("global ranktable:\n")
        logger.info(json.dumps(global_rank_table, indent=4))
        try:
            global_rank_table_path = os.path.join(get_specified_env("MIES_INSTALL_PATH"), "global_ranktable.json")
        except Exception:
            logger.error("Get global_ranktable path failed.")
            raise
        with safe_open(global_rank_table_path, "w", encoding="utf-8", permission_mode=0o640) as f:
            f.write(json.dumps(global_rank_table, indent=4))
        # change file mod
        os.chmod(global_rank_table_path, 0o400)

    @staticmethod
    def parse_npu_info():
        npu_pattern = r"NPU ID\s*[:|：]\s*(\d+)"
        chip_pattern = r"Chip Count\s*[:|：]\s*(\d+)"
        npu_info_file_path = os.path.join(g_config_dir, "npu_info.txt")
        try:
            npu_info = load_file(npu_info_file_path)
        except Exception:
            logger.error(f"Load {npu_info_file_path} failed.")
            raise
        npu_matches = re.findall(npu_pattern, npu_info)
        if (npu_matches is None) or (len(npu_matches) == 0):
            logger.error(f"Parse npu id failed in {npu_info_file_path}. "  
             f"The possible reason is that the number of NPU resources requested in the YAML file"
             f" for pulling up the pod is invalid, such as being equal to 1.")
            raise ValueError(f"Parse npu id failed in {npu_info_file_path}.")
        device_ids = [int(match) for match in npu_matches]
        device_num = len(device_ids)

        chip_matches = re.findall(chip_pattern, npu_info)
        chip_count = int(chip_matches[0])
        device_num = chip_count * device_num
        return device_ids, device_num

    @staticmethod
    def gen_hccl_a2(tp_num):
        try:
            device_ids, device_num = GenRankTable.parse_npu_info()
        except Exception:
            logger.error("Parse npu info failed.")
            raise
        device_ip_base = '1.1.1.'
        last_ip = 1
        rank_id = 0

        server_num = int(device_num / tp_num)

        hccl_ranktable = {'status': "completed"}
        server_list = []

        card_idx = 0
        for _ in range(server_num):
            server = {
                'server_id': '127.0.0.1',
                'container_ip': '127.0.0.1'
            }
            device = []
            for idx in range(card_idx, card_idx + tp_num):
                card = {
                    "device_id": str(device_ids[idx]),
                    "device_ip": device_ip_base + str(last_ip),
                    "rank_id": str(rank_id)
                }
                device.append(card)
                last_ip += 1
                rank_id += 1
            server['device'] = device
            server_list.append(server)
            card_idx += tp_num
        hccl_ranktable['server_list'] = server_list
        hccl_ranktable['server_count'] = str(len(hccl_ranktable['server_list']))
        hccl_ranktable['version'] = "1.0"

        return hccl_ranktable

    @staticmethod
    def gen_hccl_a3():
        is_complete = False
        while is_complete is False:
            json_file_path = '/user/serverid/devindex/config/hccl.json'
            json_file_path = os.path.realpath(json_file_path)
            try:
                check_file_path(json_file_path)
                check_file_size(json_file_path)
                with safe_open(json_file_path, 'r', encoding='utf-8', permission_mode=0o640) as file:
                    hccl_ranktable = json.load(file)
                if hccl_ranktable['status'] == "completed":
                    is_complete = True
                time.sleep(2)
            except PermissionError:
                logger.error(f"Permission denied when accessing {json_file_path}.")
                raise
            except Exception as e:
                logger.error(f"An error occurred while generating hccl.json file: {e}")
                raise
        return hccl_ranktable

    @staticmethod
    def add_mindie_server_group(tp_num, device_type):
        try:
            if device_type == A2_TYPE:
                hccl_ranktable = GenRankTable.gen_hccl_a2(tp_num)
            elif device_type == A3_TYPE:
                hccl_ranktable = GenRankTable.gen_hccl_a3()
            else:
                logger.error(f"Unsupported device type: {device_type}.")
                raise ValueError(f"Unsupported device type: {device_type}.")
        except Exception:
            logger.error("Generate hccl ranktable failed.")
            raise
        device_str = 'device'
        server_list_str = 'server_list'
        if device_type == A2_TYPE:
            server_count = int(hccl_ranktable['server_count'])
        else:
            server_count = int(len(hccl_ranktable[server_list_str][0][device_str]) / tp_num)

        native_ip = '127.0.0.1'
        server_group = {'group_id': '2', 'server_count': server_count, server_list_str: []}
        for i in range(server_count):
            server = {'server_id': native_ip, 'server_ip': native_ip,
                    'predict_port': str(PortHelper.get_cached_port('port')[i]),
                    'mgmt_port': str(PortHelper.get_cached_port('managementPort')[i]),
                    'metric_port': str(PortHelper.get_cached_port('metricsPort')[i]),
                    'inter_comm_port': str(PortHelper.get_cached_port('interCommPort')[i])}
            if device_type == A2_TYPE:
                item = hccl_ranktable[server_list_str][i]
                if device_str in item:
                    server[device_str] = item[device_str]
                    for j in range(len(server[device_str])):
                        server[device_str][j]["device_logical_id"] = server[device_str][j]["rank_id"]  # 单机场景直接使用rank_id
                server_group[server_list_str].append(server)
            else:
                item = hccl_ranktable[server_list_str][0][device_str]
                server[device_str] = []
                for j in range(tp_num):
                    server[device_str].append(item[j + i * tp_num])
                    server[device_str][j]["device_logical_id"] = server[device_str][j]["rank_id"]
                server_group[server_list_str].append(server)
        if device_type == A3_TYPE:
            super_pod_list_str = 'super_pod_list'
            server_group[super_pod_list_str] = hccl_ranktable[super_pod_list_str]
            server_group[super_pod_list_str][0][server_list_str][0]['server_id'] = native_ip
        return server_group

    @staticmethod
    def add_mindie_ms_controller_group():
        server_group = {'group_id': '1', 'server_count': 1, 'server_list': [{"server_ip": '127.0.0.1'}]}
        return server_group

    @staticmethod
    def add_mindie_ms_coordinator_group(coordinator_ip: str = "127.0.0.1"):
        server_group = {'group_id': '0', 'server_count': 1, 'server_list': [{"server_ip": coordinator_ip}]}
        return server_group


class MindIEConfigHelper:
    @staticmethod
    def update_mindie_config(config_path: str):
        try:
            if not config_path.strip():
                config_path = MindIEConfigHelper.__get_default_config_file_path()
            server_num = get_server_num()
            is_gen_server_port = get_bool_env("MINDIE_MS_GEN_SERVER_PORT")
            config_json_files = prepare_json_files(config_path, server_num, is_gen_server_port)
            # check port
            check_json_files(config_path, config_json_files, is_gen_server_port)
            # update specified items
            UpdateJsonAfterEnv.update_all(config_path, config_json_files, is_gen_server_port)
            # cache server_num
            PortHelper.cache_port("server_num", server_num)
        except Exception:
            logger.error("Update mindie config failed.")
            raise

    @staticmethod
    def __get_default_config_file_path() -> str:
        try:
            home_dir = get_specified_env("MIES_INSTALL_PATH")
            config_path = os.path.join(home_dir, "conf")
            if not os.path.isdir(config_path):
                logger.error(f"Invalid config path: {config_path}")
                raise ValueError(f"Invalid config path: {config_path}")
            return config_path
        except Exception:
            logger.error("Get default config file path failed.")
            raise


def execute_command(cmd_list):
    with subprocess.Popen(cmd_list,
                          shell=False,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as p:
        out, err = p.communicate(timeout=1000)
    res = out.decode()
    return res


def get_device_type():
    result = execute_command("lspci")
    pci_type = None
    for line in result.splitlines():
        if "accelerators" in line:
            match = re.search(r"Device (d\d{3})", line)
            if match:
                pci_type = match.group(1)
                break
    pci_type_to_device_type = {"d802": "800i_a2", "d803": "800i_a3"}
    return pci_type_to_device_type.get(pci_type, "unknown_device")



if __name__ == '__main__':
    if len(sys.argv) >= 2:
        g_config_dir = sys.argv[1]

    if len(sys.argv) >= 3:
        gen_multi_port_config_json_for_distance = True
        PortHelper.init_base_port()

    try:
        MindIEConfigHelper.update_mindie_config(g_config_dir)
    except Exception:
        logger.error("Update config failed.")
        sys.exit(1)

    # generating multi port strategy does not require generating global ranktable
    if gen_multi_port_config_json_for_distance:
        # successfully finish
        sys.exit(get_server_num())
    
    device_type = get_device_type()
    try:
        coordinator_ip = get_coordinator_ip_from_config(g_config_dir)
        GenRankTable.gen_global_ranktable(get_tp_num(), device_type, coordinator_ip)
    except Exception:
        logger.error("Generate global_ranktable.json failed.")
        sys.exit(1)
    # successfully finish
    sys.exit(0)
