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

if [ "$ROLE" != "kv_store" ]; then
    echo "Error: This script is for kv_store role only. Current ROLE=$ROLE"
    exit 1
fi

set_kv_store_env

BACKEND="${KV_STORE_BACKEND:-memcache}"
BACKEND_SCRIPT="$SCRIPT_DIR/kv_store_backends.${BACKEND}.${BACKEND}.sh"

if [ -f "$BACKEND_SCRIPT" ]; then
    source "$BACKEND_SCRIPT"
else
    echo "Error: Unsupported KV store backend '${BACKEND}' (script not found: ${BACKEND_SCRIPT})"
    exit 1
fi
