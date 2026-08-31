#!/bin/bash

submit_or_rollback() {
  if ! submit_job "$@"; then
    stop_jobs || true
    return 1
  fi
}

submit_engine_instances() {
  local role="$1"
  local label="$2"
  local instances="$3"
  local pod_num="$4"
  local npu_num="$5"
  local name_prefix="$6"
  local i=0

  if [ "$pod_num" -le 0 ] || [ "$npu_num" -le 0 ]; then
    return 0
  fi
  while [ "$i" -lt "$instances" ]; do
    submit_or_rollback "$label instance $i" \
      --export=ALL --partition="$PARTITION" -N "$pod_num" \
      --cpus-per-task=188 --gres="npu:$npu_num" -J "$role" \
      script/srun_motor.sh "$role" "${name_prefix}${i}" || return 1
    i=$((i + 1))
  done
}

start_deployment() {
  assert_service_placeholders || return 1

  echo "COORDINATOR_SERVICE      = $COORDINATOR_SERVICE"
  echo "CONTROLLER_SERVICE       = $CONTROLLER_SERVICE"
  echo "KVS_MASTER_SERVICE       = $KVS_MASTER_SERVICE"
  echo "KV_CONDUCTOR_SERVICE     = $KV_CONDUCTOR_SERVICE"
  echo "MF_STORE_SERVICE         = $MF_STORE_SERVICE"
  echo "ENGINE_TYPE              = $ENGINE_TYPE"
  echo "KV_CONNECTOR             = $KV_CONNECTOR"
  echo "KV_STORE_BACKEND         = $KV_STORE_BACKEND"
  echo "KV_STORE_ENABLED         = $KV_STORE_ENABLED"
  echo "KV_CONDUCTOR_ENABLED     = $KV_CONDUCTOR_ENABLED"
  echo "MF_STORE_ENABLED         = $MF_STORE_ENABLED"

  resolve_management_nodes || return 1
  if [ "$MF_STORE_ENABLED" = "1" ]; then
    export ASCEND_MF_STORE_URL="tcp://$(format_host_for_url "$MF_STORE_SERVICE"):${ASCEND_MF_STORE_PORT}"
  fi

  export LOG_RUN_DIR="logs/$(date +%Y%m%d_%H%M%S)"
  if [ -s "$JOB_ID_FILE" ]; then
    echo "ERROR: 已存在上一轮部署的 Job ID 记录: $JOB_ID_FILE" >&2
    echo "请先执行 bash deploy.sh stop，或确认上一轮作业已处理完毕后再启动。" >&2
    return 1
  fi
  mkdir -p "$(dirname "$JOB_ID_FILE")" "$LOG_RUN_DIR"
  echo "LOG_RUN_DIR              = $LOG_RUN_DIR"

  submit_or_rollback coordinator --export=ALL --partition="$PARTITION" \
    -N 1 -w "$COORDINATOR_NODE" --cpus-per-task=1 -J coordinator \
    script/srun_motor.sh coordinator || return 1
  submit_or_rollback controller --export=ALL --partition="$PARTITION" \
    -N 1 -w "$CONTROLLER_NODE" --cpus-per-task=1 -J controller \
    script/srun_motor.sh controller || return 1

  if [ "$KV_STORE_ENABLED" = "1" ]; then
    submit_or_rollback kv_store --export=ALL --partition="$PARTITION" \
      -N 1 -w "$KVS_STORE_NODE" --cpus-per-task=1 -J kv_store \
      script/srun_motor.sh kv_store || return 1
  fi
  if [ "$KV_CONDUCTOR_ENABLED" = "1" ]; then
    submit_or_rollback kv_conductor --export=ALL --partition="$PARTITION" \
      -N 1 -w "$KV_CONDUCTOR_NODE" --cpus-per-task=1 -J kv_conductor \
      script/srun_motor.sh kv_conductor || return 1
  fi
  if [ "$MF_STORE_ENABLED" = "1" ]; then
    submit_or_rollback mf_store --export=ALL --partition="$PARTITION" \
      -N 1 -w "$MF_STORE_NODE" --cpus-per-task=4 -J mf_store \
      script/srun_motor.sh mf_store || return 1
  fi

  submit_engine_instances union union "$HY_INSTANCES" "$HY_POD_NUM" "$HY_NPU_NUM" u || return 1
  submit_engine_instances encode encode "$E_INSTANCES" "$E_POD_NUM" "$E_NPU_NUM" e || return 1
  submit_engine_instances prefill prefill "$P_INSTANCES" "$P_POD_NUM" "$P_NPU_NUM" p || return 1
  submit_engine_instances decode decode "$D_INSTANCES" "$D_POD_NUM" "$D_NPU_NUM" d || return 1
}
