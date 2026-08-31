#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

ACTION="${1:-}"
CONFIG_DIR_ARG="${2:-conf}"
case "$CONFIG_DIR_ARG" in
  /*) export DEPLOY_CONFIG_DIR="$CONFIG_DIR_ARG" ;;
  *) export DEPLOY_CONFIG_DIR="$SCRIPT_DIR/$CONFIG_DIR_ARG" ;;
esac

# 部署前请替换为现场节点地址 / 分区名, 或通过环境变量覆盖 (export COORDINATOR_SERVICE=...)
export COORDINATOR_SERVICE="${COORDINATOR_SERVICE:-<coordinator-ip>}"
export COORDINATOR_INFER_SERVICE="${COORDINATOR_INFER_SERVICE:-$COORDINATOR_SERVICE}"
export COORDINATOR_OBS_SERVICE="${COORDINATOR_OBS_SERVICE:-$COORDINATOR_SERVICE}"
export CONTROLLER_SERVICE="${CONTROLLER_SERVICE:-<controller-ip>}"
export KVS_MASTER_SERVICE="${KVS_MASTER_SERVICE:-<kvs-master-ip>}"
export KV_CONDUCTOR_SERVICE="${KV_CONDUCTOR_SERVICE:-<kv-conductor-ip>}"
export MF_STORE_SERVICE="${MF_STORE_SERVICE:-<mf-store-ip>}"
# K8s deployer 当前也使用代码默认端口, 而不是 user_config 字段.
export ASCEND_MF_STORE_PORT="${ASCEND_MF_STORE_PORT:-50089}"
PARTITION="${PARTITION:-<partition>}"

export CONFIGMAP_PATH=/configmap
export CONFIG_PATH=/usr/local/Ascend/pyMotor/conf

JOB_ID_FILE="logs/.slurm_job_ids"

source "$SCRIPT_DIR/script/lib/config.sh"
source "$SCRIPT_DIR/script/lib/validation.sh"
source "$SCRIPT_DIR/script/lib/slurm.sh"
source "$SCRIPT_DIR/script/lib/jobs.sh"
source "$SCRIPT_DIR/script/lib/lifecycle.sh"
source "$SCRIPT_DIR/script/lib/start.sh"

show_menu() {
  echo ""
  echo "=========================================="
  echo "  deploy.sh - 请选择要执行的动作:"
  echo "    start    启动全部服务"
  echo "    stop     停止本次部署作业"
  echo "    clean    清理日志与临时文件"
  echo "=========================================="
  echo ""
}

resolve_action() {
  local action="${1:-}"
  while :; do
    case "$action" in
      start|stop|clean)
        printf '%s' "$action"
        return 0
        ;;
    esac
    show_menu
    printf '请输入动作名称 (start/stop/clean): '
    if ! read -r action; then
      echo "输入结束，退出。" >&2
      return 1
    fi
  done
}

main() {
  local action
  action=$(resolve_action "${1:-}") || return 1
  case "$action" in
    start)
      read_user_config || return 1
      start_deployment
      ;;
    stop)
      stop_jobs
      ;;
    clean)
      clean_deployment
      ;;
  esac
}

main "${1:-}"
