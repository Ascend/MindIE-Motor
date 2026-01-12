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


set_common_env

jemalloc_path=$(find /usr/lib /usr/lib64 -maxdepth 2 -type f -name "libjemalloc.so.2" 2>/dev/null | head -n 1)

if [[ -n "$jemalloc_path" ]]; then
    export LD_PRELOAD="${jemalloc_path}:${LD_PRELOAD}"
    echo "jemalloc found at: $jemalloc_path"
    echo "LD_PRELOAD is set successfully."
else
    echo "Warning: libjemalloc.so.2 not found under /usr"
    echo "Please make sure jemalloc is installed."
fi


export LD_LIBRARY_PATH="$MINDIE_LLM_HOME_PATH/lib:$MINDIE_LLM_HOME_PATH/lib/grpc:$MIES_INSTALL_PATH/lib:$MIES_INSTALL_PATH/lib/grpc:$LD_LIBRARY_PATH"
export PYTHONPATH="$MINDIE_LLM_HOME_PATH:$MINDIE_LLM_HOME_PATH/lib:$MIES_INSTALL_PATH/bin:$PYTHONPATH"

mindie_llm_path="$MINDIE_LLM_HOME_PATH"
chmod u+w "${mindie_llm_path}"
py_logs_path="${mindie_llm_path}/logs"
if [ ! -d "${py_logs_path}" ]; then
    mkdir "${py_logs_path}"
fi
chmod 750 "${py_logs_path}"
export MINDIE_LLM_PYTHON_LOG_PATH="${py_logs_path}"

export HSECEASY_PATH="$MIES_INSTALL_PATH/lib"
export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:$MIES_INSTALL_PATH/lib"
CONFIG_DIR="$MIES_INSTALL_PATH/conf"

cd "$MIES_INSTALL_PATH"

if [ $# -eq 0 ]; then
    PWD=$(cd "$(dirname "$0")"; pwd)
    BOOT_SCRIPT_DIR="$PWD"
    unset PWD
    exit_code=2
    if [ ! -n "$APP_TYPE" ]; then
        PYTHONUNBUFFERED=1 python3 "$BOOT_SCRIPT_DIR/get_group_id.py"
        exit_code=$?
    else
        PYTHONUNBUFFERED=1 python3 "$BOOT_SCRIPT_DIR/get_group_id.py"
        if [[ "$APP_TYPE" == *controller* ]]; then
            exit_code=1
        elif [[ "$APP_TYPE" == *coordinator* ]]; then
            exit_code=0
        else
            exit_code=2
        fi
    fi
    if [ $exit_code -eq 2 ]; then
        if [ -n "$CONFIG_FROM_CONFIGMAP_PATH" ]; then
            cp "$CONFIG_FROM_CONFIGMAP_PATH/config.json" "$CONFIG_DIR/config.json"
            cp "$CONFIG_FROM_CONFIGMAP_PATH/node_manager.json" "$CONFIG_DIR/node_manager.json"
            cp "$CONFIG_FROM_CONFIGMAP_PATH/http_client_ctl.json" "$CONFIG_DIR/http_client_ctl.json"
        fi
        export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common"
        python3 /mnt/configmap/file_utils.py "$CANN_INSTALL_PATH/ascend-toolkit/set_env.sh" --permission-mode 555 --max-size 104857600 || exit 1
        source "$CANN_INSTALL_PATH/ascend-toolkit/set_env.sh"
        python3 /mnt/configmap/file_utils.py "$CANN_INSTALL_PATH/atb-models/set_env.sh" --permission-mode 550 --max-size 104857600 || exit 1
        source "$CANN_INSTALL_PATH/atb-models/set_env.sh"
        python3 /mnt/configmap/file_utils.py "$CANN_INSTALL_PATH/nnal/atb/set_env.sh" --permission-mode 555 --max-size 104857600 || exit 1
        source "$CANN_INSTALL_PATH/nnal/atb/set_env.sh"
        export GRPC_POLL_STRATEGY=poll
        export MIES_CONTAINER_IP="$POD_IP"
        export MIES_CONTAINER_MANAGEMENT_IP="$POD_IP"

        HOME_HCCL_PATH=/home/hccl.json

        if [ -n "$MINDIE_LOG_CONFIG_PATH" ] && [ -n "$MODEL_NAME" ] && [ -n "$MODEL_ID" ]; then
            chmod 750 "$MINDIE_LOG_CONFIG_PATH"
            if [ ! -d "$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID" ];then
                mkdir -p -m 750 "$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID"
            fi
            if [ ! -d "$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/mindie" ];then
                mkdir -p -m 750 "$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/mindie"
            fi
            if [ ! -d "$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/ascend_work_path" ];then
                mkdir -p -m 750 "$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/ascend_work_path"
            fi
            if [ ! -d "$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/ascend_cache_path" ];then
                mkdir -p -m 750 "$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/ascend_cache_path"
            fi
            export MINDIE_LOG_PATH="$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/mindie"
            export ASCEND_WORK_PATH="$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/ascend_work_path"
            export ASCEND_CACHE_PATH="$MINDIE_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/ascend_cache_path"
        fi

        if [ -n "$APP_TYPE" ]; then
            while true; do
                RANKTABLEFILE_TMP=/user/serverid/devindex/config/hccl/hccl.json;
                python3 /mnt/configmap/file_utils.py "$RANKTABLEFILE_TMP" --permission-mode 640 --max-size 104857600 || exit 1
                json_string=$(cat "$RANKTABLEFILE_TMP");
                echo "$json_string";
                status=$(echo "$json_string" | grep -o '\"status\":\"[^\"]*' | sed 's/\"status\":\"//');
                echo "$status";
                if [[ "$status" = "completed" ]]; then
                    echo "status is completed";
                    cp /user/serverid/devindex/config/hccl/hccl.json "$HOME_HCCL_PATH"
                    chmod 640 "$HOME_HCCL_PATH"
                    break;
                fi;
                sleep 1;
            done;
        fi
        python3 /mnt/configmap/file_utils.py "$CONFIG_DIR/config.json" --permission-mode 640 --max-size 104857600 || exit 1
        if grep -q '"distDPServerEnabled": true' "$CONFIG_DIR/config.json"; then
            # 分布式
            export MINDIE_MS_GEN_SERVER_PORT=true
            chmod 750 "$CONFIG_DIR"
            touch "$CONFIG_DIR/npu_info.txt"
            chmod 640 "$CONFIG_DIR/npu_info.txt"
            npu-smi info -l > "$CONFIG_DIR/npu_info.txt"
            python3 /mnt/configmap/file_utils.py "$CONFIG_FROM_CONFIGMAP_PATH/gen_config_single_container.py" --permission-mode 550 || exit 1
            PYTHONUNBUFFERED=1 python3 "$CONFIG_FROM_CONFIGMAP_PATH/gen_config_single_container.py" "$CONFIG_DIR" multi-port
            exit_server_num=$?

            export RANKTABLEFILE="$HOME_HCCL_PATH"
            export RANK_TABLE_FILE="$HOME_HCCL_PATH"
            export MIES_RANKTABLEFILE="$HOME_HCCL_PATH"
            unset HCCL_OP_EXPANSION_MODE
            export ATB_WORKSPACE_MEM_ALLOC_ALG_TYPE=3
            export ATB_WORKSPACE_MEM_ALLOC_GLOBAL=1

            if [ "$ROLE" = "d" ]; then
                set_decode_env
                CURRENT_ROLE="decode"
            elif [ "$ROLE" = "p" ]; then
                set_prefill_env
                CURRENT_ROLE="prefill"
            fi
            node_manager "$exit_server_num" "$CURRENT_ROLE" &
            pid=$!
            echo "pull up $CURRENT_ROLE instance, number: $exit_server_num"
        else
            chmod 750 "$CONFIG_DIR"
            python3 /mnt/configmap/file_utils.py "$BOOT_SCRIPT_DIR/update_mindie_server_config.py" --permission-mode 550 || exit 1
            PYTHONUNBUFFERED=1 python3 "$BOOT_SCRIPT_DIR/update_mindie_server_config.py" "$CONFIG_DIR/config.json"

            if [ -n "$DIST_PD_DISAGGREGATION" ]; then
                export RANKTABLEFILE="$HOME_HCCL_PATH"
                export RANK_TABLE_FILE="$HOME_HCCL_PATH"
                export MIES_RANKTABLEFILE="$HOME_HCCL_PATH"
                export ATB_WORKSPACE_MEM_ALLOC_ALG_TYPE=3
                export ATB_WORKSPACE_MEM_ALLOC_GLOBAL=1
                set_prefill_env
            fi
            export HCCL_RDMA_RETRY_CNT=7
            export HCCL_RDMA_TIMEOUT=18
            export HCCL_EXEC_TIMEOUT=60
            if [ ! -n "$APP_TYPE" ]; then
                python3 /mnt/configmap/file_utils.py "./bin/mindieservice_daemon" --permission-mode 550 || exit 1
                ./bin/mindieservice_daemon &
            else
                node_manager &
            fi
            pid=$!
        fi
        wait $pid
        exit_code=$?
        if [ $exit_code -ne 0 ]; then
            echo "Error: mindie daemon exited with code $exit_code"
            exit 1
        fi
        echo "All processes finished successfully."
        exit 0
    fi

    if [ $exit_code -eq 1 ]; then
        if [ -n "$CONFIG_FROM_CONFIGMAP_PATH" ]; then
            cp "$CONFIG_FROM_CONFIGMAP_PATH/ms_controller.json" "$CONFIG_DIR/ms_controller.json"
            chmod 640 "$CONFIG_DIR/ms_controller.json"
            cp "$CONFIG_FROM_CONFIGMAP_PATH/http_client_ctl.json" "$CONFIG_DIR/http_client_ctl.json"
            chmod 640 "$CONFIG_DIR/http_client_ctl.json"
        fi
        cp "$GLOBAL_RANK_TABLE_FILE_PATH" "$MIES_INSTALL_PATH"
        export GLOBAL_RANK_TABLE_FILE_PATH="$MIES_INSTALL_PATH/global_ranktable.json"
        chmod 640 "$GLOBAL_RANK_TABLE_FILE_PATH"
        export MINDIE_MS_CONTROLLER_CONFIG_FILE_PATH="$CONFIG_DIR/ms_controller.json"
        export MINDIE_LOG_PATH="$CONTROLLER_LOG_CONFIG_PATH"
        if [ -n "$CONTROLLER_LOG_CONFIG_PATH" ] && [ -n "$MODEL_NAME" ] && [ -n "$MODEL_ID" ]; then
            chmod 750 "$CONTROLLER_LOG_CONFIG_PATH"
            if [ ! -d "$CONTROLLER_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID" ];then
                mkdir -p -m 750 "$CONTROLLER_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID"
            fi
            export MINDIE_LOG_PATH="$CONTROLLER_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/mindie"
        fi
        set_controller_env
        om_adapter Controller &
        python3 /mnt/configmap/file_utils.py "./bin/ms_controller" --permission-mode 550 || exit 1
        ./bin/ms_controller
    fi

    if [ $exit_code -eq 0 ]; then
        if [ -n "$CONFIG_FROM_CONFIGMAP_PATH" ]; then
            cp "$CONFIG_FROM_CONFIGMAP_PATH/ms_controller.json" "$CONFIG_DIR/ms_controller.json"
            chmod 640 "$CONFIG_DIR/ms_controller.json"
            cp "$CONFIG_FROM_CONFIGMAP_PATH/ms_coordinator.json" "$CONFIG_DIR/ms_coordinator.json"
            chmod 640 "$CONFIG_DIR/ms_coordinator.json"
            cp "$CONFIG_FROM_CONFIGMAP_PATH/http_client_ctl.json" "$CONFIG_DIR/http_client_ctl.json"
            chmod 640 "$CONFIG_DIR/http_client_ctl.json"
        fi
        export MINDIE_MS_COORDINATOR_CONFIG_FILE_PATH="$CONFIG_DIR/ms_coordinator.json"
        export MINDIE_LOG_PATH="$COORDINATOR_LOG_CONFIG_PATH"
        if [ -n "$COORDINATOR_LOG_CONFIG_PATH" ] && [ -n "$MODEL_NAME" ] && [ -n "$MODEL_ID" ]; then
            chmod 750 "$COORDINATOR_LOG_CONFIG_PATH"
            if [ ! -d "$COORDINATOR_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID" ];then
                mkdir -p -m 750 "$COORDINATOR_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID"
            fi
            export MINDIE_LOG_PATH="$COORDINATOR_LOG_CONFIG_PATH/$MODEL_NAME/$MODEL_ID/mindie"
        fi
        set_coordinator_env
        if [ -n "$SERVICE_PROF_CONFIG_PATH" ]; then
            export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/usr/local/Ascend/ascend-toolkit/latest/$(arch)-linux/devlib/linux/$(arch)"
            python3 /mnt/configmap/file_utils.py "$CANN_INSTALL_PATH/ascend-toolkit/set_env.sh" --permission-mode 555 || exit 1
            source $CANN_INSTALL_PATH/ascend-toolkit/set_env.sh
        fi
        om_adapter Coordinator &
        python3 /mnt/configmap/file_utils.py "./bin/ms_coordinator" --permission-mode 550 || exit 1
        ./bin/ms_coordinator "$POD_IP" 1025 "$POD_IP" 1026
    fi
elif [ $# -eq 1 ]; then
    if [[ "$1" == "single_container" ]]; then
        if [ -n "$CONFIG_FROM_CONFIG_FILE_PATH" ]; then
            cp -r "$CONFIG_FROM_CONFIG_FILE_PATH/..data/"* "$CONFIG_DIR/"
            chmod 640 "$CONFIG_DIR/"*
            if [ "$MINDIE_MS_GEN_SERVER_PORT" == "false" ]; then
                cp "$CONFIG_DIR/config1.json" "$CONFIG_DIR/config.json"
                chmod 640 "$CONFIG_DIR/config.json"
            fi
        fi

        export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:$CANN_INSTALL_PATH/driver/lib64/driver:$CANN_INSTALL_PATH/driver/lib64/common"

        # update mindie server config
        chmod 750 "$CONFIG_DIR"
        touch "$CONFIG_DIR/npu_info.txt"
        chmod 640 "$CONFIG_DIR/npu_info.txt"
        npu-smi info -l > "$CONFIG_DIR/npu_info.txt"
        python3 /mnt/configmap/file_utils.py "$CONFIG_FROM_CONFIGMAP_PATH/gen_config_single_container.py" --permission-mode 550 || exit 1
        PYTHONUNBUFFERED=1 python3 "$CONFIG_FROM_CONFIGMAP_PATH/gen_config_single_container.py" "$CONFIG_DIR"
        exit_code=$?
        if [ $exit_code -eq 1 ]; then
            echo "Error: failed to update mindie server config."
            exit 1
        fi

        python3 /mnt/configmap/file_utils.py "$MIES_INSTALL_PATH/global_ranktable.json" --permission-mode 640 --max-size 104857600 || exit 1
        global_rank_table_content=$(cat "$MIES_INSTALL_PATH/global_ranktable.json")
        server_num=$(echo "$global_rank_table_content" | grep -o '"server_count": *[^,}]*' | head -n 1 | sed 's/"server_count": *\(.*\)/\1/')
        server_num=$(echo "$server_num" | tr -cd '[:digit:]')
        server_num=$(expr "$server_num" + 0)
        echo "server_num: $server_num"

        #pull up all server
        python3 /mnt/configmap/file_utils.py "$CANN_INSTALL_PATH/ascend-toolkit/set_env.sh" --permission-mode 555 || exit 1
        source "$CANN_INSTALL_PATH/ascend-toolkit/set_env.sh"
        python3 /mnt/configmap/file_utils.py "$CANN_INSTALL_PATH/atb-models/set_env.sh" --permission-mode 550 || exit 1
        source "$CANN_INSTALL_PATH/atb-models/set_env.sh"
        python3 /mnt/configmap/file_utils.py "$CANN_INSTALL_PATH/nnal/atb/set_env.sh" --permission-mode 555 || exit 1
        source "$CANN_INSTALL_PATH/nnal/atb/set_env.sh"
        export GRPC_POLL_STRATEGY=poll
        for ((i=1; i<$server_num + 1; i++)); do
            export ATB_LLM_HCCL_ENABLE=1
            export PD_MODE=0
            export HCCL_OP_EXPANSION_MODE="AIV"
            export ATB_SHARE_MEMORY_NAME_SUFFIX="$i"
            export HCCL_RDMA_RETRY_CNT=7
            export HCCL_RDMA_TIMEOUT=18
            export HCCL_EXEC_TIMEOUT=60
            sleep 1
            python3 /mnt/configmap/file_utils.py "./bin/mindieservice_daemon" --permission-mode 550 || exit 1
            ./bin/mindieservice_daemon --config-file "$CONFIG_DIR/config$i.json" &
        done

        # pull up coordinator
        export MINDIE_MS_COORDINATOR_CONFIG_FILE_PATH="$CONFIG_DIR/ms_coordinator.json"
        om_adapter Coordinator &
        python3 /mnt/configmap/file_utils.py "./bin/ms_coordinator" --permission-mode 550 || exit 1
        ./bin/ms_coordinator "$POD_IP" 1025 &

        # pull up controller
        export GLOBAL_RANK_TABLE_FILE_PATH="$MIES_INSTALL_PATH/global_ranktable.json"
        export MINDIE_MS_CONTROLLER_CONFIG_FILE_PATH="$CONFIG_DIR/ms_controller.json"
        om_adapter Controller &
        python3 /mnt/configmap/file_utils.py "./bin/ms_controller" --permission-mode 550 || exit 1
        ./bin/ms_controller

    fi
fi