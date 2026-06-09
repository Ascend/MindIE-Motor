#!/usr/bin/env bash
# Stop the pyMotor observability stack for both docker/native runtimes.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "${SCRIPT_DIR}"
. "${SCRIPT_DIR}/scripts/load-dotenv.sh"

"${SCRIPT_DIR}/scripts/stop-k8s-port-forwards-host.sh" || true

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then
  PURGE=1
fi

if [[ -f .env ]]; then
  load_dotenv .env
fi

STACK_MODE="${OBS_STACK_MODE:-minimal}"
WITH_MOCK="${OBS_WITH_MOCK:-0}"
PROFILES="${OBS_COMPOSE_PROFILES:-}"

DOCKER_BIN="${DOCKER_BIN:-docker}"
if "${DOCKER_BIN}" compose version >/dev/null 2>&1; then
  PROFILE_ARGS=()
  if [[ "${STACK_MODE}" == "full" ]]; then
    PROFILE_ARGS+=(--profile full)
  fi
  if [[ "${WITH_MOCK}" -eq 1 ]]; then
    PROFILE_ARGS+=(--profile mock)
  fi
  if [[ -n "${PROFILES}" ]]; then
    IFS=',' read -r -a profiles_arr <<<"${PROFILES}"
    for p in "${profiles_arr[@]}"; do
      [[ -z "${p}" ]] && continue
      PROFILE_ARGS+=(--profile "${p}")
    done
  fi
  PROFILE_ARGS+=(--profile npu)

  ARGS=(compose "${PROFILE_ARGS[@]}" down)
  if [[ "${PURGE}" -eq 1 ]]; then
    ARGS+=(-v)
  fi
  echo "[stop] ${DOCKER_BIN} ${ARGS[*]}"
  "${DOCKER_BIN}" "${ARGS[@]}" || true
else
  echo "[stop] docker compose unavailable, skip docker runtime cleanup."
fi

RUNTIME_DIR="${SCRIPT_DIR:?}/.native-runtime"
RUN_DIR="${RUNTIME_DIR:?}/run"

stop_pid_file() {
  local pid_file="$1"
  [[ ! -f "${pid_file}" ]] && return
  local pid
  pid="$(<"${pid_file}")"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    echo "[stop] stopping native process pid=${pid} (${pid_file##*/})"
    kill "${pid}" || true
  fi
  rm -f "${pid_file}"
}

if [[ -d "${RUN_DIR}" ]]; then
  stop_pid_file "${RUN_DIR}/grafana.pid"
  stop_pid_file "${RUN_DIR}/prometheus.pid"
  stop_pid_file "${RUN_DIR}/otel-collector.pid"
  stop_pid_file "${RUN_DIR}/tempo.pid"
fi

if [[ "${PURGE}" -eq 1 ]]; then
  echo "[stop] purge native runtime data."
  rm -rf "${RUNTIME_DIR:?}/data" "${RUNTIME_DIR:?}/logs" "${RUNTIME_DIR:?}/run"
fi

echo "[stop] done."
