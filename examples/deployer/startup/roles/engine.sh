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

if [ "$ROLE" != "encode" ] && [ "$ROLE" != "prefill" ] && [ "$ROLE" != "decode" ] && [ "$ROLE" != "union" ]; then
    echo "Error: This script is for encode/prefill/decode/union role only. Current ROLE=$ROLE"
    exit 1
fi

apply_shuffle_safetensors_patch
setup_jemalloc

gen_ranktable_config
gen_kv_pool_config

set_cann_env

if is_a5_hardware; then
    set_a5_engine_env
fi

set_mf_store_env

# CRD scenario: refresh JOB_NAME with INFER_SERVICE_INDEX and INSTANCE_INDEX injected by CRD
# Final format: {namespace}-{InferServiceSet_name}-{INFER_SERVICE_INDEX}-p/d/u{INSTANCE_INDEX}
if [ -n "$INFER_SERVICE_INDEX" ] && [ -n "$INSTANCE_INDEX" ]; then
    if [ "$ROLE" = "encode" ]; then
        export JOB_NAME="${JOB_NAME}-${INFER_SERVICE_INDEX}-e${INSTANCE_INDEX}"
    elif [ "$ROLE" = "prefill" ]; then
        export JOB_NAME="${JOB_NAME}-${INFER_SERVICE_INDEX}-p${INSTANCE_INDEX}"
    elif [ "$ROLE" = "decode" ]; then
        export JOB_NAME="${JOB_NAME}-${INFER_SERVICE_INDEX}-d${INSTANCE_INDEX}"
    elif [ "$ROLE" = "union" ]; then
        export JOB_NAME="${JOB_NAME}-${INFER_SERVICE_INDEX}-u${INSTANCE_INDEX}"
    fi
    echo "CRD mode: JOB_NAME refreshed to $JOB_NAME"
fi

setup_motor_log_path
setup_ascend_work_path
setup_ascend_cache_path

if [ "$ROLE" = "encode" ]; then
    set_encode_env
elif [ "$ROLE" = "decode" ]; then
    set_decode_env
elif [ "$ROLE" = "prefill" ]; then
    set_prefill_env
elif [ "$ROLE" = "union" ]; then
    set_union_env
fi

python3 -m motor.node_manager.main &
pid=$!
echo "pull up $ROLE instance"
wait $pid
exit_code=$?

if [ $exit_code -ne 0 ]; then
    echo "Error: mindie daemon exited with code $exit_code"
    exit 1
fi
echo "All processes finished successfully."
exit 0
