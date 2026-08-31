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

if [ "$ROLE" != "mf_store" ]; then
    echo "Error: This script is for mf_store role only. Current ROLE=$ROLE"
    exit 1
fi

MF_STORE_HOST="$POD_IP"
if [[ "$MF_STORE_HOST" == *:* && "$MF_STORE_HOST" != \[*\] ]]; then
    MF_STORE_HOST="[$MF_STORE_HOST]"
fi
export ASCEND_MF_STORE_URL="tcp://$MF_STORE_HOST:$ASCEND_MF_STORE_PORT"
export ASCEND_MF_LOG_LEVEL=0

python3 -m memfabric_hybrid.launch_ascend_mf_store
