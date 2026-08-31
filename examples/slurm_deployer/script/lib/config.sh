#!/bin/bash

# 读取 user_config.json, 并生成 Slurm 启动阶段需要的环境变量.

DEPLOY_CONFIG_DIR="${DEPLOY_CONFIG_DIR:-conf}"
USER_CONFIG_FILE="${USER_CONFIG_FILE:-$DEPLOY_CONFIG_DIR/user_config.json}"
ENV_CONFIG_FILE="${ENV_CONFIG_FILE:-$DEPLOY_CONFIG_DIR/env.json}"

require_user_config() {
  if [ ! -f "$USER_CONFIG_FILE" ]; then
    echo "ERROR: $USER_CONFIG_FILE not found (请检查配置目录: $DEPLOY_CONFIG_DIR)" >&2
    return 1
  fi
  if [ ! -f "$ENV_CONFIG_FILE" ]; then
    echo "ERROR: $ENV_CONFIG_FILE not found (请将用户配置文件放入配置目录: $DEPLOY_CONFIG_DIR)" >&2
    return 1
  fi
}

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
  elif command -v python >/dev/null 2>&1; then
    PYTHON=python
  else
    PYTHON=""
  fi
}

get_motor_cfg() {
  local key="$1"
  local default="$2"
  if [ -n "$PYTHON" ]; then
    "$PYTHON" -c '
import json, sys
config = json.load(open(sys.argv[1])).get("motor_deploy_config", {})
value = config.get(sys.argv[2], sys.argv[3])
sys.stdout.write(str(sys.argv[3] if value is None else value))
' "$USER_CONFIG_FILE" "$key" "$default" 2>/dev/null
  else
    sed -n "/motor_deploy_config/,/^  }/s/.*\"$key\"[[:space:]]*:[[:space:]]*//p" \
      "$USER_CONFIG_FILE" | head -n1 | sed 's/,$//; s/^"//; s/"$//'
  fi
}

get_kv_cfg() {
  local key="$1"
  local default="$2"
  if [ -n "$PYTHON" ]; then
    "$PYTHON" -c '
import json, sys
config = json.load(open(sys.argv[1]))
store = config.get("kv_cache_store_config")
if not isinstance(store, dict):
    store = {}
value = store.get(sys.argv[2], sys.argv[3])
sys.stdout.write(str(sys.argv[3] if value is None or value == "" else value))
' "$USER_CONFIG_FILE" "$key" "$default" 2>/dev/null
  else
    echo "$default"
  fi
}

read_deploy_config() {
  IMAGE_NAME=$(get_motor_cfg image_name "")
  MODEL_PATH=$(get_motor_cfg weight_mount_path "")
  HARDWARE_TYPE=$(get_motor_cfg hardware_type "")

  E_INSTANCES=$(get_motor_cfg e_instances_num 1); : "${E_INSTANCES:=1}"
  E_POD_NUM=$(get_motor_cfg single_e_instance_pod_num 1); : "${E_POD_NUM:=1}"
  E_NPU_NUM=$(get_motor_cfg e_pod_npu_num 0); : "${E_NPU_NUM:=0}"
  P_INSTANCES=$(get_motor_cfg p_instances_num 1); : "${P_INSTANCES:=1}"
  P_POD_NUM=$(get_motor_cfg single_p_instance_pod_num 1); : "${P_POD_NUM:=1}"
  P_NPU_NUM=$(get_motor_cfg p_pod_npu_num 0); : "${P_NPU_NUM:=0}"
  D_INSTANCES=$(get_motor_cfg d_instances_num 1); : "${D_INSTANCES:=1}"
  D_POD_NUM=$(get_motor_cfg single_d_instance_pod_num 1); : "${D_POD_NUM:=1}"
  D_NPU_NUM=$(get_motor_cfg d_pod_npu_num 0); : "${D_NPU_NUM:=0}"
  HY_INSTANCES=$(get_motor_cfg hybrid_instances_num 1); : "${HY_INSTANCES:=1}"
  HY_POD_NUM=$(get_motor_cfg single_hybrid_instance_pod_num 1); : "${HY_POD_NUM:=1}"
  HY_NPU_NUM=$(get_motor_cfg hybrid_pod_npu_num 0); : "${HY_NPU_NUM:=0}"
}

read_kv_config() {
  if [ -n "$PYTHON" ]; then
    KV_STORE_ENABLED=$("$PYTHON" -c '
import json, sys
config = json.load(open(sys.argv[1]))
engine = config.get("motor_engine_prefill_config") or config.get("motor_engine_union_config") or {}
transfer = (engine.get("engine_config") or {}).get("kv_transfer_config") or {}
store = config.get("kv_cache_store_config")
print("1" if transfer.get("kv_connector") == "MultiConnector" or (isinstance(store, dict) and store) else "0")
' "$USER_CONFIG_FILE" 2>/dev/null)
    KV_CONNECTOR=$("$PYTHON" -c '
import json, sys
config = json.load(open(sys.argv[1]))
engine = config.get("motor_engine_prefill_config") or config.get("motor_engine_union_config") or {}
transfer = (engine.get("engine_config") or {}).get("kv_transfer_config") or {}
print(transfer.get("kv_connector", "") or "", end="")
' "$USER_CONFIG_FILE" 2>/dev/null)
    ENGINE_TYPE=$("$PYTHON" -c '
import json, sys
config = json.load(open(sys.argv[1]))
engine = config.get("motor_engine_prefill_config") or config.get("motor_engine_union_config") or {}
print(engine.get("engine_type", "") or "", end="")
' "$USER_CONFIG_FILE" 2>/dev/null)
    KV_CONDUCTOR_PORT=$("$PYTHON" -c '
import json, sys
config = json.load(open(sys.argv[1]))
print((config.get("kv_conductor_config") or {}).get("http_server_port", 0) or 0, end="")
' "$USER_CONFIG_FILE" 2>/dev/null)
    KV_STORE_BACKEND=$(get_kv_cfg backend memcache)
    KV_STORE_CONFIG_IS_DICT=$("$PYTHON" -c '
import json, sys
config = json.load(open(sys.argv[1]))
print("1" if isinstance(config.get("kv_cache_store_config"), dict) else "0")
' "$USER_CONFIG_FILE" 2>/dev/null)
  else
    KV_CONNECTOR=$(sed -n 's/.*"kv_connector"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$USER_CONFIG_FILE" | head -n1)
    KV_STORE_ENABLED=0
    if [ "$KV_CONNECTOR" = "MultiConnector" ] || grep -q '"kv_cache_store_config"' "$USER_CONFIG_FILE"; then
      KV_STORE_ENABLED=1
    fi
    ENGINE_TYPE=$(sed -n 's/.*"engine_type"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$USER_CONFIG_FILE" | head -n1)
    KV_CONDUCTOR_PORT=0
    KV_STORE_BACKEND=memcache
    KV_STORE_CONFIG_IS_DICT=0
    if grep -q '"kv_cache_store_config"[[:space:]]*:' "$USER_CONFIG_FILE"; then
      KV_STORE_CONFIG_IS_DICT=1
    fi
  fi

  : "${KV_STORE_ENABLED:=0}"
  : "${KV_CONNECTOR:=}"
  : "${ENGINE_TYPE:=}"
  : "${KV_CONDUCTOR_PORT:=0}"
  : "${KV_STORE_BACKEND:=memcache}"
  : "${KV_STORE_CONFIG_IS_DICT:=0}"
  if [ "$KV_STORE_ENABLED" = "1" ] && [ "$KV_STORE_CONFIG_IS_DICT" != "1" ]; then
    echo "ERROR: 启用 KV Store 时，$USER_CONFIG_FILE 中必须存在字典类型的 kv_cache_store_config。" >&2
    return 1
  fi

  KV_CACHE_STORE_PORT=$(get_kv_cfg port 50088)
  KV_STORE_EVICTION_HIGH_WATERMARK_RATIO=$(get_kv_cfg eviction_high_watermark_ratio "")
  KV_STORE_EVICTION_RATIO=$(get_kv_cfg eviction_ratio "")
  DEFAULT_KV_LEASE_TTL=$(get_kv_cfg default_kv_lease_ttl 11000)
  MMC_CONFIG_STORE_PORT=$(get_kv_cfg config_store_port 50089)
  MMC_METRICS_PORT=$(get_kv_cfg metrics_port "")
  MMC_LOCAL_SERVICE_MODE=$(get_kv_cfg local_service_mode "")
  if [ -z "$MMC_METRICS_PORT" ]; then
    if [ "$KV_STORE_BACKEND" = "mooncake" ]; then
      MMC_METRICS_PORT="$KV_CACHE_STORE_PORT"
    else
      MMC_METRICS_PORT=50090
    fi
  fi

  if [ "$KV_STORE_ENABLED" = "1" ] && [ "$KV_STORE_BACKEND" = "mooncake" ] && \
      { [ -z "$KV_STORE_EVICTION_HIGH_WATERMARK_RATIO" ] || [ -z "$KV_STORE_EVICTION_RATIO" ]; }; then
    echo "ERROR: backend=mooncake 时，$USER_CONFIG_FILE 必须配置 eviction_high_watermark_ratio 和 eviction_ratio。" >&2
    return 1
  fi
  if [ "$KV_STORE_ENABLED" != "1" ]; then
    KV_STORE_BACKEND=""
  fi
}

derive_runtime_flags() {
  KV_CONDUCTOR_ENABLED=0
  [ "$KV_CONDUCTOR_PORT" != "0" ] && KV_CONDUCTOR_ENABLED=1

  MF_STORE_ENABLED=0
  [ "$ENGINE_TYPE" = "sglang" ] && MF_STORE_ENABLED=1

  if [ "$HARDWARE_TYPE" = "800I_A2" ] || [ "$HARDWARE_TYPE" = "800T_A2" ]; then
    ASCEND_MF_TRANSFER_PROTOCOL=device_rdma
  else
    ASCEND_MF_TRANSFER_PROTOCOL=sdma
  fi
}

read_user_config() {
  require_user_config || return 1
  find_python
  read_deploy_config
  read_kv_config || return 1
  derive_runtime_flags

  if [ -z "$IMAGE_NAME" ]; then
    echo "ERROR: 未在 $USER_CONFIG_FILE 中读取到 image_name" >&2
    return 1
  fi
  if [ -z "$MODEL_PATH" ]; then
    echo "ERROR: 未在 $USER_CONFIG_FILE 中读取到 weight_mount_path" >&2
    return 1
  fi

  export IMAGE_NAME MODEL_PATH HARDWARE_TYPE ENGINE_TYPE KV_CONNECTOR
  export E_INSTANCES E_POD_NUM E_NPU_NUM P_INSTANCES P_POD_NUM P_NPU_NUM
  export D_INSTANCES D_POD_NUM D_NPU_NUM HY_INSTANCES HY_POD_NUM HY_NPU_NUM
  export KV_STORE_ENABLED KV_CONDUCTOR_ENABLED MF_STORE_ENABLED KV_STORE_BACKEND
  export KV_CACHE_STORE_PORT KV_STORE_EVICTION_HIGH_WATERMARK_RATIO KV_STORE_EVICTION_RATIO
  export DEFAULT_KV_LEASE_TTL MMC_CONFIG_STORE_PORT MMC_METRICS_PORT MMC_LOCAL_SERVICE_MODE KV_CONDUCTOR_PORT
  export ASCEND_MF_TRANSFER_PROTOCOL
}
