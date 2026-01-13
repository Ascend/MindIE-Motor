#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


ms_controller_json_path="./conf/ms_controller.json"
if [ ! -f "$ms_controller_json_path" ]; then
    echo "ERROR: $ms_controller_json_path is not exist! Please put ms_controller.json in ./conf/"
    exit 1
fi
ms_controller_json_content=$(cat $ms_controller_json_path)
deploy_mode=$(echo $ms_controller_json_content | grep -o '"deploy_mode": *[^,}]*' | sed 's/"deploy_mode": *\(.*\)/\1/' | sed 's/"//g')

if [[ $deploy_mode != "pd_separate" && $deploy_mode != "pd_disaggregation" && $deploy_mode != "pd_disaggregation_single_container" && $deploy_mode != "single_node" ]]; then
    echo "ERROR: The value of 'deploy_mode' in ms_controller.json should be 'pd_separate', 'pd_disaggregation', 'pd_disaggregation_single_container' or 'single_node'. The current value is ${deploy_mode}."
    exit 1
fi

is_single_container_flag="false"
if [[ $deploy_mode == "pd_disaggregation_single_container" ]]; then
    is_single_container_flag="true"
fi

kubectl create configmap common-env --from-literal=MINDIE_USER_HOME_PATH=/usr/local -n mindie
kubectl create configmap boot-bash-script --from-file=./boot_helper/boot.sh -n mindie;
kubectl create configmap python-file-utils --from-file=./utils/file_utils.py -n mindie;

if [ $is_single_container_flag == "false" ]; then
    kubectl create configmap python-script-get-group-id --from-file=./boot_helper/get_group_id.py -n mindie;
    kubectl create configmap python-script-update-server-conf --from-file=./boot_helper/update_mindie_server_config.py -n mindie;

    kubectl create configmap global-ranktable --from-file=./gen_ranktable_helper/global_ranktable.json -n mindie;
    kubectl create configmap mindie-server-config --from-file=./conf/config.json -n mindie;
    kubectl create configmap mindie-ms-coordinator-config --from-file=./conf/ms_coordinator.json -n mindie;
    kubectl create configmap mindie-ms-controller-config --from-file=./conf/ms_controller.json -n mindie;
    kubectl create configmap mindie-http-client-ctl-config --from-file=./conf/http_client_ctl.json -n mindie;

    kubectl apply -f ./deployment/mindie_ms_coordinator.yaml;
    kubectl apply -f ./deployment/mindie_ms_controller.yaml;
    kubectl apply -f ./deployment/mindie_server.yaml;
    if [ $# -eq 1 ]; then
        if [[ "$1" == "heter" ]]; then
            kubectl apply -f ./deployment/mindie_server_heterogeneous.yaml;
            python3 ./gen_ranktable_helper/gen_global_ranktable.py --heter True
            exit 0
        fi
    fi
    python3 ./gen_ranktable_helper/gen_global_ranktable.py
elif [ $is_single_container_flag == "true" ]; then
    current_path=$(pwd)
    kubectl create configmap config-file-path --from-file="$current_path"/conf -n mindie
    kubectl create configmap python-script-gen-config-single-container --from-file=./boot_helper/gen_config_single_container.py -n mindie;

    device_type="800i_a2"
    if [ -n "$1" ]; then
        device_type="$1"
    fi
    if [ $device_type == "800i_a2" ]; then
        kubectl apply -f ./deployment/mindie_service_single_container.yaml
    elif [ $device_type == "800i_a3" ]; then
        kubectl apply -f ./deployment/mindie_service_single_container_base_A3.yaml
    else
        echo "ERROR: The device type is not supported for single container deployment. "\
             "For the server model 'Atlas 800I A2', use '800i_a2'; for 'Atlas 800I A3', use '800i_a3'."
        exit 1
    fi
fi