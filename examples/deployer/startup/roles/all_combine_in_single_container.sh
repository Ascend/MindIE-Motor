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

if [ "$ROLE" != "SINGLE_CONTAINER" ]; then
    echo "Error: This script is for SINGLE_CONTAINER role only. Current ROLE=$ROLE"
    exit 1
fi

# Multiple components start in one container; disable ASCII logo to avoid log spam.
export MOTOR_DISABLE_LOG_LOGO=1

export CONTROLLER_SERVICE="$POD_IP"
export COORDINATOR_SERVICE="$POD_IP"

# Coordinator registers pool_endpoint from its own environment. The default
# KVS_MASTER_SERVICE is a k8s Service name, which does not exist here. Rewrite
# it before any child starts; the memcache and mooncake branches repeat this
# for processes they launch later.
if [ -n "${KV_STORE_BACKEND:-}" ] && [ "${KV_STORE_EXTERNAL:-0}" != "1" ] && [ -n "${POD_IP:-}" ]; then
    export KVS_MASTER_SERVICE="$POD_IP"
fi

# Claim Coordinator, Controller, KV store, Render, Conductor and NodeManager
# ports before any child binds. A later auto-scan in each process would move
# its own copy and the others would still dial the old number.
if ! python3 -m motor.common.utils.single_container_ports; then
    echo "Error: single-container port planning failed" >&2
    exit 1
fi
_port_env_file="$(dirname "$USER_CONFIG_PATH")/single_container_ports.env"
if [ ! -f "$_port_env_file" ]; then
    echo "Error: single-container port plan was not written" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$_port_env_file"

# set_kv_store_env is generated at deploy time and exports the original ports.
# Put the planned ports back after it runs.
apply_planned_kv_env() {
    if [ -n "${MOTOR_KV_CACHE_STORE_PORT:-}" ]; then
        export KV_CACHE_STORE_PORT="$MOTOR_KV_CACHE_STORE_PORT"
    fi
    if [ -n "${MOTOR_MMC_CONFIG_STORE_URL:-}" ]; then
        export MMC_CONFIG_STORE_URL="$MOTOR_MMC_CONFIG_STORE_URL"
    fi
    if [ -n "${MOTOR_MMC_METRICS_URL:-}" ]; then
        export MMC_METRICS_URL="$MOTOR_MMC_METRICS_URL"
    fi
    if [ -n "${MOTOR_MOONCAKE_METRICS_PORT:-}" ]; then
        export MOONCAKE_METRICS_PORT="$MOTOR_MOONCAKE_METRICS_PORT"
    fi
}

# Worker configs keep the template port. Point them at the planned port.
rewrite_planned_mmc_ports() {
    local _dst="$1"
    [ -f "$_dst" ] || return 0
    if [ -n "${MOTOR_KV_CACHE_STORE_PORT:-}" ]; then
        sed -E -i.bak "s|^(ock\\.mmc\\.meta_service_url[[:space:]]*=[[:space:]]*tcp://[^[:space:]]+:)[0-9]+|\\1${MOTOR_KV_CACHE_STORE_PORT}|" "$_dst"
        rm -f "${_dst}.bak"
    fi
    if [ -n "${MOTOR_MMC_CONFIG_STORE_URL:-}" ]; then
        local _cfg_port="${MOTOR_MMC_CONFIG_STORE_URL##*:}"
        sed -E -i.bak "s|^(ock\\.mmc\\.local_service\\.config_store_url[[:space:]]*=[[:space:]]*tcp://[^[:space:]]+:)[0-9]+|\\1${_cfg_port}|" "$_dst"
        rm -f "${_dst}.bak"
    fi
}

set_cann_env

if is_a5_hardware; then
    set_a5_engine_env
fi

apply_patches || exit 1

set_coordinator_env

# not necessary if no ccae
python3 -m ccae_reporter.run Coordinator &
motor_track_helper $!

ROLE=coordinator python3 -m motor.coordinator.main &
motor_track_child $!

set_controller_env

# not necessary if no ccae
python3 -m ccae_reporter.run Controller &
motor_track_helper $!

ROLE=controller python3 -m motor.controller.main --config "$USER_CONFIG_PATH" &
motor_track_child $!

case "${KV_STORE_BACKEND:-}" in
    mooncake)
        gen_kv_store_config
        set_kv_store_env
        apply_planned_kv_env
        if [ "${KV_STORE_EXTERNAL:-0}" = "1" ]; then
            echo "KV_STORE_EXTERNAL=1: using external mooncake master, skip local mooncake_master"
        else
            ROLE=kv_store mooncake_master --port "$KV_CACHE_STORE_PORT" \
                --eviction_high_watermark_ratio "$KV_STORE_EVICTION_HIGH_WATERMARK_RATIO" \
                --eviction_ratio "$KV_STORE_EVICTION_RATIO" --default_kv_lease_ttl "$DEFAULT_KV_LEASE_TTL" \
                --metrics_port "${MOONCAKE_METRICS_PORT:-50090}" &
            motor_track_child $!
        fi
        ;;
    memcache)
        # Single-container has no k8s Service named mindie-motor-kvs-master.
        # sync_mmc_local_config rewrites meta/config-store URLs only when this is set.
        if [ "${KV_STORE_EXTERNAL:-0}" != "1" ]; then
            export KVS_MASTER_SERVICE="$POD_IP"
        fi
        sync_mmc_local_config
        rewrite_planned_mmc_ports "$CONFIG_PATH/mmc-local-inprocess.conf"
        rewrite_planned_mmc_ports "$CONFIG_PATH/mmc-local-standalone.conf"
        set_kv_store_env
        apply_planned_kv_env
        ROLE=kv_store python3 "$CONFIGMAP_PATH/kv_store_backends.memcache.memcache_meta_service.py" &
        motor_track_child $!
        ;;
esac

# Started after the KV store so it inherits KVS_MASTER_SERVICE, same as the k8s
# kv-conductor container. KV_CONDUCTOR_PORT is exported only when
# kv_conductor_config.http_server_port is configured.
if [ -n "${KV_CONDUCTOR_PORT:-}" ]; then
    set_kv_conductor_env
    ROLE=kv_conductor python3 -m motor.kv_conductor --host 0.0.0.0 --port "$KV_CONDUCTOR_PORT" &
    motor_track_child $!
    echo "pull up kv conductor: ${KV_CONDUCTOR_SERVICE:-$POD_IP}:$KV_CONDUCTOR_PORT"
fi

# RENDER_PORT is exported only by the single-container start script. K8s runs
# Render as the vllm-render sidecar and leaves this unset.
if [ -n "${RENDER_PORT:-}" ]; then
    if [ -z "${MOTOR_DEPLOYER_DIR:-}" ] || [ ! -d "$MOTOR_DEPLOYER_DIR" ]; then
        echo "Error: MOTOR_DEPLOYER_DIR is required to start Render in this container."
        exit 1
    fi
    ROLE=render PYTHONPATH="${MOTOR_DEPLOYER_DIR}${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -c "from lib.generator.render import exec_render_in_place
import json, os
exec_render_in_place(json.load(open(os.environ['USER_CONFIG_PATH'], encoding='utf-8')))" &
    motor_track_child $!
    echo "pull up render: 127.0.0.1:$RENDER_PORT"
fi

if grep -q '"motor_engine_union_config"' "$USER_CONFIG_PATH"; then
    hybrid_instances_num=$(grep '"hybrid_instances_num"' "$USER_CONFIG_PATH" | sed 's/.*:[[:space:]]*\([0-9.]*\).*/\1/')
    if [ -z "$hybrid_instances_num" ] || [ "$hybrid_instances_num" -lt 1 ]; then
        echo "Error: PD hybrid single container requires hybrid_instances_num >= 1"
        exit 1
    fi

    set_union_env
    for i in $(seq 0 $((hybrid_instances_num - 1))); do
        ROLE=union INDEX=$i JOB_NAME=u$i python3 -m motor.node_manager.main &
        motor_track_child $!
        echo "pull up instance: ROLE=union INDEX=$i JOB_NAME=u$i python3 -m motor.node_manager.main &"
    done
else
    p_instances_num=$(grep '"p_instances_num"' "$USER_CONFIG_PATH" | sed 's/.*:[[:space:]]*\([0-9.]*\).*/\1/')
    d_instances_num=$(grep '"d_instances_num"' "$USER_CONFIG_PATH" | sed 's/.*:[[:space:]]*\([0-9.]*\).*/\1/')

    set_prefill_env
    for i in $(seq 0 $((p_instances_num - 1))); do
        ROLE=prefill INDEX=$i JOB_NAME=p$i python3 -m motor.node_manager.main &
        motor_track_child $!
        echo "pull up instance: ROLE=prefill INDEX=$i JOB_NAME=p$i python3 -m motor.node_manager.main &"
    done

    set_decode_env
    for i in $(seq 0 $((d_instances_num - 1))); do
        ROLE=decode INDEX=$i JOB_NAME=d$i python3 -m motor.node_manager.main &
        motor_track_child $!
        echo "pull up instance: ROLE=decode INDEX=$i JOB_NAME=d$i python3 -m motor.node_manager.main &"
    done
fi

motor_supervise_children
exit $?
