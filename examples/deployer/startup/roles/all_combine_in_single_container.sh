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

setup_jemalloc

export CONTROLLER_SERVICE="$POD_IP"
export COORDINATOR_SERVICE="$POD_IP"

# only A2 and A3 need it, A5 does not need it.
gen_ranktable_config

set_cann_env

if is_a5_hardware; then
    set_a5_engine_env
fi

apply_shuffle_safetensors_patch

pids=()

set_coordinator_env

# not necessary if no ccae
python3 -m ccae_reporter.run Coordinator &

ROLE=coordinator python3 -m motor.coordinator.main &
pids+=($!)

set_controller_env

# not necessary if no ccae
python3 -m ccae_reporter.run Controller &

ROLE=controller python3 -m motor.controller.main --config "$USER_CONFIG_PATH" &
pids+=($!)

case "${KV_STORE_BACKEND:-}" in
    mooncake)
        gen_kv_store_config
        set_kv_store_env
        ROLE=kv_store mooncake_master --port "$KV_CACHE_STORE_PORT" \
            --eviction_high_watermark_ratio "$KV_STORE_EVICTION_HIGH_WATERMARK_RATIO" \
            --eviction_ratio "$KV_STORE_EVICTION_RATIO" --default_kv_lease_ttl "$DEFAULT_KV_LEASE_TTL" &
        pids+=($!)
        ;;
    memcache)
        sync_mmc_local_config
        set_kv_store_env
        ROLE=kv_store python3 "$CONFIGMAP_PATH/kv_store_backends.memcache.memcache_meta_service.py" &
        pids+=($!)
        ;;
esac

if grep -q '"motor_engine_union_config"' "$USER_CONFIG_PATH"; then
    hybrid_instances_num=$(grep '"hybrid_instances_num"' "$USER_CONFIG_PATH" | sed 's/.*:[[:space:]]*\([0-9.]*\).*/\1/')
    if [ -z "$hybrid_instances_num" ] || [ "$hybrid_instances_num" -lt 1 ]; then
        echo "Error: PD hybrid single container requires hybrid_instances_num >= 1"
        exit 1
    fi

    set_union_env
    for i in $(seq 0 $((hybrid_instances_num - 1))); do
        ROLE=union INDEX=$i JOB_NAME=u$i RANKTABLE_PATH=$CONFIG_PATH/ranktable_u${i}.json python3 -m motor.node_manager.main &
        pids+=($!)
        echo "pull up instance: ROLE=union INDEX=$i JOB_NAME=u$i RANKTABLE_PATH=$CONFIG_PATH/ranktable_u${i}.json python3 -m motor.node_manager.main &"
    done
else
    p_instances_num=$(grep '"p_instances_num"' "$USER_CONFIG_PATH" | sed 's/.*:[[:space:]]*\([0-9.]*\).*/\1/')
    d_instances_num=$(grep '"d_instances_num"' "$USER_CONFIG_PATH" | sed 's/.*:[[:space:]]*\([0-9.]*\).*/\1/')

    set_prefill_env
    for i in $(seq 0 $((p_instances_num - 1))); do
        ROLE=prefill INDEX=$i JOB_NAME=p$i RANKTABLE_PATH=$CONFIG_PATH/ranktable_p${i}.json python3 -m motor.node_manager.main &
        pids+=($!)
        echo "pull up instance: ROLE=prefill INDEX=$i JOB_NAME=p$i RANKTABLE_PATH=$CONFIG_PATH/ranktable_p${i}.json python3 -m motor.node_manager.main &"
    done

    set_decode_env
    for i in $(seq 0 $((d_instances_num - 1))); do
        ROLE=decode INDEX=$i JOB_NAME=d$i RANKTABLE_PATH=$CONFIG_PATH/ranktable_d${i}.json python3 -m motor.node_manager.main &
        pids+=($!)
        echo "pull up instance: ROLE=decode INDEX=$i JOB_NAME=d$i RANKTABLE_PATH=$CONFIG_PATH/ranktable_d${i}.json python3 -m motor.node_manager.main &"
    done
fi

for pid in "${pids[@]}"; do
    wait $pid
done
echo "All processes finished successfully."
exit 0
