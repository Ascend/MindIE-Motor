#!/usr/bin/env bash
# Launch the pyMotor observability stack via Docker Compose.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "${SCRIPT_DIR}"
. "${SCRIPT_DIR}/scripts/load-dotenv.sh"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "[start] created .env from .env.example"
fi

PROFILES=""
STACK_MODE="${OBS_STACK_MODE:-minimal}"
WITH_MOCK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minimal)
      STACK_MODE="minimal"
      shift
      ;;
    --full)
      STACK_MODE="full"
      shift
      ;;
    --with-mock)
      WITH_MOCK=1
      shift
      ;;
    --profile)
      shift
      PROFILES="$1"
      shift
      ;;
    -h|--help)
      cat <<EOF
Usage: $0 [options]
  --minimal          start core stack with Loki (default)
  --full             add node-exporter/cAdvisor infra exporters
  --with-mock        enable mock profile when present
  --profile <list>   comma-separated profiles (default: none)
                     known: npu, full
  -h, --help         show this help

Loki image:
  Pulls grafana/loki from registry first; on failure runs
  ./scripts/build-loki-image.sh to build a local scratch image.
EOF
      exit 0
      ;;
    *)
      echo "[start] unknown option: $1" >&2
      exit 1
      ;;
  esac
done

DISCOVERED_ENV="${SCRIPT_DIR}/generated/discovered.env"
# Preserve launch.sh / caller overrides before dotenv files (see PR 252).
PRESERVE_PROMETHEUS_CONFIG="${PROMETHEUS_CONFIG_FILE:-}"
PRESERVE_OTEL_CONFIG="${OTEL_CONFIG_FILE:-}"
PRESERVE_GRAFANA_PROV="${GRAFANA_PROVISIONING_DIR:-}"
# Load .env defaults first; discovered.env (from launch.sh discovery) must win.
load_dotenv "${SCRIPT_DIR}/.env"
load_dotenv "${DISCOVERED_ENV}"
if [[ -n "${PRESERVE_PROMETHEUS_CONFIG}" ]]; then
  PROMETHEUS_CONFIG_FILE="${PRESERVE_PROMETHEUS_CONFIG}"
fi
if [[ -n "${PRESERVE_OTEL_CONFIG}" ]]; then
  OTEL_CONFIG_FILE="${PRESERVE_OTEL_CONFIG}"
fi
if [[ -n "${PRESERVE_GRAFANA_PROV}" ]]; then
  GRAFANA_PROVISIONING_DIR="${PRESERVE_GRAFANA_PROV}"
fi

prepare_minimal_provisioning() {
  local out_dir="${SCRIPT_DIR}/generated/grafana-provisioning-minimal"
  mkdir -p "${out_dir}/datasources" "${out_dir}/dashboards"
  cp -f "${SCRIPT_DIR}/grafana/provisioning/dashboards/dashboard-providers.yml" "${out_dir}/dashboards/dashboard-providers.yml"
  cp -f "${SCRIPT_DIR}/grafana/provisioning/datasources/datasources-minimal.yml" "${out_dir}/datasources/datasources.yml"
  GRAFANA_PROVISIONING_DIR="./generated/grafana-provisioning-minimal"
  export GRAFANA_PROVISIONING_DIR
}

prepare_minimal_prometheus() {
  local generated_prom="${SCRIPT_DIR}/generated/prometheus.yml"
  local input_file="${PROMETHEUS_CONFIG_FILE:-}"
  if [[ -z "${input_file}" || "${input_file}" == "./prometheus/prometheus.yml" ]]; then
    if [[ -f "${generated_prom}" ]]; then
      input_file="${generated_prom}"
    else
      input_file="${SCRIPT_DIR}/prometheus/prometheus-minimal.yml"
    fi
  elif [[ ! -f "${input_file}" ]]; then
    if [[ -f "${generated_prom}" ]]; then
      input_file="${generated_prom}"
    else
      input_file="${SCRIPT_DIR}/prometheus/prometheus-minimal.yml"
    fi
  fi
  local output_file="${SCRIPT_DIR}/generated/prometheus-minimal.runtime.yml"
  mkdir -p "${SCRIPT_DIR}/generated"
  cp "${input_file}" "${output_file}"
  PROMETHEUS_CONFIG_FILE="./generated/prometheus-minimal.runtime.yml"
  export PROMETHEUS_CONFIG_FILE
}

start_host_helpers() {
  if [[ -f "${DISCOVERED_ENV}" ]]; then
    "${SCRIPT_DIR}/scripts/run-k8s-port-forwards-host.sh" --env-file "${DISCOVERED_ENV}"
  fi
}

ensure_loki_image() {
  local prefix="${REGISTRY_PREFIX:-}"
  local lv="${LOKI_VERSION:-3.3.0}"
  local loki_img="${LOKI_IMAGE:-${prefix}grafana/loki:${lv}}"
  local docker_bin="${DOCKER_BIN:-docker}"

  if "${docker_bin}" image inspect "${loki_img}" >/dev/null 2>&1; then
    echo "[start] Loki image available: ${loki_img}"
    LOKI_IMAGE="${loki_img}"
    export LOKI_IMAGE
    return 0
  fi

  echo "[start] pulling Loki image: ${loki_img}"
  if "${docker_bin}" pull "${loki_img}" >/dev/null 2>&1; then
    LOKI_IMAGE="${loki_img}"
    export LOKI_IMAGE
    return 0
  fi

  if [[ -n "${prefix}" ]]; then
    local hub_img="grafana/loki:${lv}"
    echo "[start] pulling ${hub_img}..."
    if "${docker_bin}" pull "${hub_img}" >/dev/null 2>&1; then
      "${docker_bin}" tag "${hub_img}" "${loki_img}"
      LOKI_IMAGE="${loki_img}"
      export LOKI_IMAGE
      return 0
    fi
  fi

  echo "[start] Loki pull failed; building local image via scripts/build-loki-image.sh"
  LOKI_IMAGE="${loki_img}" "${SCRIPT_DIR}/scripts/build-loki-image.sh"
  LOKI_IMAGE="${loki_img}"
  export LOKI_IMAGE
}

DOCKER_BIN="${DOCKER_BIN:-docker}"
if ! "${DOCKER_BIN}" compose version >/dev/null 2>&1; then
  echo "[start] error: '${DOCKER_BIN} compose' is not available. Install Docker Compose v2." >&2
  exit 1
fi

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

ensure_loki_image

if [[ "${STACK_MODE}" == "minimal" ]]; then
  prepare_minimal_provisioning
  prepare_minimal_prometheus
  OTEL_CONFIG_FILE="${OTEL_CONFIG_FILE:-./otel-collector/otel-collector-minimal.yaml}"
else
  PROMETHEUS_CONFIG_FILE="${PROMETHEUS_CONFIG_FILE:-./prometheus/prometheus.yml}"
  GRAFANA_PROVISIONING_DIR="${GRAFANA_PROVISIONING_DIR:-./grafana/provisioning}"
  OTEL_CONFIG_FILE="${OTEL_CONFIG_FILE:-./otel-collector/otel-collector.yaml}"
fi
export PROMETHEUS_CONFIG_FILE OTEL_CONFIG_FILE GRAFANA_PROVISIONING_DIR

start_host_helpers

ensure_compose_images() {
  local saved_grafana_prov="${GRAFANA_PROVISIONING_DIR:-}"
  local saved_prom_config="${PROMETHEUS_CONFIG_FILE:-}"
  local saved_otel_config="${OTEL_CONFIG_FILE:-}"
  local saved_loki_image="${LOKI_IMAGE:-}"
  if [[ -f .env ]]; then
    load_dotenv "${SCRIPT_DIR}/.env"
  fi
  if [[ "${STACK_MODE}" == "minimal" && -f "${DISCOVERED_ENV}" ]]; then
    load_dotenv "${DISCOVERED_ENV}"
  fi
  if [[ -n "${saved_grafana_prov}" ]]; then
    GRAFANA_PROVISIONING_DIR="${saved_grafana_prov}"
    export GRAFANA_PROVISIONING_DIR
  fi
  if [[ -n "${saved_prom_config}" ]]; then
    PROMETHEUS_CONFIG_FILE="${saved_prom_config}"
    export PROMETHEUS_CONFIG_FILE
  fi
  if [[ -n "${saved_otel_config}" ]]; then
    OTEL_CONFIG_FILE="${saved_otel_config}"
    export OTEL_CONFIG_FILE
  fi
  if [[ -n "${saved_loki_image}" ]]; then
    LOKI_IMAGE="${saved_loki_image}"
    export LOKI_IMAGE
  fi
  local prefix="${REGISTRY_PREFIX:-}"
  local gv="${GRAFANA_VERSION:-11.3.0}"
  local grafana_img="${prefix}grafana/grafana:${gv}"
  local legacy_img="${prefix}pymotor/grafana:${gv}"

  if ! "${DOCKER_BIN}" image inspect "${grafana_img}" >/dev/null 2>&1; then
    if "${DOCKER_BIN}" image inspect "grafana/grafana:${gv}" >/dev/null 2>&1; then
      echo "[start] tagging grafana/grafana:${gv} -> ${grafana_img}"
      "${DOCKER_BIN}" tag "grafana/grafana:${gv}" "${grafana_img}"
    fi
  fi
  if ! "${DOCKER_BIN}" image inspect "${legacy_img}" >/dev/null 2>&1 \
    && "${DOCKER_BIN}" image inspect "${grafana_img}" >/dev/null 2>&1; then
    "${DOCKER_BIN}" tag "${grafana_img}" "${legacy_img}" 2>/dev/null || true
  fi
}

ensure_compose_images

COMPOSE_PULL="${OBS_COMPOSE_PULL:-missing}"
COMPOSE_UP_ARGS=(up -d --pull "${COMPOSE_PULL}")
if [[ "${OBS_COMPOSE_BUILD:-0}" == "1" ]]; then
  COMPOSE_UP_ARGS+=(--build)
else
  COMPOSE_UP_ARGS+=(--no-build)
fi

echo "[start] starting Docker Compose stack mode=${STACK_MODE} loki_image=${LOKI_IMAGE} profiles: ${PROFILES:-<none>}"
"${DOCKER_BIN}" compose "${PROFILE_ARGS[@]}" "${COMPOSE_UP_ARGS[@]}"

GRAFANA_PORT="${GRAFANA_PORT:-3000}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
LOKI_PORT="${LOKI_PORT:-3100}"

if [[ "${STACK_MODE}" == "minimal" ]] && command -v curl >/dev/null 2>&1; then
  curl -fsS -X POST "http://localhost:${PROMETHEUS_PORT}/-/reload" >/dev/null 2>&1 || true
fi

cat <<EOF

================================================================
pyMotor observability stack is up.

  Grafana       http://localhost:${GRAFANA_PORT}   (user: motor / pass: motor)
  Prometheus    http://localhost:${PROMETHEUS_PORT}
  Tempo         http://localhost:${TEMPO_QUERY_PORT:-3200}
  Loki          http://localhost:${LOKI_PORT}  (image: ${LOKI_IMAGE})
  OTel OTLP     localhost:${OTEL_GRPC_PORT:-4317} (gRPC) / ${OTEL_HTTP_PORT:-4318} (HTTP)

Mode: ${STACK_MODE}
Active profiles: ${PROFILES:-<none>}

Tips:
  * 推荐入口: ./launch.sh （自动发现 + 自动生成 Prometheus 配置）
  * 当前 Prometheus 配置: ${PROMETHEUS_CONFIG_FILE:-./prometheus/prometheus.yml}
  * Verify tracing: ./scripts/verify-tracing.sh  (OTLP → Tempo)
  * Build Loki locally: LOKI_DOWNLOAD_INSECURE=1 ./scripts/build-loki-image.sh
================================================================
EOF
