# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

# PreStop hook entry point called by Kubernetes YAML lifecycle.preStop.exec.
# All output goes to stdout — collected by log_monitor.py via kubectl logs -f
# and written to log_collect/log/ alongside P/D instance logs.

set -e

# Redirect all output to PID 1's stdout so kubectl logs / log_monitor.py can capture it
exec >> /proc/1/fd/1 2>&1

echo "[prestop] Timestamp: $(date -Iseconds)"
echo "[prestop] CONFIGMAP_PATH: ${CONFIGMAP_PATH}"
echo "[prestop] Args: $@"

# Find NM PID before pause — NM renames itself via setproctitle to
# "MindIE-Motor::NodeManager", so grep for NodeManager instead of the original command.
NM_PID=$(ps -eo pid,args 2>/dev/null | grep "NodeManager" | grep -v grep | awk '{print $1}' | head -1)
if [ -z "${NM_PID}" ]; then
    echo "[prestop] WARNING: Could not find NM process, exiting"
    exit 1
fi
echo "[prestop] Found NM PID: ${NM_PID}"

python3 "${CONFIGMAP_PATH}/prestop.py" "$@" 2>&1
exit_code=$?

echo "[prestop] Exit code: ${exit_code}"
echo "[prestop] Timestamp: $(date -Iseconds)"

# Kill NM via SIGTERM — it has a signal handler that stops Daemon (engines),
# HeartbeatManager (threads), and uvicorn server gracefully.
kill -TERM ${NM_PID}
echo "[prestop] Sent SIGTERM to NM PID ${NM_PID}"

exit ${exit_code}
