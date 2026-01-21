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

import os
import atexit
import subprocess
import argparse
import random

from command_helper import CommandHelper
from compile_utils import compile_mies, deployment
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
    parser = argparse.ArgumentParser(description="if need compile source code")
    parser.add_argument('-c', '--compile',
                        action=STORE_TRUE,
                        required=False, default=False,
                        help="enable compile")

    parser.add_argument('-d', '--deployment',
                        action=STORE_TRUE,
                        required=False, default=False,
                        help="enable deployment")

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


if __name__ == '__main__':
    # get commond line message
    args = create_parser()
    print_to_screen(
        f'command from user: [compile: {args.compile}] '
        f'[performance: {args.performance}] '
        f'[preciosn: {args.preciosn}] '
    )

    # load config.json
    configs = load_config(f'{parent_dir}/config.json')
    compile_config = configs["compile_environment"]
    environment_config = configs["test_environment"]
    test_cases = configs["test_cases"]
    if args.compile:
        print_to_screen(f'compile config: {compile_config}')

    # create a tmux session
    session_id = random.randint(0, 9999)
    session_name = f'mysession_{session_id}'
    command_helper_instance = CommandHelper(session_name)
    atexit.register(clean_up, session_name)

    # compile source code
    if args.compile:
        compile_mies(command_helper_instance, compile_config)

    # init server and client
    server_id = command_helper_instance.new_terminal()
    client_id = command_helper_instance.new_terminal()

    # get config from test case
    container_name = environment_config["container_name"]
    mies_install_path = environment_config.get("mies_install_path", '$MINDIE_USER_HOME_PATH/lib/python3.11/site-packages/mindie_motor')

    # start container
    command_helper_instance.exec_command(server_id, f'docker start {container_name}', wait_time=5)
    command_helper_instance.exec_command(server_id, f'docker exec -it {container_name} bash', wait_time=5)
    command_helper_instance.wait_to_enter_container(server_id)

    command_helper_instance.exec_command(client_id, f'docker start {container_name}', wait_time=5)
    command_helper_instance.exec_command(client_id, f'docker exec -it {container_name} bash', wait_time=5)
    command_helper_instance.wait_to_enter_container(client_id)

    # deploy compiled
    if args.compile or args.deployment:
        try:
            deployment(command_helper_instance, server_id, environment_config)
        except Exception as e:
            print_to_screen('Deployment failed, please check the compile result and try again')

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

        # kill all service
        command_helper_instance.exec_command(server_id, f'source {parent_dir}/kill_all_service.sh', wait_time=5)
        command_helper_instance.exec_command(client_id, f'source {parent_dir}/kill_all_service.sh', wait_time=5)

        # start service
        command_helper_instance.exec_command(server_id, f'export MIES_CONFIG_JSON_PATH={test_case["config_path"]}',
                                            wait_time=1)
        command_helper_instance.exec_command(server_id, f"mindie_llm_server",
                                            True, wait_strs=["Daemon start success"], wait_time=180)

        # start client
        command_helper_instance.exec_command(client_id, f'cd {parent_dir}', wait_time=1)
        benchmark_cmd = gen_benchmark_cmd(test_case)
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
        command_helper_instance.exec_command(client_id, f'source {parent_dir}/kill_all_service.sh', wait_time=5)

    # graceful exit
    command_helper_instance.exec_command(server_id, 'exit')
    command_helper_instance.exec_command(client_id, 'exit')

    save_results(results_of_performance_test, f'{args.outputdir}/output_preformance.csv')
    save_results(results_of_precison_test, f'{args.outputdir}/output_precison.csv')

    print_to_screen("All test cases done!")