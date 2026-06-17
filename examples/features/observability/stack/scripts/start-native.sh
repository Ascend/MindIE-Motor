#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
STACK_DIR="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
cd "${STACK_DIR}"
. "${SCRIPT_DIR}/load-dotenv.sh"

ENV_FILE="./generated/discovered.env"
PROMETHEUS_FILE="./generated/prometheus.yml"

usage() {
  cat <<'EOF'
Usage: ./scripts/start-native.sh [options]

Options:
  --env-file <path>          Discovered env file (default: ./generated/discovered.env)
  --prometheus-file <path>   Generated prometheus config (default: ./generated/prometheus.yml)
  -h, --help                 Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      [[ $# -lt 2 ]] && { echo "[native] missing value for --env-file" >&2; exit 1; }
      ENV_FILE="$2"
      shift 2
      ;;
    --prometheus-file)
      [[ $# -lt 2 ]] && { echo "[native] missing value for --prometheus-file" >&2; exit 1; }
      PROMETHEUS_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[native] unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -f "${STACK_DIR}/.env" ]]; then
  load_dotenv "${STACK_DIR}/.env"
elif [[ -f "${STACK_DIR}/.env.example" ]]; then
  load_dotenv "${STACK_DIR}/.env.example"
fi
load_dotenv "${ENV_FILE}"

PROXY_SH="${PROXY_SH:-}"
if [[ -n "${PROXY_SH}" && -f "${PROXY_SH}" ]]; then
  load_dotenv "${PROXY_SH}"
  echo "[native] loaded proxy config: ${PROXY_SH}"
fi

RUNTIME_DIR="${STACK_DIR}/.native-runtime"
BIN_DIR="${RUNTIME_DIR}/bin"
LOG_DIR="${RUNTIME_DIR}/logs"
RUN_DIR="${RUNTIME_DIR}/run"
DATA_DIR="${RUNTIME_DIR}/data"
GRAFANA_DIR="${RUNTIME_DIR}/grafana"
GRAFANA_PROVISIONING_DIR="${GRAFANA_DIR}/provisioning"
GRAFANA_DASHBOARD_DIR="${GRAFANA_DIR}/dashboards"

mkdir -p "${BIN_DIR}" "${LOG_DIR}" "${RUN_DIR}" "${DATA_DIR}" \
  "${GRAFANA_PROVISIONING_DIR}/datasources" "${GRAFANA_PROVISIONING_DIR}/dashboards" "${GRAFANA_DASHBOARD_DIR}"

GRAFANA_PORT="${GRAFANA_PORT:-3000}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9090}"
TEMPO_QUERY_PORT="${TEMPO_QUERY_PORT:-3200}"
OTEL_GRPC_PORT="${OTEL_GRPC_PORT:-4317}"
OTEL_HTTP_PORT="${OTEL_HTTP_PORT:-4318}"
TEMPO_OTLP_GRPC_PORT="${TEMPO_OTLP_GRPC_PORT:-14317}"
TEMPO_OTLP_HTTP_PORT="${TEMPO_OTLP_HTTP_PORT:-14318}"
GF_SECURITY_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-motor}"
GF_SECURITY_ADMIN_PASSWORD="${GF_SECURITY_ADMIN_PASSWORD:-motor}"
PROMETHEUS_VERSION="${PROMETHEUS_VERSION:-v2.55.1}"
TEMPO_VERSION="${TEMPO_VERSION:-2.6.1}"
OTEL_COLLECTOR_VERSION="${OTEL_COLLECTOR_VERSION:-0.115.1}"
GRAFANA_VERSION="${GRAFANA_VERSION:-11.3.0}"

download_file() {
  local url="$1"
  local out_file="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --retry-delay 2 -o "${out_file}" "${url}"
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -O "${out_file}" "${url}"
    return
  fi
  echo "[native] neither curl nor wget is available" >&2
  exit 1
}

install_prometheus() {
  local target="${BIN_DIR}/prometheus"
  [[ -x "${target}" ]] && return
  local ver="${PROMETHEUS_VERSION#v}"
  local archive="${RUNTIME_DIR}/prometheus-${ver}.tar.gz"
  local url="https://github.com/prometheus/prometheus/releases/download/${PROMETHEUS_VERSION}/prometheus-${ver}.linux-amd64.tar.gz"
  echo "[native] downloading Prometheus ${PROMETHEUS_VERSION}..."
  download_file "${url}" "${archive}"
  tar -xzf "${archive}" -C "${RUNTIME_DIR}"
  cp "${RUNTIME_DIR}/prometheus-${ver}.linux-amd64/prometheus" "${target}"
  chmod +x "${target}"
}

install_tempo() {
  local target="${BIN_DIR}/tempo"
  [[ -x "${target}" ]] && return
  local ver="${TEMPO_VERSION#v}"
  local archive="${RUNTIME_DIR}/tempo-${ver}.tar.gz"
  local url="https://github.com/grafana/tempo/releases/download/v${ver}/tempo_${ver}_linux_amd64.tar.gz"
  echo "[native] downloading Tempo ${TEMPO_VERSION}..."
  download_file "${url}" "${archive}"
  tar -xzf "${archive}" -C "${RUNTIME_DIR}"
  cp "${RUNTIME_DIR}/tempo" "${target}"
  chmod +x "${target}"
}

install_otel_collector() {
  local target="${BIN_DIR}/otelcol-contrib"
  [[ -x "${target}" ]] && return
  local ver="${OTEL_COLLECTOR_VERSION#v}"
  local archive="${RUNTIME_DIR}/otelcol-contrib-${ver}.tar.gz"
  local url="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${ver}/otelcol-contrib_${ver}_linux_amd64.tar.gz"
  echo "[native] downloading OTel Collector ${OTEL_COLLECTOR_VERSION}..."
  download_file "${url}" "${archive}"
  tar -xzf "${archive}" -C "${RUNTIME_DIR}"
  cp "${RUNTIME_DIR}/otelcol-contrib" "${target}"
  chmod +x "${target}"
}

install_grafana() {
  local grafana_home="${RUNTIME_DIR}/grafana-v${GRAFANA_VERSION}"
  [[ -x "${grafana_home}/bin/grafana" ]] && return
  local archive="${RUNTIME_DIR}/grafana-${GRAFANA_VERSION}.tar.gz"
  local url="https://dl.grafana.com/oss/release/grafana-${GRAFANA_VERSION}.linux-amd64.tar.gz"
  echo "[native] downloading Grafana ${GRAFANA_VERSION}..."
  download_file "${url}" "${archive}"
  tar -xzf "${archive}" -C "${RUNTIME_DIR}"
}

prepare_configs() {
  cat > "${RUNTIME_DIR}/tempo.yaml" <<EOF
server:
  http_listen_port: ${TEMPO_QUERY_PORT}
  grpc_listen_port: 9095

distributor:
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:${TEMPO_OTLP_GRPC_PORT}
        http:
          endpoint: 0.0.0.0:${TEMPO_OTLP_HTTP_PORT}

ingester:
  trace_idle_period: 10s
  max_block_duration: 5m

storage:
  trace:
    backend: local
    wal:
      path: ${DATA_DIR}/tempo/wal
    local:
      path: ${DATA_DIR}/tempo/traces

compactor:
  compaction:
    block_retention: 72h

usage_report:
  reporting_enabled: false
EOF

  cat > "${RUNTIME_DIR}/otel-collector.yaml" <<EOF
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:${OTEL_GRPC_PORT}
      http:
        endpoint: 0.0.0.0:${OTEL_HTTP_PORT}

processors:
  batch:
    send_batch_size: 1024
    timeout: 5s

exporters:
  otlp/tempo:
    endpoint: localhost:${TEMPO_OTLP_GRPC_PORT}
    tls:
      insecure: true
  debug:
    verbosity: basic

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [otlp/tempo, debug]
  telemetry:
    logs:
      level: info
EOF

  if [[ -f "${PROMETHEUS_FILE}" ]]; then
    cp "${PROMETHEUS_FILE}" "${RUNTIME_DIR}/prometheus.yml"
  else
    cp "./prometheus/prometheus.yml" "${RUNTIME_DIR}/prometheus.yml"
  fi

  python3 - <<'PY'
from pathlib import Path
runtime_file = Path(".native-runtime/prometheus.yml")
text = runtime_file.read_text(encoding="utf-8")
text = text.replace("node-exporter:9100", "localhost:9100")
text = text.replace("cadvisor:8080", "localhost:8088")
runtime_file.write_text(text, encoding="utf-8")
PY

  cat > "${GRAFANA_PROVISIONING_DIR}/datasources/datasources.yml" <<EOF
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    uid: prometheus
    access: proxy
    url: http://localhost:${PROMETHEUS_PORT}
    isDefault: true
    jsonData:
      timeInterval: 5s
      httpMethod: POST

  - name: Tempo
    type: tempo
    uid: tempo
    access: proxy
    url: http://localhost:${TEMPO_QUERY_PORT}
    jsonData:
      httpMethod: GET
      nodeGraph:
        enabled: true
      search:
        hide: false
EOF

  cat > "${GRAFANA_PROVISIONING_DIR}/dashboards/dashboard-providers.yml" <<EOF
apiVersion: 1
providers:
  - name: motor-native
    orgId: 1
    folder: ""
    type: file
    disableDeletion: false
    editable: true
    updateIntervalSeconds: 30
    allowUiUpdates: true
    options:
      path: ${GRAFANA_DASHBOARD_DIR}
      foldersFromFilesStructure: false
EOF

  cp "./grafana/dashboards/motor-all-metrics.json" "${GRAFANA_DASHBOARD_DIR}/"
  cp "./grafana/dashboards/motor-kv-cache.json" "${GRAFANA_DASHBOARD_DIR}/"
  cp "./grafana/dashboards/motor-vllm-profiling.json" "${GRAFANA_DASHBOARD_DIR}/"
}

is_pid_running() {
  local pid="$1"
  [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1
}

start_component() {
  local name="$1"
  shift
  local pid_file="${RUN_DIR}/${name}.pid"
  local log_file="${LOG_DIR}/${name}.log"
  if [[ -f "${pid_file}" ]]; then
    local old_pid
    old_pid="$(<"${pid_file}")"
    if is_pid_running "${old_pid}"; then
      echo "[native] ${name} already running (pid=${old_pid})"
      return 0
    fi
  fi
  nohup "$@" >"${log_file}" 2>&1 &
  local new_pid=$!
  echo "${new_pid}" > "${pid_file}"
  echo "[native] started ${name} (pid=${new_pid})"
}

install_prometheus
install_tempo
install_otel_collector
install_grafana
prepare_configs

mkdir -p "${DATA_DIR}/tempo" "${DATA_DIR}/prometheus" "${DATA_DIR}/grafana"

start_component "tempo" \
  "${BIN_DIR}/tempo" \
  "-config.file=${RUNTIME_DIR}/tempo.yaml"

start_component "otel-collector" \
  "${BIN_DIR}/otelcol-contrib" \
  "--config=${RUNTIME_DIR}/otel-collector.yaml"

start_component "prometheus" \
  "${BIN_DIR}/prometheus" \
  "--config.file=${RUNTIME_DIR}/prometheus.yml" \
  "--storage.tsdb.path=${DATA_DIR}/prometheus" \
  "--storage.tsdb.retention.time=72h" \
  "--web.listen-address=:${PROMETHEUS_PORT}" \
  "--web.enable-lifecycle" \
  "--web.enable-remote-write-receiver" \
  "--enable-feature=utf8-names"

GRAFANA_HOME="${RUNTIME_DIR}/grafana-v${GRAFANA_VERSION}"
start_component "grafana" \
  env \
  GF_SECURITY_ADMIN_USER="${GF_SECURITY_ADMIN_USER}" \
  GF_SECURITY_ADMIN_PASSWORD="${GF_SECURITY_ADMIN_PASSWORD}" \
  GF_USERS_ALLOW_SIGN_UP="false" \
  GF_LOG_LEVEL="warn" \
  GF_SERVER_HTTP_PORT="${GRAFANA_PORT}" \
  GF_PATHS_PROVISIONING="${GRAFANA_PROVISIONING_DIR}" \
  GF_PATHS_DATA="${DATA_DIR}/grafana" \
  "${GRAFANA_HOME}/bin/grafana" server --homepath "${GRAFANA_HOME}"

cat <<EOF

================================================================
pyMotor observability stack is running in native mode.

  Grafana       http://localhost:${GRAFANA_PORT}   (user: ${GF_SECURITY_ADMIN_USER} / pass: ${GF_SECURITY_ADMIN_PASSWORD})
  Prometheus    http://localhost:${PROMETHEUS_PORT}
  Tempo         http://localhost:${TEMPO_QUERY_PORT}
  OTel OTLP     localhost:${OTEL_GRPC_PORT} (gRPC) / ${OTEL_HTTP_PORT} (HTTP)

Runtime dir: ${RUNTIME_DIR}
Logs dir:    ${LOG_DIR}
================================================================
EOF
