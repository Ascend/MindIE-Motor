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


echo "log.sh示例脚本即将日落，请使用同级目录下的collect_pd_cluster_log.sh脚本收集日志。"
echo "The example script log.sh will be deprecated soon. Please use the collect_pd_cluster_log.sh script in the same directory to collect logs."
NAME_SPACE=mindie
APP_NAME=mindie-server
if [ -n "$1" ]; then
    NAME_SPACE=$1
    APP_NAME=mindie-ms-server
fi
ms_controller_json_path="./conf/ms_controller.json"
if [ ! -f "$ms_controller_json_path" ]; then
    echo "error: $ms_controller_json_path is not exist! Please put ms_controller.json in ./conf/"
    exit 1
fi
ms_controller_json_content=$(cat $ms_controller_json_path)
deploy_mode=$(echo $ms_controller_json_content | grep -o '"deploy_mode": *[^,}]*' | sed 's/"deploy_mode": *\(.*\)/\1/' | sed 's/"//g')

if [[ $deploy_mode != "pd_separate" && $deploy_mode != "pd_disaggregation" && $deploy_mode != "pd_disaggregation_single_container" && $deploy_mode != "single_node" ]]; then
    echo "error: The value of 'deploy_mode' in ms_controller.json should be 'pd_separate', 'pd_disaggregation', 'pd_disaggregation_single_container' or 'single_node'. The current value is ${deploy_mode}."
    exit 1
fi

is_single_container_flag="false"
if [[ $deploy_mode == "pd_disaggregation_single_container" ]]; then
    is_single_container_flag="true"
fi

if [ $is_single_container_flag == "false" ]; then
    echo "==================================================== Stdout Log From MindIE-MS Coordinator ===================================================="
    kubectl logs -l app=mindie-ms-coordinator -n "$NAME_SPACE"
    echo

    echo "==================================================== Stdout Log From MindIE-MS Controller ======================================================"
    kubectl logs -l app=mindie-ms-controller -n "$NAME_SPACE"
    echo

    echo "==================================================== Stdout Log From MindIE-Server ============================================================="
    kubectl logs -l app="$APP_NAME" -n "$NAME_SPACE"
    echo
elif [ $is_single_container_flag == "true" ]; then
    echo "==================================================== Stdout Log From MindIE-Service ============================================================="
    kubectl logs -l app=mindie-server -n "$NAME_SPACE"
    echo
fi