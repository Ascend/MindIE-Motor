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

apply_shuffle_safetensors_patch
setup_jemalloc

export CONTROLLER_SERVICE="$POD_IP"
export COORDINATOR_SERVICE="$POD_IP"

gen_ranktable_config

set_cann_env

if is_a5_hardware; then
    set_a5_engine_env
fi

pids=()

set_coordinator_env

# not necessary if no ccae
python3 -m ccae_reporter.run Coordinator &

ROLE=coordinator python3 -m motor.coordinator.main &
pids+=($!)

set_controller_env

# not necessary if no ccae
python3 -m ccae_reporter.run Controller &

ROLE=controller python3 -m motor.controller.main --config $USER_CONFIG_PATH &
pids+=($!)

gen_kv_pool_config

if [ -n "$KVP_MASTER_SERVICE" ]; then
    set_kv_pool_env
    ROLE=kv_pool mooncake_master --port "$KV_POOL_PORT" \
    --eviction_high_watermark_ratio "$KV_POOL_EVICTION_HIGH_WATERMARK_RATIO" \
    --eviction_ratio "$KV_POOL_EVICTION_RATIO" --default_kv_lease_ttl "$DEFAULT_KV_LEASE_TTL" &
    pids+=($!)
fi

p_instances_num=$(grep '"p_instances_num"' $USER_CONFIG_PATH | sed 's/.*:[[:space:]]*\([0-9.]*\).*/\1/')
d_instances_num=$(grep '"d_instances_num"' $USER_CONFIG_PATH | sed 's/.*:[[:space:]]*\([0-9.]*\).*/\1/')

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

for pid in "${pids[@]}"; do
    wait $pid
done
echo "All processes finished successfully."
exit 0
