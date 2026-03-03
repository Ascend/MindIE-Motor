#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


# 初始化变量
SCRIPT_MODE=""
SERVICE_TYPE=""
COORDINATOR_IP=""

# 检查是多机还是单机场景
if [[ "$*" =~ single_container ]]; then
    SCRIPT_MODE="single_container"
    COORDINATOR_IP="127.0.0.1"
fi

# 检查是否传入探针类型
if [ -z "$1" ]; then
    echo "Error: Missing probe type. Please provide one of 'startup', 'readiness', or 'liveness'."
    exit 1
fi
PROBE_TYPE=$1
TIMEOUT_SECONDS=${2:-1}
RETRY_TIMES=${3:-0}
cd $MIES_INSTALL_PATH
CURRENT_DIR=$(cd "$(dirname "$0")"; pwd)

export HSECEASY_PATH=$MIES_INSTALL_PATH/lib
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:$MIES_INSTALL_PATH/lib
export MINDIE_UTILS_HTTP_CLIENT_CTL_CONFIG_FILE_PATH=$MIES_INSTALL_PATH/conf/http_client_ctl.json

# 读取各组件port数据
CONFIG_DIR=$MIES_INSTALL_PATH/conf
MINDIE_MS_CONTROLLER_PORT_INFO_PATH="$CONFIG_DIR/ms_controller.json"
MINDIE_MS_CONTROLLER_PORT_KEY_PATH="http_server.port"
MINDIE_MS_CONTROLLER_PORT_TYPE="int"
MINDIE_MS_COORDINATOR_PORT_INFO_PATH="$CONFIG_DIR/ms_coordinator.json"
MINDIE_MS_COORDINATOR_PORT_KEY_PATH="http_config.status_port"
MINDIE_MS_COORDINATOR_PORT_TYPE="str"
MINDIE_MS_COORDINATOR_IP_KEY_PATH="http_config.manage_ip"
MINDIE_MS_COORDINATOR_IP_TYPE="ip"
MINDIE_SERVER_PORT_KEY_PATH="ServerConfig.managementPort"
MINDIE_SERVER_PORT_TYPE="int"

if [[ "$SCRIPT_MODE" == "single_container" ]]; then
    http_server_port=$(python $CURRENT_DIR/get_mies_mgmt_port.py "$MINDIE_MS_CONTROLLER_PORT_INFO_PATH" "$MINDIE_MS_CONTROLLER_PORT_KEY_PATH" "$MINDIE_MS_CONTROLLER_PORT_TYPE")
    manage_port=$(python $CURRENT_DIR/get_mies_mgmt_port.py "$MINDIE_MS_COORDINATOR_PORT_INFO_PATH" "$MINDIE_MS_COORDINATOR_PORT_KEY_PATH" "$MINDIE_MS_COORDINATOR_PORT_TYPE")
    COORDINATOR_IP=$(python $CURRENT_DIR/get_mies_mgmt_port.py "$MINDIE_MS_COORDINATOR_PORT_INFO_PATH" "$MINDIE_MS_COORDINATOR_IP_KEY_PATH" "$MINDIE_MS_COORDINATOR_IP_TYPE")
    json_files=("$CONFIG_DIR/config[0-9]*.json")
    managementPort=()
    for json_file in "${json_files[@]}"; do
        if [[ "$json_file" != "$CONFIG_DIR/config.json" ]]; then
            port=$(python "$CURRENT_DIR/get_mies_mgmt_port.py" "$json_file" "$MINDIE_SERVER_PORT_KEY_PATH" "$MINDIE_SERVER_PORT_TYPE")
            managementPort+=("$port")
        fi
    done

    port_list=()
    port_list+=(${http_server_port})
    port_list+=(${manage_port})
    port_list+=(${managementPort[@]})
    case "$PROBE_TYPE" in
        startup)
            url_list=("/v1/startup" "/v1/startup")
            mies_url="/v2/health/ready"
            ;;
        readiness)
            url_list=("/v1/health" "/v1/readiness")
            mies_url="/v2/health/ready"
            ;;
        liveness)
            url_list=("/v1/health" "/v1/health")
            mies_url="/v2/health/ready"
            ;;
        *)
            echo "Error: Invalid probe type. Please use 'startup', 'readiness', or 'liveness'."
            exit 1
            ;;
    esac
    # 添加n个server url
    for ((i=0; i<${#managementPort[@]}; i++)); do
        url_list+=($mies_url)
    done
else
    group_id=-1
    if [ ! -n "$APP_TYPE" ]; then
        if [[ $MINDIE_SERVER_PROBE_ONLY -ne 1 ]]; then
            PYTHONUNBUFFERED=1 python3 ${CURRENT_DIR}/get_group_id.py
            group_id=$?
            if [ $group_id -eq 255 ]; then
                echo "Error: Getting group id from the global rank table failed."
                exit 1
            fi
        fi
    else
        echo "======APP TYPE EXIST======"
        if [[ $APP_TYPE == *controller* ]]; then
            group_id=1
        elif [[ $APP_TYPE == *coordinator* ]]; then
            group_id=0
        else
            group_id=2
        fi
    fi

    if [[ $MINDIE_SERVER_DISTRIBUTE -eq 1 ]]; then
        PYTHONUNBUFFERED=1 python3 ${CURRENT_DIR}/get_distribute_role.py
        role=$?
        if [ $role -eq 255 ]; then
            echo "Error: Getting group id from the global rank table failed."
            exit 1
        fi
        if [ $role -eq 1 ]; then
            echo "Distributed slave mindie-server skip probe."
            exit 0
        fi
    fi

    # MindIE-Server
    SERVICE_TYPE=
    if [ $group_id -eq 2 ] || [[ $MINDIE_SERVER_PROBE_ONLY -eq 1 ]]; then
        STARTUP_URL=/v2/health/ready
        READINESS_URL=/v2/health/ready
        LIVENESS_URL=/v2/health/ready
        json_file=($CONFIG_DIR/config.json)
        port_list=$(python3 $CURRENT_DIR/get_mies_mgmt_port.py "$json_file" "$MINDIE_SERVER_PORT_KEY_PATH" "$MINDIE_SERVER_PORT_TYPE")
        if [ $port_list -eq -1 ]; then
            echo "get PORT fail"
            exit 1
        fi
        SERVICE_TYPE="mies"
    fi

    # MindIE-MS-Controller
    if [ $group_id -eq 1 ]; then
        STARTUP_URL=/v1/startup
        READINESS_URL=/v1/health
        LIVENESS_URL=/v1/health
        port_list=$(python $CURRENT_DIR/get_mies_mgmt_port.py "$MINDIE_MS_CONTROLLER_PORT_INFO_PATH" "$MINDIE_MS_CONTROLLER_PORT_KEY_PATH" "$MINDIE_MS_CONTROLLER_PORT_TYPE")
    fi

    # MindIE-MS-Coordinator
    if [ $group_id -eq 0 ]; then
        STARTUP_URL=/v1/startup
        READINESS_URL=/v1/readiness
        LIVENESS_URL=/v1/health
        port_list=$(python $CURRENT_DIR/get_mies_mgmt_port.py "$MINDIE_MS_COORDINATOR_PORT_INFO_PATH" "$MINDIE_MS_COORDINATOR_PORT_KEY_PATH" "$MINDIE_MS_COORDINATOR_PORT_TYPE")
    fi
fi

# 根据不同的探针类型执行不同的逻辑
case "$PROBE_TYPE" in
    startup)
        echo "Executing startup probe..."
        # 在这里放置你需要的启动探针逻辑
        # 比如检查某个服务是否已经成功启动
        for ((i=0; i<${#port_list[@]}; i++)); do
            PORT=${port_list[$i]}
            if [[ "$SCRIPT_MODE" == "single_container" ]]; then
                URL=${url_list[$i]}
                if [ $i -eq 0 ]; then
                    $MIES_INSTALL_PATH/bin/http_client_ctl $POD_IP $PORT $URL $TIMEOUT_SECONDS $RETRY_TIMES
                elif [ $i -eq 1 ]; then
                    $MIES_INSTALL_PATH/bin/http_client_ctl $COORDINATOR_IP $PORT $URL $TIMEOUT_SECONDS $RETRY_TIMES
                else
                    $MIES_INSTALL_PATH/bin/http_client_ctl "127.0.0.1" $PORT $URL $TIMEOUT_SECONDS $RETRY_TIMES
                fi
            else
                $MIES_INSTALL_PATH/bin/http_client_ctl $POD_IP $PORT $STARTUP_URL $TIMEOUT_SECONDS $RETRY_TIMES
            fi
            if [ $? -ne 0 ]; then
                echo "Service is not running."
                exit 1
            fi
        done
        echo "Service is running."
        exit 0
        ;;

    readiness)
        echo "Executing readiness probe..."
        # 在这里放置你的就绪探针逻辑
        # 比如检查某个API端点是否可用
        for ((i=0; i<${#port_list[@]}; i++)); do
            PORT=${port_list[$i]}
            if [[ "$SCRIPT_MODE" == "single_container" ]]; then
                URL=${url_list[$i]}
                if [ $i -eq 0 ]; then
                    $MIES_INSTALL_PATH/bin/http_client_ctl $POD_IP $PORT $URL $TIMEOUT_SECONDS $RETRY_TIMES
                elif [ $i -eq 1 ]; then
                    $MIES_INSTALL_PATH/bin/http_client_ctl $COORDINATOR_IP $PORT $URL $TIMEOUT_SECONDS $RETRY_TIMES
                else
                    $MIES_INSTALL_PATH/bin/http_client_ctl "127.0.0.1" $PORT $URL $TIMEOUT_SECONDS $RETRY_TIMES
                fi
            else
                if [ "${SERVICE_TYPE}X" = "miesX" ]; then
                    python3 $CURRENT_DIR/check_npu_status.py
                    if [ $? -ne 0 ]; then
                        echo "Service is not ready."
                        exit 1
                    fi
                fi
                $MIES_INSTALL_PATH/bin/http_client_ctl $POD_IP $PORT $READINESS_URL $TIMEOUT_SECONDS $RETRY_TIMES
            fi
            if [ $? -ne 0 ]; then
                echo "Service is not ready."
                exit 1
            fi
        done
        echo "Service is ready."
        exit 0
        ;;

    liveness)
        echo "Executing liveness probe..."
        # 在这里放置你的存活探针逻辑
        # 比如检查进程是否还在运行
        for ((i=0; i<${#port_list[@]}; i++)); do
            PORT=${port_list[$i]}
            health_status=0
            if [[ "$SCRIPT_MODE" == "single_container" ]]; then
                URL=${url_list[$i]}
                if [ $i -eq 0 ]; then
                    $MIES_INSTALL_PATH/bin/http_client_ctl $POD_IP $PORT $URL $TIMEOUT_SECONDS $RETRY_TIMES
                    if [ $? -ne 0 ]; then
                        health_status=1
                    fi
                elif [ $i -eq 1 ]; then
                    $MIES_INSTALL_PATH/bin/http_client_ctl $COORDINATOR_IP $PORT $URL $TIMEOUT_SECONDS $RETRY_TIMES
                    if [ $? -ne 0 ]; then
                        health_status=1
                    fi
                else
                    $MIES_INSTALL_PATH/bin/http_client_ctl "127.0.0.1" $PORT $URL $TIMEOUT_SECONDS $RETRY_TIMES
                    if [ $? -ne 0 ]; then
                        health_status=1
                    fi
                fi
            else
                $MIES_INSTALL_PATH/bin/http_client_ctl $POD_IP $PORT $LIVENESS_URL $TIMEOUT_SECONDS $RETRY_TIMES
                if [ $? -ne 0 ]; then
                    health_status=1
                fi
                if [ "${SERVICE_TYPE}X" = "miesX" ]; then
                    python3 $CURRENT_DIR/check_npu_status.py
                    if [ $? -ne 0 ]; then
                        echo "Service is not alive."
                        exit 1
                    fi
                fi
            fi
            if [ $health_status -ne 0 ]; then
                echo "Service is not alive."
                exit 1
            fi
        done
        echo "Service is alive."
        exit 0
        ;;

    *)
        echo "Error: Invalid probe type. Please use 'startup', 'readiness', or 'liveness'."
        exit 1
        ;;
esac