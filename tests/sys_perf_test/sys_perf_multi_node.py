#!/usr/bin/env python3
# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import time
import atexit
import subprocess
import argparse
import random

from command_helper import CommandHelper, kill_all_service
from utils import (
    PerfIndex,
    print_to_screen,
    load_config,
    extract_result_from_perf_csv,
    extract_result_from_common_csv,
    gen_benchmark_cmd,
    save_results,
    parent_dir,
    get_latest_commit_id,
    get_time_stamp_ms
)


STORE_TRUE = 'store_true'


def create_parser():
    '''create arguments
    '''
    parser = argparse.ArgumentParser(description="perf test settings")
    parser.add_argument('-p', '--performance',
                        action=STORE_TRUE,
                        required=False, default=True,
                        help="enable performance test")

    parser.add_argument('-a', '--preciosn',
                        action=STORE_TRUE,
                        required=False, default=True,
                        help="enable preciosn test")

    parser.add_argument('-o', '--outputdir',
                        required=False, default=f'{parent_dir}',
                        help="Set output file path")

    return parser.parse_args()


def clean_up(session_name: str):
    subprocess.Popen(["tmux", "kill-session", "-t", f"{session_name}"], cwd=parent_dir)


def set_env(command_helper_instance, terminal_id, env_config):
    command_helper_instance.exec_command(
        terminal_id, f'export RANK_TABLE_FILE={env_config["RankTableFile"]}', wait_time=1)
    command_helper_instance.exec_command(
        terminal_id, f'export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True', wait_time=1)
    command_helper_instance.exec_command(
        terminal_id, f'export ATB_WORKSPACE_MEM_ALLOC_ALG_TYPE=3', wait_time=1)
    command_helper_instance.exec_command(
        terminal_id, f'export NPU_MEMORY_FRACTION=0.96', wait_time=1)
    command_helper_instance.exec_command(
        terminal_id, f'export MIES_CONTAINER_IP={env_config["ip"]}', wait_time=1)
    command_helper_instance.exec_command(
        terminal_id, f'export HCCL_CONNECT_TIMEOUT=7200', wait_time=1)
    command_helper_instance.exec_command(
        terminal_id, f'HCCL_EXEC_TIMEOUT=0', wait_time=1)


if __name__ == '__main__':
    # get commond line message
    args = create_parser()
    print_to_screen(
        f'[performance: {args.performance}] '
        f'[preciosn: {args.preciosn}] '
    )

    # load config.json
    configs = load_config(f'{parent_dir}/multi_node_config.json')
    environment_config = configs["test_environment"]
    test_cases = configs["test_cases"]

    # create a tmux session
    session_id = random.randint(0, 9999)
    session_name = f'mysession_{session_id}'
    command_helper_instance = CommandHelper(session_name)
    atexit.register(clean_up, session_name)

    server_id = []
    container_names = []
    mies_install_paths = []
    for idx, env_config in enumerate(environment_config):
        # get config from test case
        container_name = env_config["container_name"]
        container_names.append(container_name)
        mies_install_path = env_config.get("mies_install_path", '/usr/local/lib/python3.11/site-packages/mindie_motor')
        mies_install_paths.append(mies_install_path)

        # start container
        # master and client are in the same node
        if idx == 0:
            server_id.append(command_helper_instance.new_terminal())
            command_helper_instance.exec_command(server_id[0], f'docker start {container_name}', wait_time=5)
            command_helper_instance.exec_command(server_id[0], f'docker exec -it {container_name} bash', wait_time=5)
            command_helper_instance.wait_to_enter_container(server_id[0])
            set_env(command_helper_instance, server_id[0], env_config)

            client_id = command_helper_instance.new_terminal()
            command_helper_instance.exec_command(client_id, f'docker start {container_name}', wait_time=5)
            command_helper_instance.exec_command(client_id, f'docker exec -it {container_name} bash', wait_time=5)
            command_helper_instance.wait_to_enter_container(client_id)
            set_env(command_helper_instance, client_id, env_config)
            continue

        ip = env_config["ip"]
        user = env_config["user"]
        server_id.append(command_helper_instance.new_terminal())
        # link to slave server because this script is on master node
        command_helper_instance.exec_command(server_id[-1], f'ssh {user}@{ip}', wait_time=5)
        command_helper_instance.exec_command(server_id[-1], f'docker start {container_name}', wait_time=5)
        command_helper_instance.exec_command(server_id[-1], f'docker exec -it {container_name} bash', wait_time=5)
        time.sleep(5)
        print_to_screen(f'Check if slave node is in container using command `tmux attach -t <session-name>:2`')
        set_env(command_helper_instance, server_id[-1], env_config)

    results_of_performance_test = []
    results_of_precison_test = []
    for test_case in test_cases:
        # filter test cases, TestMode 0 is performance test, TestMode 1 is precison test
        test_mode = test_case["TestMode"]
        if test_mode not in [0, 1]:
            print_to_screen(f'Invalid TestMode of case {test_case["case_id"]}')
            continue
        if test_mode == 0 and not args.performance:
            continue
        if test_mode == 1 and not args.preciosn:
            continue

        # kill all service for master node
        kill_all_service(command_helper_instance, server_id[0])
        kill_all_service(command_helper_instance, client_id)
        command_helper_instance.exec_command(
            server_id[0], f'export MIES_CONFIG_JSON_PATH={test_case["config_path"][0]}', wait_time=1)

        # start service for slave node
        for idx, cur_id in enumerate(server_id):
            if idx == 0:
                continue
            kill_all_service(command_helper_instance, cur_id)
            command_helper_instance.exec_command(
                cur_id, f'export MIES_CONFIG_JSON_PATH={test_case["config_path"][idx]}', wait_time=1)
            command_helper_instance.exec_command(cur_id, "mindie_llm_server", wait_time=1)

        # start service for master node
        command_helper_instance.exec_command(server_id[0], "mindie_llm_server",
                                            True, wait_strs=["Daemon start success"], wait_time=360)

        # start client
        command_helper_instance.exec_command(client_id, f'cd {parent_dir}', wait_time=1)
        benchmark_cmd = gen_benchmark_cmd(test_case, 0)
        print_to_screen(f'benchmark command : {benchmark_cmd}')
        command_helper_instance.exec_command(client_id, benchmark_cmd, True, wait_time=1200,
                                             wait_strs=['Benchmark task completed successfully'])

        # parse and record results
        data = dict()
        data.update({"Commit ID": get_latest_commit_id(), "Time Stamp": get_time_stamp_ms()})
        data.update(extract_result_from_perf_csv(f'{parent_dir}/instance'))
        data.update(extract_result_from_common_csv(f'{parent_dir}/instance'))
        if test_mode == 0:
            results_of_performance_test.append(data)
        else:
            results_of_precison_test.append(data)

        # terminate service
        kill_all_service(command_helper_instance, client_id)
        for cur_id in server_id:
            command_helper_instance.exec_command(cur_id, 'C-c', wait_time=5)

    # graceful exit
    for cur_id in server_id:
        command_helper_instance.exec_command(cur_id, 'exit')
    command_helper_instance.exec_command(client_id, 'exit')

    save_results(results_of_performance_test, f'{args.outputdir}/output_performance.csv')
    save_results(results_of_precison_test, f'{args.outputdir}/output_precison.csv')

    print_to_screen("All test cases done!")