#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

if [ "$ROLE" != "render" ]; then
    echo "Error: This script is for render role only. Current ROLE=$ROLE"
    exit 1
fi

if [ -z "${MOTOR_DEPLOYER_DIR:-}" ] || [ ! -d "$MOTOR_DEPLOYER_DIR" ]; then
    echo "Error: MOTOR_DEPLOYER_DIR is not set to the examples/deployer directory."
    exit 1
fi

export PYTHONPATH="${MOTOR_DEPLOYER_DIR}${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -c "from lib.generator.render import exec_render_in_place
import json, os
exec_render_in_place(json.load(open(os.environ['USER_CONFIG_PATH'], encoding='utf-8')))
"
