#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "${SCRIPT_DIR}"
. "${SCRIPT_DIR}/scripts/load-dotenv.sh"

NAMESPACE="${MOTOR_NAMESPACE:-}"
NODE_IP="${MOTOR_NODE_IP:-}"
USER_CONFIG="${MOTOR_USER_CONFIG:-}"
ENGINE_MGMT_PORT="${MOTOR_ENGINE_MGMT_PORT:-10001}"
OBS_HOST_INPUT="${OBS_HOST:-}"

FORCE_NATIVE=0
DISCOVER_ONLY=0
DRY_RUN=0
STACK_MODE="${OBS_STACK_MODE:-minimal}"

usage() {
  cat <<'EOF'
Usage: ./launch.sh [options]

Options:
  --namespace <namespace>     Kubernetes namespace / job_id
  --node-ip <node-ip>         Node IP used for NodePort access
  --user-config <path>        pyMotor user_config.json path
  --minimal                   Start core stack with Loki (default)
  --full                      Add node-exporter/cAdvisor infra exporters
  --discover-only             Only run discovery, do not start stack
  --dry-run                   Run discovery and print generated Prometheus config
  --native                    Skip Docker Compose and run native runtime
  -h, --help                  Show this help

Environment:
  MOTOR_NAMESPACE
  MOTOR_NODE_IP
  MOTOR_USER_CONFIG
  MOTOR_ENGINE_MGMT_PORT
  OBS_HOST
  PROXY_SH              dotenv file for native binary downloads only (see SERVICE_GUIDE.md §2.4)

Proxy (see SERVICE_GUIDE.md §2.4):
  - Discovery/kubectl: unset shell proxy before launch (script also strips proxy for kubectl).
  - Docker image pull: export HTTP_PROXY in current shell or source your proxy.sh before launch.
  - Native runtime: set PROXY_SH=/path/to/dotenv in .env (optional; default empty).
  - Grafana container: HTTP_PROXY cleared for in-stack prometheus/tempo.
  - OBS_COMPOSE_PULL=never|missing|always  OBS_COMPOSE_BUILD=0|1
  - OBS_FORCE_NATIVE_FALLBACK=1  only then fall back to full native runtime on Docker failure
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace)
      [[ $# -lt 2 ]] && { echo "[launch] missing value for --namespace" >&2; exit 1; }
      NAMESPACE="$2"
      shift 2
      ;;
    --node-ip)
      [[ $# -lt 2 ]] && { echo "[launch] missing value for --node-ip" >&2; exit 1; }
      NODE_IP="$2"
      shift 2
      ;;
    --user-config)
      [[ $# -lt 2 ]] && { echo "[launch] missing value for --user-config" >&2; exit 1; }
      USER_CONFIG="$2"
      shift 2
      ;;
    --minimal)
      STACK_MODE="minimal"
      shift
      ;;
    --full)
      STACK_MODE="full"
      shift
      ;;
    --discover-only)
      DISCOVER_ONLY=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --native)
      FORCE_NATIVE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[launch] unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  echo "[launch] created .env from .env.example"
fi

DISCOVERY_RUNTIME="docker"
if [[ "${FORCE_NATIVE}" -eq 1 ]]; then
  DISCOVERY_RUNTIME="native"
fi
DISCOVERY_CMD=(python3 "./scripts/discover-targets.py" "--output-dir" "./generated" "--engine-mgmt-port" "${ENGINE_MGMT_PORT}" "--runtime" "${DISCOVERY_RUNTIME}")
[[ -n "${NAMESPACE}" ]] && DISCOVERY_CMD+=("--namespace" "${NAMESPACE}")
[[ -n "${NODE_IP}" ]] && DISCOVERY_CMD+=("--node-ip" "${NODE_IP}")
[[ -n "${USER_CONFIG}" ]] && DISCOVERY_CMD+=("--user-config" "${USER_CONFIG}")
[[ -n "${OBS_HOST_INPUT}" ]] && DISCOVERY_CMD+=("--obs-host" "${OBS_HOST_INPUT}")

echo "[launch] discovering targets..."
"${DISCOVERY_CMD[@]}"

load_dotenv "./generated/discovered.env"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo
  echo "========== generated/prometheus.yml =========="
  sed -n '1,240p' "./generated/prometheus.yml"
  echo "============================================="
fi

if [[ "${DISCOVER_ONLY}" -eq 1 || "${DRY_RUN}" -eq 1 ]]; then
  echo "[launch] discovery completed."
  exit 0
fi

run_native() {
  echo "[launch] starting native runtime..."
  echo "[launch] refreshing discovery for native runtime..."
  NATIVE_DISCOVERY_CMD=(python3 "./scripts/discover-targets.py" "--output-dir" "./generated" "--engine-mgmt-port" "${ENGINE_MGMT_PORT}" "--runtime" "native")
  [[ -n "${NAMESPACE}" ]] && NATIVE_DISCOVERY_CMD+=("--namespace" "${NAMESPACE}")
  [[ -n "${NODE_IP}" ]] && NATIVE_DISCOVERY_CMD+=("--node-ip" "${NODE_IP}")
  [[ -n "${USER_CONFIG}" ]] && NATIVE_DISCOVERY_CMD+=("--user-config" "${USER_CONFIG}")
  [[ -n "${OBS_HOST_INPUT}" ]] && NATIVE_DISCOVERY_CMD+=("--obs-host" "${OBS_HOST_INPUT}")
  "${NATIVE_DISCOVERY_CMD[@]}"
  ./scripts/start-native.sh \
    --env-file "./generated/discovered.env" \
    --prometheus-file "./generated/prometheus.yml"
}

wait_for_http() {
  local url=$1
  local label=$2
  local max_attempts="${3:-30}"
  local sleep_sec="${4:-2}"

  local attempt=1
  while (( attempt <= max_attempts )); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    if (( attempt == 1 )); then
      echo "[launch] waiting for ${label}..."
    fi
    sleep "${sleep_sec}"
    attempt=$((attempt + 1))
  done

  echo "[launch] ${label} readiness check failed: ${url}" >&2
  return 1
}

check_core_stack() {
  local docker_bin="${DOCKER_BIN:-docker}"
  local missing=()
  local core_containers=(
    pymotor-prometheus
    pymotor-grafana
    pymotor-tempo
    pymotor-loki
    pymotor-otel-collector
  )

  for name in "${core_containers[@]}"; do
    if ! "${docker_bin}" inspect -f '{{.State.Running}}' "${name}" 2>/dev/null | grep -qx 'true'; then
      missing+=("${name}")
    fi
  done

  if ((${#missing[@]} > 0)); then
    echo "[launch] core stack unhealthy; not running containers: ${missing[*]}" >&2
    return 1
  fi

  if command -v curl >/dev/null 2>&1; then
    local loki_port="${LOKI_PORT:-3100}"
    local loki_attempts="${LOKI_READY_MAX_ATTEMPTS:-30}"
    local loki_sleep="${LOKI_READY_SLEEP_SEC:-2}"
    wait_for_http \
      "http://127.0.0.1:${loki_port}/ready" \
      "Loki :${loki_port}" \
      "${loki_attempts}" \
      "${loki_sleep}" || return 1
  fi

  echo "[launch] core stack healthy (includes pymotor-loki)"
  return 0
}

if [[ "${FORCE_NATIVE}" -eq 1 ]]; then
  run_native
  exit 0
fi

echo "[launch] starting Docker Compose stack..."
set +e
PROMETHEUS_CONFIG_FILE="./generated/prometheus.yml" \
OBS_HOST="${OBS_HOST:-}" \
./start.sh "--${STACK_MODE}"
DOCKER_RC=$?
set -e

if [[ "${DOCKER_RC}" -ne 0 ]]; then
  if [[ "${OBS_FORCE_NATIVE_FALLBACK:-0}" == "1" ]]; then
    echo "[launch] Docker startup failed (exit=${DOCKER_RC}); OBS_FORCE_NATIVE_FALLBACK=1, switching to native runtime."
    ./stop.sh || true
    run_native
    exit 0
  fi
  echo "[launch] Docker startup failed (exit=${DOCKER_RC}). Set OBS_FORCE_NATIVE_FALLBACK=1 to fall back to native runtime." >&2
  exit "${DOCKER_RC}"
fi

if ! check_core_stack; then
  if [[ "${OBS_FORCE_NATIVE_FALLBACK:-0}" == "1" ]]; then
    echo "[launch] core stack check failed; OBS_FORCE_NATIVE_FALLBACK=1, switching to native runtime."
    ./stop.sh || true
    run_native
    exit 0
  fi
  echo "[launch] core stack check failed. Set OBS_FORCE_NATIVE_FALLBACK=1 to fall back to native runtime." >&2
  exit 1
fi
