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

# check probe type
if [ -z "$1" ]; then
    echo "Error: Missing probe type. Please provide one of 'startup', 'readiness', or 'liveness'."
    exit 1
fi

probe_type=$1

if [ -z "$2" ]; then
    role=$ROLE
else
    role=$2
fi

# Execute probe
python3 "$CONFIGMAP_PATH/probe.py" "$role" "$probe_type"
