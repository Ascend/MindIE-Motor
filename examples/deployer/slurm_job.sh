#!/bin/bash
set -euo pipefail

# slurm_deploy.py replaces this token with the packed ConfigMap.
CONFIGMAP_ARCHIVE_B64="__MOTOR_CONFIGMAP_ARCHIVE_B64__"
SLURM_DEPLOYMENT_PATH="${SLURM_DISTRIBUTION_PATH}/${SLURM_DEPLOYMENT_ID}"
SLURM_DEPLOYMENT_LOG_PATH="${SLURM_LOG_PATH}/${SLURM_DEPLOYMENT_ID}"
SLURM_LOCAL_WORKER_SCRIPT="${SLURM_DEPLOYMENT_PATH}/mindie_motor_${SLURM_JOB_ID}.sh"

# The first process creates the deployment directory on every assigned node.
# It then copies this script to every node and starts one worker on each node.
if [ "${1:-}" != "__worker" ]; then
  export LC_CTYPE=C.UTF-8
  export ROLE="$1"
  export JOB_NAME="${2:-}"
  srun --ntasks-per-node=1 mkdir -m 700 -p "$SLURM_DEPLOYMENT_PATH" "$SLURM_DEPLOYMENT_LOG_PATH"
  sbcast --force "$0" "$SLURM_LOCAL_WORKER_SCRIPT"
  exec srun --ntasks-per-node=1 bash "$SLURM_LOCAL_WORKER_SCRIPT" __worker
fi

# Job ID and task ID keep workers from using the same ConfigMap on one node.
CONFIGMAP_PATH="${SLURM_DEPLOYMENT_PATH}/mindie_motor_${SLURM_JOB_ID}_${SLURM_PROCID:-0}/configmap"
cleanup_worker() {
  if [ -n "${INSTANCE_NAME:-}" ]; then
    apptainer instance stop "$INSTANCE_NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "${CONFIGMAP_PATH%/configmap}"
  rm -f "$SLURM_LOCAL_WORKER_SCRIPT"
  # The last worker on a node removes the now-empty deployment directory.
  rmdir "$SLURM_DEPLOYMENT_PATH" >/dev/null 2>&1 || true
}
trap cleanup_worker EXIT TERM INT

rm -rf "${CONFIGMAP_PATH%/configmap}"
mkdir -m 700 -p "${CONFIGMAP_PATH%/configmap}"
LOCAL_LOG_FILE="$SLURM_DEPLOYMENT_LOG_PATH/${ROLE}_${SLURM_JOB_ID}_task${SLURM_PROCID:-0}_${SLURMD_NODENAME:-unknown-node}.log"
exec >>"$LOCAL_LOG_FILE" 2>&1

printf '%s' "$CONFIGMAP_ARCHIVE_B64" | base64 --decode | tar -xzf - -C "${CONFIGMAP_PATH%/configmap}"
unset CONFIGMAP_ARCHIVE_B64
if [ ! -f "$CONFIGMAP_PATH/boot.sh" ]; then
  echo "ERROR: failed to unpack ConfigMap into ${CONFIGMAP_PATH%/configmap}" >&2
  exit 1
fi

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

IFACE=$(ip -o addr show scope global up | \
  awk -v ip="$POD_IP" \
    '{addr=$4; sub(/\/.*/, "", addr); if (addr == ip && !found) {iface=$2; found=1}} END {if (found) print iface}')
echo "This is the $ROLE $POD_IP${IFACE:+ iface=$IFACE}."
mkdir -p /root/.cache /root/ascend/log

INSTANCE_NAME="${ROLE}_${SLURM_JOB_ID:-$$}"
RUNTIME_ENV=(
  --env "CONFIGMAP_PATH=$CONFIGMAP_PATH"
  --env "CONFIG_PATH=$CONFIG_PATH"
  --env "ROLE=$ROLE"
  --env "POD_IP=$POD_IP"
  --env "HOST_IP=$POD_IP"
  --env "SGLANG_HOST_IP=$POD_IP"
  --env "JOB_NAME=$JOB_NAME"
  --env "ENGINE_TYPE=$ENGINE_TYPE"
  --env "HCCL_IF_BASE_PORT=5000"
  --env "HCCL_IF_IP=$POD_IP"
)
if [ -n "$IFACE" ]; then
  RUNTIME_ENV+=(
    --env "GLOO_SOCKET_IFNAME=$IFACE"
    --env "HCCL_SOCKET_IFNAME=$IFACE"
    --env "TP_SOCKET_IFNAME=$IFACE"
  )
fi

case "$ROLE" in
  coordinator|controller|encode|prefill|decode|union)
    RUNTIME_ENV+=(
      --env "COORDINATOR_SERVICE=$COORDINATOR_SERVICE"
      --env "CONTROLLER_SERVICE=$CONTROLLER_SERVICE"
    )
    ;;
esac

case "$ROLE" in
  coordinator)
    if [ "$KV_STORE_ENABLED" = "1" ]; then
      RUNTIME_ENV+=(
        --env "KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE"
        --env "KV_STORE_BACKEND=$KV_STORE_BACKEND"
      )
    fi
    if [ "$KV_CONDUCTOR_ENABLED" = "1" ]; then
      RUNTIME_ENV+=(--env "KV_CONDUCTOR_SERVICE=$KV_CONDUCTOR_SERVICE")
    fi
    ;;
  encode|prefill|decode|union)
    if [ "$KV_STORE_ENABLED" = "1" ]; then
      RUNTIME_ENV+=(
        --env "KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE"
        --env "KV_STORE_BACKEND=$KV_STORE_BACKEND"
      )
      if [ "$KV_STORE_BACKEND" = "memcache" ] && [ -n "$MMC_LOCAL_SERVICE_MODE" ]; then
        RUNTIME_ENV+=(--env "MMC_LOCAL_SERVICE_MODE=$MMC_LOCAL_SERVICE_MODE")
      fi
    fi
    if [ "$MF_STORE_ENABLED" = "1" ]; then
      RUNTIME_ENV+=(
        --env "ASCEND_MF_STORE_URL=$ASCEND_MF_STORE_URL"
        --env "ASCEND_MF_TRANSFER_PROTOCOL=$ASCEND_MF_TRANSFER_PROTOCOL"
      )
    fi
    ;;
  kv_store)
    if [ "$KV_STORE_ENABLED" = "1" ]; then
      RUNTIME_ENV+=(
        --env "KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE"
        --env "KV_CACHE_STORE_PORT=$KV_CACHE_STORE_PORT"
        --env "KV_STORE_EVICTION_HIGH_WATERMARK_RATIO=$KV_STORE_EVICTION_HIGH_WATERMARK_RATIO"
        --env "KV_STORE_EVICTION_RATIO=$KV_STORE_EVICTION_RATIO"
        --env "DEFAULT_KV_LEASE_TTL=$DEFAULT_KV_LEASE_TTL"
        --env "KV_STORE_BACKEND=$KV_STORE_BACKEND"
      )
      if [ "$KV_STORE_BACKEND" = "memcache" ]; then
        RUNTIME_ENV+=(
          --env "MMC_CONFIG_STORE_URL=tcp://0.0.0.0:$MMC_CONFIG_STORE_PORT"
          --env "MMC_METRICS_URL=http://0.0.0.0:$MMC_METRICS_PORT"
        )
      fi
    fi
    ;;
  kv_conductor)
    if [ "$KV_CONDUCTOR_ENABLED" = "1" ]; then
      RUNTIME_ENV+=(
        --env "KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE"
        --env "KV_CONDUCTOR_SERVICE=$KV_CONDUCTOR_SERVICE"
        --env "KV_CONDUCTOR_PORT=$KV_CONDUCTOR_PORT"
      )
    fi
    ;;
  mf_store)
    if [ "$MF_STORE_ENABLED" = "1" ]; then
      RUNTIME_ENV+=(--env "ASCEND_MF_STORE_PORT=$ASCEND_MF_STORE_PORT")
    fi
    ;;
esac

apptainer instance start --cleanenv --no-home --writable-tmpfs \
  --bind /usr/local/Ascend/driver:/usr/local/Ascend/driver \
  --bind /usr/local/sbin:/usr/local/sbin \
  --bind "$MODEL_PATH:$MODEL_PATH" \
  --bind /root/.cache:/root/.cache \
  --bind /root/ascend/log:/root/ascend/log \
  --bind "$CONFIGMAP_PATH:$CONFIGMAP_PATH:ro" \
  "$IMAGE_NAME" "$INSTANCE_NAME"

apptainer exec \
  --cleanenv \
  --env "ASCEND_RUNTIME_OPTIONS=NODRV" \
  "${RUNTIME_ENV[@]}" \
  "instance://${INSTANCE_NAME}" \
  bash -c "source ${CONFIGMAP_PATH}/boot.sh"
