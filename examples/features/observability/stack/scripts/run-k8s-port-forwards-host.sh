#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
STACK_DIR="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
. "${SCRIPT_DIR}/load-dotenv.sh"
RUN_DIR="${STACK_DIR}/generated"
ENV_FILE="${RUN_DIR}/discovered.env"
PID_FILE="${RUN_DIR}/k8s-port-forwards.pids"
META_FILE="${RUN_DIR}/k8s-port-forwards.meta"
LOG_DIR="${RUN_DIR}/logs"
BIND_HOST="${PORT_FORWARD_BIND_HOST:-0.0.0.0}"

usage() {
  echo "Usage: $0 [--env-file <generated/discovered.env>]"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      [[ $# -lt 2 ]] && { echo "[port-forward] missing value for --env-file" >&2; exit 1; }
      ENV_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[port-forward] unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

[[ -f "${ENV_FILE}" ]] || { echo "[port-forward] env file not found: ${ENV_FILE}"; exit 0; }
mkdir -p "${RUN_DIR}" "${LOG_DIR}"
load_dotenv "${ENV_FILE}"

PORT_FORWARD_COUNT="${PORT_FORWARD_COUNT:-0}"
if [[ "${PORT_FORWARD_COUNT}" -eq 0 ]]; then
  echo "[port-forward] no host TCP forwards requested."
  exit 0
fi

desired_specs() {
  local idx var
  for ((idx = 0; idx < PORT_FORWARD_COUNT; idx++)); do
    var="PORT_FORWARD_${idx}"
    [[ -n "${!var:-}" ]] && echo "${!var}"
  done
}

is_pid_running() {
  local pid="$1"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
}

stop_existing() {
  [[ -f "${PID_FILE}" ]] || return 0
  while IFS= read -r pid; do
    [[ -z "${pid}" ]] && continue
    if is_pid_running "${pid}"; then
      echo "[port-forward] stopping pid=${pid}"
      kill "${pid}" || true
    fi
  done <"${PID_FILE}"
  rm -f "${PID_FILE}" "${META_FILE}"
}

args_matches_listen_port() {
  local args="$1"
  local port="$2"
  [[ "${args}" =~ (^|[[:space:]])--listen-port[[:space:]]+${port}([^0-9]|$) ]] && return 0
  [[ "${args}" =~ --listen-port=${port}([^0-9]|$) ]] && return 0
  return 1
}

cleanup_orphans_for_ports() {
  local ports=("$@")
  command -v pgrep >/dev/null 2>&1 || return 0
  local pid args port
  while IFS= read -r pid; do
    [[ -z "${pid}" || "${pid}" == "$$" ]] && continue
    args="$(ps -p "${pid}" -o args= 2>/dev/null || true)"
    [[ "${args}" == *"tcp-forward.py"* ]] || continue
    for port in "${ports[@]}"; do
      if args_matches_listen_port "${args}" "${port}"; then
        echo "[port-forward] cleaning orphan pid=${pid} listen_port=${port}"
        kill "${pid}" || true
      fi
    done
  done < <(pgrep -f "tcp-forward.py" || true)
}

port_accepts_connections() {
  local port="$1"
  python3 - "${port}" <<'PY'
import socket
import sys
port = int(sys.argv[1])
sock = socket.socket()
sock.settimeout(1)
try:
    sock.connect(("127.0.0.1", port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
}

start_one() {
  local spec="$1"
  local namespace pod_ip remote_port local_port pod_name
  IFS='|' read -r namespace pod_ip remote_port local_port pod_name <<<"${spec}"
  local log_file="${LOG_DIR}/tcp-forward-${local_port}.log"
  python3 "${SCRIPT_DIR}/tcp-forward.py" \
    --listen-host "${BIND_HOST}" \
    --listen-port "${local_port}" \
    --target-host "${pod_ip}" \
    --target-port "${remote_port}" >"${log_file}" 2>&1 &
  local pid=$!
  sleep 0.5
  if ! is_pid_running "${pid}" || ! port_accepts_connections "${local_port}"; then
    kill "${pid}" >/dev/null 2>&1 || true
    sleep 0.3
    python3 "${SCRIPT_DIR}/tcp-forward.py" \
      --listen-host "${BIND_HOST}" \
      --listen-port "${local_port}" \
      --target-host "${pod_ip}" \
      --target-port "${remote_port}" >>"${log_file}" 2>&1 &
    pid=$!
    sleep 0.5
  fi
  if ! is_pid_running "${pid}" || ! port_accepts_connections "${local_port}"; then
    echo "[port-forward] failed to start ${namespace}/${pod_name} ${pod_ip}:${remote_port} -> ${local_port}" >&2
    return 1
  fi
  echo "${pid}" >>"${PID_FILE}"
  echo "[port-forward] started ${namespace}/${pod_name} ${pod_ip}:${remote_port} -> ${BIND_HOST}:${local_port} pid=${pid}"
}

DESIRED_FILE="$(mktemp)"
EXISTING_FILE="$(mktemp)"
trap 'rm -f "${DESIRED_FILE}" "${EXISTING_FILE}"' EXIT
desired_specs | sort >"${DESIRED_FILE}"
if [[ -f "${META_FILE}" ]]; then
  sort "${META_FILE}" >"${EXISTING_FILE}"
else
  : >"${EXISTING_FILE}"
fi

all_alive=1
if [[ -f "${PID_FILE}" ]]; then
  while IFS= read -r pid; do
    [[ -z "${pid}" ]] && continue
    is_pid_running "${pid}" || all_alive=0
  done <"${PID_FILE}"
else
  all_alive=0
fi

if cmp -s "${DESIRED_FILE}" "${EXISTING_FILE}" && [[ "${all_alive}" -eq 1 ]]; then
  echo "[port-forward] existing topology is current."
  exit 0
fi

mapfile -t local_ports < <(desired_specs | awk -F'|' '{print $4}' | sort -u)
stop_existing
cleanup_orphans_for_ports "${local_ports[@]}"
: >"${PID_FILE}"
start_failed=0
while IFS= read -r spec; do
  [[ -z "${spec}" ]] && continue
  if ! start_one "${spec}"; then
    start_failed=1
  fi
done <"${DESIRED_FILE}"

if [[ "${start_failed}" -eq 1 ]]; then
  rm -f "${META_FILE}"
  echo "[port-forward] one or more forwards failed; META not updated (will retry on next run)." >&2
  exit 1
fi
cp "${DESIRED_FILE}" "${META_FILE}"
