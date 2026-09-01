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

if [ "$ROLE" != "coordinator_controller" ]; then
    echo "Error: This script is for coordinator_controller role only. Current ROLE=$ROLE"
    exit 1
fi

# Both components start in one container; disable ASCII logo to avoid log spam.
export MOTOR_DISABLE_LOG_LOGO=1

set_cann_env
setup_jemalloc
setup_motor_log_path

for warn_file in \
    "${CONFIGMAP_PATH}/nodeport_conflict_coordinator.txt" \
    "${CONFIGMAP_PATH}/nodeport_conflict_controller.txt"
do
    if [ -f "$warn_file" ] && [ -s "$warn_file" ]; then
        echo "========== [NodePort] CONFLICT WARNING =========="
        cat "$warn_file"
        echo "================================================="
    fi
done

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

motor_supervise_children
exit $?
