#!/usr/bin/env bash
# 验证 observability stack 的 Tracing 通路：OTel Collector (:4317) → Tempo (:3200)
#
# 用法（stack 已 ./start.sh 启动后）：
#   ./scripts/verify-tracing.sh
#   OTEL_HOST=127.0.0.1 ./scripts/verify-tracing.sh
#
# 依赖：python3 + opentelemetry-exporter-otlp-proto-grpc
#   pip install opentelemetry-exporter-otlp-proto-grpc opentelemetry-sdk

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
STACK_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

OTEL_HOST="${OTEL_HOST:-127.0.0.1}"
OTEL_GRPC_PORT="${OTEL_GRPC_PORT:-4317}"
TEMPO_PORT="${TEMPO_QUERY_PORT:-3200}"
SERVICE_NAME="${SERVICE_NAME:-pymotor-tracing-verify}"

if [[ -f "${STACK_DIR}/.env" ]]; then
  OTEL_GRPC_PORT="$(grep -E '^OTEL_GRPC_PORT=' "${STACK_DIR}/.env" 2>/dev/null | tail -n1 | cut -d= -f2 || true)"
  OTEL_GRPC_PORT="${OTEL_GRPC_PORT:-4317}"
  TEMPO_PORT="$(grep -E '^TEMPO_QUERY_PORT=' "${STACK_DIR}/.env" 2>/dev/null | tail -n1 | cut -d= -f2 || true)"
  TEMPO_PORT="${TEMPO_PORT:-3200}"
fi

echo "[verify-tracing] sending test span to ${OTEL_HOST}:${OTEL_GRPC_PORT} (service=${SERVICE_NAME})"

if ! python3 -c "from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter" 2>/dev/null; then
  echo "[verify-tracing] installing opentelemetry packages..." >&2
  pip install -q opentelemetry-exporter-otlp-proto-grpc opentelemetry-sdk
fi

python3 - "${OTEL_HOST}" "${OTEL_GRPC_PORT}" "${SERVICE_NAME}" <<'PY'
import sys
import time
import uuid

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

host, port, service_name = sys.argv[1:4]
endpoint = f"{host}:{port}"

resource = Resource.create({"service.name": service_name})
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
trace.set_tracer_provider(provider)

tracer = trace.get_tracer("pymotor.observability.verify")
with tracer.start_as_current_span("verify-tracing-span") as span:
    span.set_attribute("verify", True)
    span.set_attribute("stack", "pymotor-observability")
    time.sleep(0.05)

provider.force_flush(timeout_millis=5000)
provider.shutdown()
print(f"[verify-tracing] span exported to grpc://{endpoint}")
PY

echo "[verify-tracing] waiting for Tempo ingest..."
sleep 3

SEARCH_URL="http://${OTEL_HOST}:${TEMPO_PORT}/api/search?limit=20"
echo "[verify-tracing] querying Tempo: ${SEARCH_URL}"
RESP="$(curl -sf "${SEARCH_URL}" || true)"

if echo "${RESP}" | grep -q "${SERVICE_NAME}"; then
  echo "[verify-tracing] OK — trace visible in Tempo (service.name=${SERVICE_NAME})"
  echo "[verify-tracing] open Grafana → Explore → Tempo, search service: ${SERVICE_NAME}"
  exit 0
fi

echo "[verify-tracing] WARN — span sent but not yet found in Tempo search response." >&2
echo "[verify-tracing] response snippet: $(echo "${RESP}" | head -c 200)" >&2
echo "[verify-tracing] check: docker compose logs otel-collector tempo | tail -50" >&2
exit 1
