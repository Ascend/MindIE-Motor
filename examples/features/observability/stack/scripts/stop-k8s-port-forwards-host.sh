#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
STACK_DIR="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
RUN_DIR="${STACK_DIR}/generated"
PID_FILE="${RUN_DIR}/k8s-port-forwards.pids"
META_FILE="${RUN_DIR}/k8s-port-forwards.meta"

is_pid_running() {
  local pid="$1"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
}

if [[ -f "${PID_FILE}" ]]; then
  while IFS= read -r pid; do
    [[ -z "${pid}" ]] && continue
    if is_pid_running "${pid}"; then
      echo "[port-forward-stop] stopping pid=${pid}"
      kill "${pid}" || true
    fi
  done <"${PID_FILE}"
fi

if command -v pgrep >/dev/null 2>&1; then
  while IFS= read -r pid; do
    [[ -z "${pid}" || "${pid}" == "$$" ]] && continue
    args="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
    [[ "${args}" == *"tcp-forward.py"* ]] || continue
    [[ "${args}" == *"${SCRIPT_DIR}/tcp-forward.py"* || "${args}" == *" tcp-forward.py"* ]] || continue
    echo "[port-forward-stop] stopping orphan pid=${pid}"
    kill "${pid}" || true
  done < <(pgrep -f "tcp-forward.py" || true)
fi

rm -f "${PID_FILE}" "${META_FILE}"
echo "[port-forward-stop] done."
