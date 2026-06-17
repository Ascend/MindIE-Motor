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

cd ./log_collect || {
  echo "Error: log_collect directory not found. Run this script from examples/deployer." >&2
  exit 1
}

CONFIG="log_config.ini"
[[ -f "$CONFIG" ]] || {
  echo "Error: ${CONFIG} not found in $(pwd) (log_collect directory)." >&2
  exit 1
}
# Reject the default template (name_space left empty); full INI checks are in log_monitor.py
grep -qE '^[[:space:]]*name_space[[:space:]]*=[[:space:]]*[^[:space:]#]+' "$CONFIG" || {
  echo "Error: The name_space option in log_collect/log_config.ini is missing, empty, or invalid." >&2
  echo "Under [LogSetting], set name_space to your Kubernetes namespace, then retry." >&2
  exit 1
}

NAMESPACE=$(grep -E '^[[:space:]]*name_space[[:space:]]*=' "$CONFIG" | head -1 | sed 's/.*=[[:space:]]*//; s/[[:space:]]*$//')

# Gracefully stop any existing log_monitor instance for this namespace only
touch "stop_log_${NAMESPACE}"
sleep 2
rm -f "stop_log_${NAMESPACE}"

setsid nohup python3 -u log_monitor.py > output.log 2>&1 </dev/null &

timeout 2 tail -f output.log
