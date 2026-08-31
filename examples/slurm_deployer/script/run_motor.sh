#!/bin/bash
set -euo pipefail

# 优先使用显式指定的 POD_IP; 否则从本机活动网卡中选择地址.
# 双栈环境优先 IPv4, IPv4 不可用时使用全局 IPv6 地址.
if [ -z "${POD_IP:-}" ]; then
  POD_IP=$(ip -o -4 addr show scope global up | \
    awk '$2 != "lo" && !found {sub(/\/.*/, "", $4); first=$4; found=1} END {if (found) print first}')
fi
if [ -z "${POD_IP:-}" ]; then
  POD_IP=$(ip -o -6 addr show scope global up | \
    awk '$2 != "lo" && !found {sub(/\/.*/, "", $4); first=$4; found=1} END {if (found) print first}')
fi
if [ -z "${POD_IP:-}" ]; then
  echo "ERROR: failed to detect a global IPv4 or IPv6 address" >&2
  exit 1
fi

export POD_IP
export HOST_IP="$POD_IP"
export SGLANG_HOST_IP="$POD_IP"
# 根据 POD_IP 的地址族查找对应网卡, IPv4 和 IPv6 均适用.
IFACE=$(ip -o addr show scope global up | \
  awk -v ip="$POD_IP" '{addr=$4; sub(/\/.*/, "", addr); if (addr == ip && !found) {iface=$2; found=1}} END {if (found) print iface}')
if [ -n "$IFACE" ]; then
  export GLOO_SOCKET_IFNAME="$IFACE"
  export HCCL_SOCKET_IFNAME="$IFACE"
  export TP_SOCKET_IFNAME="$IFACE"
fi
export HCCL_IF_IP="$POD_IP"

DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_CONFIG_DIR="${DEPLOY_CONFIG_DIR:-$DEPLOY_ROOT/conf}"

echo "This is the $ROLE $POD_IP${IFACE:+ iface=$IFACE}."

mkdir -p /root/.cache /root/ascend/log

# 实例名带上 SLURM_JOB_ID, 避免同节点按 ROLE 重跑时撞名; 作业结束时停止实例.
INSTANCE_NAME="${ROLE}_${SLURM_JOB_ID:-$$}"
SERVICE_RUNTIME_ENV=()
case "$ROLE" in
  coordinator|controller|encode|prefill|decode|union)
    SERVICE_RUNTIME_ENV=(
      --env "COORDINATOR_SERVICE=$COORDINATOR_SERVICE"
      --env "COORDINATOR_INFER_SERVICE=$COORDINATOR_INFER_SERVICE"
      --env "COORDINATOR_OBS_SERVICE=$COORDINATOR_OBS_SERVICE"
      --env "CONTROLLER_SERVICE=$CONTROLLER_SERVICE"
    )
    ;;
esac
KV_RUNTIME_ENV=()
MF_RUNTIME_ENV=()
NETWORK_RUNTIME_ENV=(--env "HCCL_IF_IP=$HCCL_IF_IP")
if [ -n "${GLOO_SOCKET_IFNAME:-}" ]; then
  NETWORK_RUNTIME_ENV+=(--env "GLOO_SOCKET_IFNAME=$GLOO_SOCKET_IFNAME")
fi
if [ -n "${HCCL_SOCKET_IFNAME:-}" ]; then
  NETWORK_RUNTIME_ENV+=(--env "HCCL_SOCKET_IFNAME=$HCCL_SOCKET_IFNAME")
fi
if [ -n "${TP_SOCKET_IFNAME:-}" ]; then
  NETWORK_RUNTIME_ENV+=(--env "TP_SOCKET_IFNAME=$TP_SOCKET_IFNAME")
fi
case "$ROLE" in
  coordinator)
    if [ "$KV_STORE_ENABLED" = "1" ]; then
      KV_RUNTIME_ENV=(
        --env "KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE"
        --env "KV_STORE_BACKEND=$KV_STORE_BACKEND"
      )
    fi
    if [ "$KV_CONDUCTOR_ENABLED" = "1" ]; then
      KV_RUNTIME_ENV+=(--env "KV_CONDUCTOR_SERVICE=$KV_CONDUCTOR_SERVICE")
    fi
    ;;
  encode|prefill|decode|union)
    if [ "$KV_STORE_ENABLED" = "1" ]; then
      KV_RUNTIME_ENV=(
        --env "KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE"
        --env "KV_STORE_BACKEND=$KV_STORE_BACKEND"
      )
      if [ "$KV_STORE_BACKEND" = "memcache" ] && [ -n "$MMC_LOCAL_SERVICE_MODE" ]; then
        KV_RUNTIME_ENV+=(--env "MMC_LOCAL_SERVICE_MODE=$MMC_LOCAL_SERVICE_MODE")
      fi
    fi
    if [ "$MF_STORE_ENABLED" = "1" ]; then
      MF_RUNTIME_ENV=(
        --env "ASCEND_MF_STORE_URL=$ASCEND_MF_STORE_URL"
        --env "ASCEND_MF_TRANSFER_PROTOCOL=$ASCEND_MF_TRANSFER_PROTOCOL"
      )
    fi
    ;;
  kv_store)
    if [ "$KV_STORE_ENABLED" = "1" ]; then
      KV_RUNTIME_ENV=(
        --env "KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE"
        --env "KV_CACHE_STORE_PORT=$KV_CACHE_STORE_PORT"
        --env "KV_STORE_EVICTION_HIGH_WATERMARK_RATIO=$KV_STORE_EVICTION_HIGH_WATERMARK_RATIO"
        --env "KV_STORE_EVICTION_RATIO=$KV_STORE_EVICTION_RATIO"
        --env "DEFAULT_KV_LEASE_TTL=$DEFAULT_KV_LEASE_TTL"
        --env "KV_STORE_BACKEND=$KV_STORE_BACKEND"
      )
      if [ "$KV_STORE_BACKEND" = "memcache" ]; then
        KV_RUNTIME_ENV+=(
          --env "MMC_CONFIG_STORE_URL=tcp://0.0.0.0:$MMC_CONFIG_STORE_PORT"
          --env "MMC_METRICS_URL=http://0.0.0.0:$MMC_METRICS_PORT"
        )
      fi
    fi
    ;;
  kv_conductor)
    if [ "$KV_CONDUCTOR_ENABLED" = "1" ]; then
      KV_RUNTIME_ENV=(
        --env "KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE"
        --env "KV_CONDUCTOR_SERVICE=$KV_CONDUCTOR_SERVICE"
        --env "KV_CONDUCTOR_PORT=$KV_CONDUCTOR_PORT"
      )
    fi
    ;;
  mf_store)
    if [ "$MF_STORE_ENABLED" = "1" ]; then
      MF_RUNTIME_ENV=(--env "ASCEND_MF_STORE_PORT=$ASCEND_MF_STORE_PORT")
    fi
    ;;
esac
cleanup_instance() {
  apptainer instance stop "$INSTANCE_NAME" >/dev/null 2>&1 || true
}
trap cleanup_instance EXIT TERM INT

apptainer instance start --cleanenv --no-home --writable-tmpfs --no-mount tmp \
--bind /usr/local/Ascend/driver:/usr/local/Ascend/driver \
--bind /usr/local/sbin:/usr/local/sbin \
--bind "$MODEL_PATH:$MODEL_PATH" \
--bind /root/.cache:/root/.cache \
--bind /root/ascend/log:/root/ascend/log \
--bind "$DEPLOY_CONFIG_DIR:/conf:ro" \
--bind "$DEPLOY_ROOT/script/prepare.sh:/slurm_prepare.sh:ro" \
"$IMAGE_NAME" "$INSTANCE_NAME"

apptainer exec \
  --env "CONFIGMAP_PATH=$CONFIGMAP_PATH" \
  "instance://${INSTANCE_NAME}" \
  bash /slurm_prepare.sh

apptainer exec \
--env "ASCEND_RUNTIME_OPTIONS=NODRV" \
--env "CONFIGMAP_PATH=$CONFIGMAP_PATH" \
--env "CONFIG_PATH=$CONFIG_PATH" \
--env "ROLE=$ROLE" \
--env "POD_IP=$POD_IP" \
--env "HOST_IP=$HOST_IP" \
--env "SGLANG_HOST_IP=$SGLANG_HOST_IP" \
--env "JOB_NAME=$JOB_NAME" \
"${SERVICE_RUNTIME_ENV[@]}" \
"${KV_RUNTIME_ENV[@]}" \
"${MF_RUNTIME_ENV[@]}" \
--env "HCCL_IF_BASE_PORT=5000" \
"${NETWORK_RUNTIME_ENV[@]}" \
"instance://${INSTANCE_NAME}" \
bash -c "source ${CONFIGMAP_PATH}/boot.sh"
