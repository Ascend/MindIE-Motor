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

if [ "$ROLE" != "controller" ]; then
    echo "Error: This script is for controller role only. Current ROLE=$ROLE"
    exit 1
fi

set_controller_env
setup_motor_log_path

NODEPORT_WARN_FILE="${CONFIGMAP_PATH}/nodeport_conflict_controller.txt"
if [ -f "$NODEPORT_WARN_FILE" ] && [ -s "$NODEPORT_WARN_FILE" ]; then
    echo "========== [NodePort] CONFLICT WARNING =========="
    cat "$NODEPORT_WARN_FILE"
    echo "================================================="
fi

# not necessary if no ccae
python3 -m ccae_reporter.run Controller &

python3 -m motor.controller.main --config $USER_CONFIG_PATH
