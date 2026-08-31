#!/bin/bash

format_host_for_url() {
  local host="$1"
  if [[ "$host" == \[*\] ]]; then
    printf '%s' "$host"
  elif [[ "$host" == *:* ]]; then
    printf '[%s]' "$host"
  else
    printf '%s' "$host"
  fi
}

resolve_node_name() {
  local service="$1"
  local label="$2"
  local explicit_node="${3:-}"
  local lookup node

  if [ -n "$explicit_node" ]; then
    printf '%s' "$explicit_node"
    return 0
  fi

  lookup="$service"
  if [[ "$lookup" == \[*\] ]]; then
    lookup="${lookup#[}"
    lookup="${lookup%]}"
  fi
  node=$(getent hosts "$lookup" | awk 'NF >= 2 && !found {node=$2; found=1} END {if (found) print node}')

  if [ -z "$node" ] && command -v scontrol >/dev/null 2>&1; then
    node=$(scontrol show node -o 2>/dev/null | awk -v addr="$lookup" '
      {
        name = ""; node_addr = ""
        for (i = 1; i <= NF; i++) {
          if ($i ~ /^NodeName=/) { name = $i; sub(/^NodeName=/, "", name) }
          if ($i ~ /^NodeAddr=/) { node_addr = $i; sub(/^NodeAddr=/, "", node_addr) }
        }
        if (node_addr == addr || node_addr == "[" addr "]") { print name; exit }
      }')
  fi

  if [ -z "$node" ]; then
    echo "ERROR: 无法将 $label=$service 映射为 Slurm NodeName。请配置 DNS、/etc/hosts、slurm.conf 的 NodeAddr，或设置对应的 *_NODE 变量。" >&2
    return 1
  fi
  printf '%s' "$node"
}

resolve_management_nodes() {
  COORDINATOR_NODE=$(resolve_node_name \
    "$COORDINATOR_SERVICE" COORDINATOR_SERVICE "${COORDINATOR_NODE:-}") || return 1
  CONTROLLER_NODE=$(resolve_node_name \
    "$CONTROLLER_SERVICE" CONTROLLER_SERVICE "${CONTROLLER_NODE:-}") || return 1
  KVS_STORE_NODE=""
  if [ "$KV_STORE_ENABLED" = "1" ] || [ "$KV_CONDUCTOR_ENABLED" = "1" ]; then
    KVS_STORE_NODE=$(resolve_node_name \
      "$KVS_MASTER_SERVICE" KVS_MASTER_SERVICE \
      "${KVS_MASTER_NODE:-${KVS_STORE_NODE:-}}") || return 1
  fi

  local conductor_node_hint="${KV_CONDUCTOR_NODE:-}"
  KV_CONDUCTOR_NODE=""
  if [ "$KV_CONDUCTOR_ENABLED" = "1" ]; then
    KV_CONDUCTOR_NODE=$(resolve_node_name \
      "$KV_CONDUCTOR_SERVICE" KV_CONDUCTOR_SERVICE "$conductor_node_hint") || return 1
  fi

  local mf_node_hint="${MF_STORE_NODE:-}"
  MF_STORE_NODE=""
  if [ "$MF_STORE_ENABLED" = "1" ]; then
    MF_STORE_NODE=$(resolve_node_name \
      "$MF_STORE_SERVICE" MF_STORE_SERVICE "$mf_node_hint") || return 1
  fi
}
