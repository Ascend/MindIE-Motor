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

if [ "$ROLE" != "kv_pool" ]; then
    echo "Error: This script is for kv_pool role only. Current ROLE=$ROLE"
    exit 1
fi

export LD_LIBRARY_PATH=/usr/local/Ascend/ascend-toolkit/latest/python/site-packages/mooncake:$LD_LIBRARY_PATH

set_kv_pool_env

mooncake_master --port "$KV_POOL_PORT" \
    --eviction_high_watermark_ratio "$KV_POOL_EVICTION_HIGH_WATERMARK_RATIO" \
    --eviction_ratio "$KV_POOL_EVICTION_RATIO" --default_kv_lease_ttl "$DEFAULT_KV_LEASE_TTL"
