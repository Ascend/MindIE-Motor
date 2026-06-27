#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

MOTOR_PATCH_ROOT="/tmp/motor/examples/deployer/patch"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Current node role: ROLE=$ROLE"

set_common_env

apply_openssl_gen_cert() {
    local ca_path=$1
    local base_cert_path=$2
    local cert_names=$3
    local gen_cert_script=$4
    local ca_password=${5:-1234qwer}
    local cert_password=${6:-5678asdf}

    if [ ! -f "$gen_cert_script" ]; then
        echo "Error: Certificate generation script not found: $gen_cert_script"
        return 1
    fi

    if [ -z "$cert_names" ]; then
        echo "Error: cert_names parameter is required"
        echo "Usage: apply_openssl_gen_cert <ca_path> <base_cert_path> <cert_names> <gen_cert_script> [ca_password] [cert_password]"
        echo "Example: apply_openssl_gen_cert /path/to/ca /path/to/security \"infer mgmt etcd clusterd\" /path/to/openssl_gen_cert.sh"
        return 1
    fi

    for cert_name in $cert_names; do
        local cert_path="${base_cert_path}/${cert_name}"
        echo "Generating certificate for: $cert_name"
        echo "Certificate path: $cert_path"

        mkdir -p "$cert_path"

        cat > "${cert_path}/key_pwd.txt" <<- EOF
		${cert_password}
		EOF

        cp "$ca_path/ca.pem" "$ca_path/ca.key.pem" "$cert_path"

        bash "$gen_cert_script" "$ca_path" "$cert_path" "$ca_password" "$cert_password"

        if [ $? -ne 0 ]; then
            echo "Error: Failed to generate certificate for $cert_name"
            return 1
        fi

        echo "Certificate generated successfully for: $cert_name"
        echo "---"
    done

    echo "All certificates generated successfully!"
}

setup_tls_certificates() {
    CA_PATH="${CA_PATH:-/mnt/cert_scripts/ca}"
    BASE_CERT_PATH="${BASE_CERT_PATH:-/usr/local/Ascend/pyMotor/conf/security}"
    CERT_NAMES="${CERT_NAMES:-infer mgmt}"
    GEN_CERT_SCRIPT="${GEN_CERT_SCRIPT:-/mnt/cert_scripts/openssl_gen_cert.sh}"

    if [ ! -f "$GEN_CERT_SCRIPT" ]; then
        echo "Error: Certificate generation script not found: $GEN_CERT_SCRIPT"
        echo "Please copy openssl_gen_cert.sh to the specified path or set GEN_CERT_SCRIPT environment variable"
        return 1
    fi

    if [ ! -f "$CA_PATH/ca.pem" ] || [ ! -f "$CA_PATH/ca.key.pem" ]; then
        echo "Error: CA certificate not found at $CA_PATH"
        echo "Please generate CA certificate first:"
        echo "  bash /mnt/cert_scripts/openssl_gen_ca.sh /mnt/cert_scripts/ca/"
        echo "Or set CA_PATH environment variable to the correct CA certificate path"
        return 1
    fi

    echo "TLS is enabled, generating certificates..."
    echo "CA_PATH: $CA_PATH"
    echo "BASE_CERT_PATH: $BASE_CERT_PATH"
    echo "CERT_NAMES: $CERT_NAMES"
    echo "GEN_CERT_SCRIPT: $GEN_CERT_SCRIPT"
    apply_openssl_gen_cert "$CA_PATH" "$BASE_CERT_PATH" "$CERT_NAMES" "$GEN_CERT_SCRIPT"
}

if [ -n "$ENABLE_GEN_CERT" ] && [ "$ENABLE_GEN_CERT" = "true" ]; then
    setup_tls_certificates
fi

setup_motor_log_path() {
    if [ -n "$MOTOR_LOG_ROOT_PATH" ] && [ -n "$MODEL_NAME" ] && [ -n "$SERVICE_ID" ]; then
        chmod 750 "$MOTOR_LOG_ROOT_PATH"
        if [ ! -d "$MOTOR_LOG_ROOT_PATH/$MODEL_NAME/$SERVICE_ID/motor" ]; then
            mkdir -p -m 750 "$MOTOR_LOG_ROOT_PATH/$MODEL_NAME/$SERVICE_ID/motor"
        fi
        export MOTOR_LOG_PATH="$MOTOR_LOG_ROOT_PATH/$MODEL_NAME/$SERVICE_ID/motor"
    fi
}

setup_ascend_work_path() {
    if [ -n "$MOTOR_LOG_ROOT_PATH" ] && [ -n "$MODEL_NAME" ] && [ -n "$SERVICE_ID" ]; then
        chmod 750 "$MOTOR_LOG_ROOT_PATH"
        if [ ! -d "$MOTOR_LOG_ROOT_PATH/$MODEL_NAME/$SERVICE_ID/ascend_work_path" ];then
            mkdir -p -m 750 "$MOTOR_LOG_ROOT_PATH/$MODEL_NAME/$SERVICE_ID/ascend_work_path"
        fi
        export ASCEND_WORK_PATH="$MOTOR_LOG_ROOT_PATH/$MODEL_NAME/$SERVICE_ID/ascend_work_path"
    fi
}

setup_ascend_cache_path() {
    if [ -n "$MOTOR_LOG_ROOT_PATH" ] && [ -n "$MODEL_NAME" ] && [ -n "$SERVICE_ID" ]; then
        chmod 750 "$MOTOR_LOG_ROOT_PATH"
        if [ ! -d "$MOTOR_LOG_ROOT_PATH/$MODEL_NAME/$SERVICE_ID/ascend_cache_path" ];then
            mkdir -p -m 750 "$MOTOR_LOG_ROOT_PATH/$MODEL_NAME/$SERVICE_ID/ascend_cache_path"
        fi
        export ASCEND_CACHE_PATH="$MOTOR_LOG_ROOT_PATH/$MODEL_NAME/$SERVICE_ID/ascend_cache_path"
    fi
}

apply_shuffle_safetensors_patch() {
    local patch_script="${MOTOR_PATCH_ROOT}/patch_apply_shuffle_safetensors.py"
    if [ ! -f "$patch_script" ]; then
        echo "Warning: shuffle safetensors patch script not found: $patch_script"
        return 0
    fi
    python3 "$patch_script"
}

setup_jemalloc() {
    jemalloc_path=$(find /usr -type f -name "libjemalloc.so.2" 2>/dev/null | head -n 1)
    if [[ -n "$jemalloc_path" ]]; then
        export LD_PRELOAD="${jemalloc_path}:${LD_PRELOAD}"
        echo "jemalloc found at: $jemalloc_path"
        echo "LD_PRELOAD is set successfully."
    else
        echo "Warning: libjemalloc.so.2 not found under /usr"
        echo "Please make sure jemalloc is installed."
    fi
}

USER_CONFIG_FILE="$CONFIGMAP_PATH/user_config.json"
export USER_CONFIG_PATH="$USER_CONFIG_FILE"

mkdir "$CONFIG_PATH" -p
chmod 750 "$CONFIG_PATH"

USER_CONFIG_DST="$CONFIG_PATH/user_config.json"
CONFIG_SYNC_INTERVAL="${CONFIG_SYNC_INTERVAL:-10}"
CONFIG_SYNC_PID_FILE="$CONFIG_PATH/user_config_sync.pid"

sync_user_config() {
    if [ -f "$USER_CONFIG_FILE" ]; then
        if [ ! -f "$USER_CONFIG_DST" ] || ! cmp -s "$USER_CONFIG_FILE" "$USER_CONFIG_DST"; then
            cp -f "$USER_CONFIG_FILE" "$USER_CONFIG_DST"
            chmod 640 "$USER_CONFIG_DST"
        fi
        export USER_CONFIG_PATH="$USER_CONFIG_DST"
    else
        export USER_CONFIG_PATH="$USER_CONFIG_FILE"
    fi
}

sync_user_config
if [ -f "$USER_CONFIG_FILE" ]; then
    if [ -f "$CONFIG_SYNC_PID_FILE" ] && kill -0 "$(cat "$CONFIG_SYNC_PID_FILE")" 2>/dev/null; then
        echo "Config sync loop already running (pid=$(cat "$CONFIG_SYNC_PID_FILE"))"
    else
        (
            while true; do
                sleep "$CONFIG_SYNC_INTERVAL"
                sync_user_config
            done
        ) &
        echo "$!" > "$CONFIG_SYNC_PID_FILE"
    fi
fi

if [ "$SAVE_CORE_DUMP_FILE_ENABLE" = "1" ]; then
    ulimit -c 31457280
    mkdir -p /var/coredump
    chmod 700 /var/coredump
    sysctl -w kernel.core_pattern=/var/coredump/core.%e.%p.%t
else
    ulimit -c 0
fi

set_cann_env() {
    export LD_LIBRARY_PATH="$LD_LIBRARY_PATH:/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common"
    source "$CANN_INSTALL_PATH/ascend-toolkit/set_env.sh"
    source "$CANN_INSTALL_PATH/nnal/atb/set_env.sh"
}

_motor_deploy_hardware_type() {
    local cfg=""
    if [ -n "$USER_CONFIG_PATH" ] && [ -f "$USER_CONFIG_PATH" ]; then
        cfg="$USER_CONFIG_PATH"
    elif [ -n "$USER_CONFIG_FILE" ] && [ -f "$USER_CONFIG_FILE" ]; then
        cfg="$USER_CONFIG_FILE"
    fi
    if [ -z "$cfg" ]; then
        echo "Error: Config file missing" >&2
        return
    fi
    python3 -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); print(d.get('motor_deploy_config',{}).get('hardware_type',''))" "$cfg" 2>/dev/null || echo ""
}

is_a5_hardware() {
    local hw_type
    hw_type="$(_motor_deploy_hardware_type)"
    case "$hw_type" in
        350-Atlas-8|350-Atlas-16|350-Atlas-4p-8|350-Atlas-4p-16|850-Atlas-8p-8|850-SuperPod-Atlas-8|950-SuperPod-Atlas-8)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

set_a5_engine_env() {
    local if_name ip
    if_name=$(awk '$2 == "00000000" {print $1; exit}' /proc/net/route)
    ip="${HOST_IP:-$POD_IP}"

    if [ -z "$if_name" ]; then
        echo "Warning: failed to detect default route interface from /proc/net/route, skip GLOO/TP/HCCL socket ifname env" >&2
        unset GLOO_SOCKET_IFNAME TP_SOCKET_IFNAME HCCL_SOCKET_IFNAME
    else
        export GLOO_SOCKET_IFNAME="$if_name"
        export TP_SOCKET_IFNAME="$if_name"
        export HCCL_SOCKET_IFNAME="$if_name"
    fi

    if [ -z "$ip" ]; then
        echo "Warning: HOST_IP and POD_IP are both empty, skip HCCL_IF_IP env" >&2
        unset HCCL_IF_IP
    else
        export HCCL_IF_IP="$ip"
    fi

    export PATH="$PATH:/usr/local/go/bin"
    export LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH:-}"
    export ASCEND_LOCAL_COMM_RES_PATH="${ASCEND_LOCAL_COMM_RES_PATH:-/etc/hixlep}"
}

gen_ranktable_config() {
    if is_a5_hardware; then
        echo "hardware_type is in A5 list, skip gen_ranktable_config (no hccl/ranktable on this platform)"
        return
    fi
    if [ -f "$CONFIGMAP_PATH/hccl_tools.py" ]; then
        echo "Using hccl_tools.py to generate ranktable.json..."
        export HCCL_PATH="$CONFIG_PATH/hccl.json"
        export PATH="/usr/local/Ascend/driver/tools:$PATH"
        PYTHONUNBUFFERED=1 python3 "$CONFIGMAP_PATH/hccl_tools.py" --hccl_path "$HCCL_PATH"
        export RANKTABLE_PATH="$CONFIG_PATH/ranktable.json"
    else
        echo "hccl_tools.py does not exist, skip ranktable generation"
    fi
}

gen_kv_pool_config() {
    if [ -n "$KVP_MASTER_SERVICE" ]; then
        echo "Updating kv cache pool configuration file..."
        export MOONCAKE_CONFIG_PATH=$CONFIG_PATH/kv_cache_pool_config.json
        export LD_LIBRARY_PATH=/usr/local/Ascend/ascend-toolkit/latest/python/site-packages/mooncake:$LD_LIBRARY_PATH
        if [ "$ROLE" = "SINGLE_CONTAINER" ]; then
            KVP_MASTER_SERVICE=$POD_IP
        fi
        python3 "$CONFIGMAP_PATH/mooncake_config.py" pool "$MOONCAKE_CONFIG_PATH" "$USER_CONFIG_PATH"
    fi
}

set_mf_store_env() {
    if [ -n "$ASCEND_MF_STORE_URL" ]; then
        # IPv6 single-stack: accept ASCEND_MF_STORE_URL forms like
        #   tcp://[2001:db8::1]:2379  /  [::1]:2379
        if [[ "$ASCEND_MF_STORE_URL" =~ ^(tcp://)?\[([0-9a-fA-F:]+)\](:([0-9]+))?$ ]]; then
            PROTO="${BASH_REMATCH[1]}"
            HOST="${BASH_REMATCH[2]}"
            PORT="${BASH_REMATCH[4]}"
            echo "HOST is already IPv6 literal: $HOST"
        elif [[ "$ASCEND_MF_STORE_URL" =~ ^(tcp://)?([^:/]+)(:([0-9]+))?$ ]]; then
            PROTO="${BASH_REMATCH[1]}"
            HOST="${BASH_REMATCH[2]}"
            PORT="${BASH_REMATCH[4]}"

            if [[ ! "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
                MAX_RETRY=5
                RETRY_INTERVAL=10
                RETRY_COUNT=0
                MF_STORE_POD_IP=""
                while [ $RETRY_COUNT -lt $MAX_RETRY ]; do
                    MF_STORE_POD_IP=$(getent hosts "$HOST" | awk '{print $1}' | head -n1)

                    if [ -n "$MF_STORE_POD_IP" ]; then
                        break
                    fi

                    RETRY_COUNT=$((RETRY_COUNT+1))
                    echo "resolve $HOST failed, retry $RETRY_COUNT/$MAX_RETRY ..."
                    sleep $RETRY_INTERVAL
                done

                if [ -z "$MF_STORE_POD_IP" ]; then
                    echo "get pod ip error: $HOST"
                    exit 1
                else
                    echo "$HOST pod ip: $MF_STORE_POD_IP"
                    # Wrap resolved IPv6 literals in [] to keep the URL RFC 3986 compliant.
                    if [[ "$MF_STORE_POD_IP" == *:* ]]; then
                        export ASCEND_MF_STORE_URL="${PROTO}[${MF_STORE_POD_IP}]:${PORT}"
                    else
                        export ASCEND_MF_STORE_URL="${PROTO}${MF_STORE_POD_IP}:${PORT}"
                    fi
                fi
            else
                echo "HOST is already IP: $HOST"
            fi
        else
            echo "ASCEND_MF_STORE_URL format invalid: $ASCEND_MF_STORE_URL"
            exit 1
        fi

        echo "ASCEND_MF_STORE_URL: $ASCEND_MF_STORE_URL"
    fi
}
