#!/bin/bash

assert_service_placeholders() {
  local name value
  for name in COORDINATOR_SERVICE CONTROLLER_SERVICE PARTITION; do
    value="${!name-}"
    if [ -z "$value" ] || [[ "$value" == *'<'* || "$value" == *'>'* ]]; then
      echo "ERROR: $name 仍是占位符或为空（当前: ${value:-<empty>}）。请先 export 现场值，或修改 deploy.sh 顶部默认值。" >&2
      return 1
    fi
  done
  if [ "${KV_STORE_ENABLED:-0}" = "1" ] || [ "${KV_CONDUCTOR_ENABLED:-0}" = "1" ]; then
    if [ -z "${KVS_MASTER_SERVICE:-}" ] || [[ "$KVS_MASTER_SERVICE" == *'<'* || "$KVS_MASTER_SERVICE" == *'>'* ]]; then
      echo "ERROR: KVS_MASTER_SERVICE 为空或仍是占位符。启用 KV Store 或 KV Conductor 时请设置服务地址。" >&2
      return 1
    fi
  fi
  if [ "${MF_STORE_ENABLED:-0}" = "1" ] && \
      { [ -z "${MF_STORE_SERVICE:-}" ] || [[ "$MF_STORE_SERVICE" == *'<'* || "$MF_STORE_SERVICE" == *'>'* ]]; }; then
    echo "ERROR: MF_STORE_SERVICE 仍是占位符或为空。sglang 场景请先设置现场地址。" >&2
    return 1
  fi
  if [ "${KV_CONDUCTOR_ENABLED:-0}" = "1" ] && \
      { [ -z "${KV_CONDUCTOR_SERVICE:-}" ] || [[ "$KV_CONDUCTOR_SERVICE" == *'<'* || "$KV_CONDUCTOR_SERVICE" == *'>'* ]]; }; then
    echo "ERROR: KV_CONDUCTOR_SERVICE 为空或仍是占位符。启用 kv_conductor 时请设置服务地址。" >&2
    return 1
  fi
}
