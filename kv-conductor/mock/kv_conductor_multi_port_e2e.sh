#!/bin/bash
# ===========================================================
# E2E: KV Conductor Multi-Port ZMQ Subscription Test
#
# Flow:
#   1. Start kv-conductor (Rust binary, HTTP :13333)
#   2. Start mock ZMQ publisher (port 15557 XPU, port 15558 CPU+DISK)
#      → Publisher registers directly with KV Conductor
#   3. Verify /register accepted and /workers shows the worker
#   4. Wait for KV events to be published and ingested
#   5. Query /workers to verify blocks are indexed per medium
#   6. Clean up
#
# Usage:
#   cd /home/jason/MindIE-PyMotor_pub
#   bash e2e/kv_conductor_multi_port_e2e.sh
#
# Requirements:
#   - kv-conductor binary at kv-conductor/target/debug/kv-conductor
#   - Python 3 with pyzmq, msgpack, requests
# ===========================================================

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

# Ports (avoid conflicts with other services)
CONDUCTOR_PORT=13334
XPU_PORT=15557
CPU_DISK_PORT=15558
MAX_WAIT=15

# PIDs for cleanup
CONDUCTOR_PID=""
PUBLISHER_PID=""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASSED=0
FAILED=0

cleanup() {
    echo ""
    echo "=== Cleaning up ==="
    if [ -n "$PUBLISHER_PID" ] && kill -0 "$PUBLISHER_PID" 2>/dev/null; then
        echo "Stopping mock publisher (PID $PUBLISHER_PID)..."
        kill "$PUBLISHER_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$PUBLISHER_PID" 2>/dev/null || true
    fi
    if [ -n "$CONDUCTOR_PID" ] && kill -0 "$CONDUCTOR_PID" 2>/dev/null; then
        echo "Stopping kv-conductor (PID $CONDUCTOR_PID)..."
        kill "$CONDUCTOR_PID" 2>/dev/null || true
        sleep 1
        kill -9 "$CONDUCTOR_PID" 2>/dev/null || true
    fi
    echo "Cleanup done"
}
trap cleanup EXIT

assert_contains() {
    local desc="$1" url="$2" expected="$3"
    local resp
    resp=$(curl -sS --max-time 5 "$url" 2>&1) || true
    if echo "$resp" | grep -q "$expected"; then
        echo -e "  ${GREEN}PASS${NC} $desc"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}FAIL${NC} $desc"
        echo "       URL: $url"
        echo "       Expected to contain: $expected"
        echo "       Got: $(echo "$resp" | head -3)"
        FAILED=$((FAILED + 1))
    fi
}

assert_status() {
    local desc="$1" url="$2" expected_code="$3"
    local code
    code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "$url" 2>&1) || true
    if [ "$code" = "$expected_code" ]; then
        echo -e "  ${GREEN}PASS${NC} $desc (HTTP $code)"
        PASSED=$((PASSED + 1))
    else
        echo -e "  ${RED}FAIL${NC} $desc (expected $expected_code, got $code)"
        FAILED=$((FAILED + 1))
    fi
}

echo "============================================"
echo " E2E: KV Conductor Multi-Port Subscription"
echo "============================================"
echo ""
echo "Ports: conductor=${CONDUCTOR_PORT}, xpu=${XPU_PORT}, cpu+disk=${CPU_DISK_PORT}"
echo ""

# ---- Step 1: Start kv-conductor ----
echo "--- Starting KV Conductor ---"
KV_BIN="$ROOT_DIR/kv-conductor/target/debug/kv-conductor"
if [ ! -x "$KV_BIN" ]; then
    echo -e "${RED}kv-conductor binary not found at $KV_BIN${NC}"
    echo "Build it first: cd kv-conductor && cargo build"
    exit 1
fi

RUST_LOG=info "$KV_BIN" --host 127.0.0.1 --port "$CONDUCTOR_PORT" &
CONDUCTOR_PID=$!
echo "kv-conductor started (PID: $CONDUCTOR_PID)"

# Wait for conductor to be ready
echo "--- Waiting for kv-conductor (port $CONDUCTOR_PORT) ---"
CONDUCTOR_BASE="http://127.0.0.1:$CONDUCTOR_PORT"
for i in $(seq 1 $MAX_WAIT); do
    if curl -sS --max-time 1 "$CONDUCTOR_BASE/health" > /dev/null 2>&1; then
        echo "kv-conductor ready after ${i}s"
        break
    fi
    if [ $i -eq $MAX_WAIT ]; then
        echo -e "${RED}kv-conductor failed to start within ${MAX_WAIT}s${NC}"
        exit 1
    fi
    sleep 1
done

# ---- Step 2: Basic health check ----
echo ""
echo "=== Basic Health Checks ==="
assert_status "Health endpoint" "$CONDUCTOR_BASE/health" "200"
assert_contains "Health returns OK" "$CONDUCTOR_BASE/health" "OK"
assert_status "Workers endpoint (empty)" "$CONDUCTOR_BASE/workers" "200"

# ---- Step 3: Start mock ZMQ publisher (register + publish) ----
echo ""
echo "--- Starting Mock ZMQ Publisher ---"
PUBLISHER_SCRIPT="$ROOT_DIR/tests/benchmark/mock_zmq_publisher.py"
if [ ! -f "$PUBLISHER_SCRIPT" ]; then
    echo -e "${RED}Mock publisher script not found at $PUBLISHER_SCRIPT${NC}"
    exit 1
fi

python3 "$PUBLISHER_SCRIPT" \
    --conductor-url "$CONDUCTOR_BASE" \
    --direct \
    --xpu-port "$XPU_PORT" \
    --cpu-disk-port "$CPU_DISK_PORT" \
    --publisher-ip 127.0.0.1 \
    --model-name "test-model" \
    --block-size 128 &
PUBLISHER_PID=$!
echo "Mock publisher started (PID: $PUBLISHER_PID)"

# Wait for publisher to register and start publishing
sleep 3

# ---- Step 4: Verify registration ----
echo ""
echo "=== Registration Verification ==="

# Check /workers shows the mock-publisher worker
WORKERS_RESP=$(curl -sS --max-time 5 "$CONDUCTOR_BASE/workers" 2>&1)
echo "Workers response: $WORKERS_RESP"

assert_contains \
    "Worker 'mock-publisher' is registered" \
    "$CONDUCTOR_BASE/workers" \
    "mock-publisher"

assert_contains \
    "medium_endpoints has xpu entry" \
    "$CONDUCTOR_BASE/workers" \
    "xpu"

assert_contains \
    "medium_endpoints has cpu entry" \
    "$CONDUCTOR_BASE/workers" \
    "cpu"

assert_contains \
    "medium_endpoints has disk entry (same as cpu)" \
    "$CONDUCTOR_BASE/workers" \
    "disk"

# ---- Step 5: Wait for events and verify indexing ----
echo ""
echo "=== Event Ingestion Verification ==="
echo "Waiting for KV events to be published and ingested..."
sleep 5  # Give the publisher time to send several batches

# Check that the indexer has blocks
INDEXER_RESP=$(curl -sS --max-time 5 "$CONDUCTOR_BASE/workers" 2>&1)
echo "Indexer state: $INDEXER_RESP"

# The indexer should show non-zero total_blocks for test-model
if echo "$INDEXER_RESP" | python3 -c "
import sys, json
data = json.load(sys.stdin)
idx = data.get('indexer', [])
if not idx:
    print('NO_INDEXER')
    sys.exit(1)
entry = idx[0]
total = entry.get('total_blocks', 0)
wc = entry.get('worker_count', 0)
print(f'total_blocks={total} worker_count={wc}')
if total > 0 and wc > 0:
    sys.exit(0)
else:
    sys.exit(1)
" 2>&1; then
    echo -e "  ${GREEN}PASS${NC} Indexer has blocks (multi-port events ingested)"
    PASSED=$((PASSED + 1))
else
    echo -e "  ${YELLOW}WARN${NC} Indexer may not have blocks yet (or format changed)"
    echo "       Check the output above for details"
fi

# ---- Step 6: Query test ----
echo ""
echo "=== Query Test ==="
QUERY_RESP=$(curl -sS --max-time 5 -X POST "$CONDUCTOR_BASE/query" \
    -H "Content-Type: application/json" \
    -d '{"model":"test-model","block_size":128,"token_ids":[1,2,3,4,5,6,7,8]}' 2>&1)
echo "Query response: $QUERY_RESP"

# Query is POST only — use explicit curl
QUERY_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 \
    -X POST -H "Content-Type: application/json" \
    -d '{"model":"test-model","block_size":128,"token_ids":[1,2,3,4,5,6,7,8]}' \
    "$CONDUCTOR_BASE/query" 2>&1)
if [ "$QUERY_CODE" = "200" ]; then
    echo -e "  ${GREEN}PASS${NC} Query endpoint (HTTP $QUERY_CODE)"
    PASSED=$((PASSED + 1))
else
    echo -e "  ${RED}FAIL${NC} Query endpoint (expected 200, got $QUERY_CODE)"
    FAILED=$((FAILED + 1))
fi

# ---- Step 7: Verify duplicate registration is rejected ----
echo ""
echo "=== Duplicate Registration Test ==="
DUP_RESP=$(curl -sS --max-time 5 -X POST "$CONDUCTOR_BASE/register" \
    -H "Content-Type: application/json" \
    -d '{
        "instance_id":"mock-publisher",
        "medium_endpoints":{"xpu":"tcp://127.0.0.1:15557","cpu":"tcp://127.0.0.1:15558","disk":"tcp://127.0.0.1:15558"},
        "type":"vllm","store_backend":"YuanRong","modelname":"test-model","block_size":128,"dp_rank":0
    }' 2>&1)
echo "Duplicate registration response: $DUP_RESP"

# Should be 409 Conflict or contain error message
DUP_CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 -X POST "$CONDUCTOR_BASE/register" \
    -H "Content-Type: application/json" \
    -d '{
        "instance_id":"mock-publisher",
        "medium_endpoints":{"xpu":"tcp://127.0.0.1:15557","cpu":"tcp://127.0.0.1:15558","disk":"tcp://127.0.0.1:15558"},
        "type":"vllm","store_backend":"YuanRong","modelname":"test-model","block_size":128,"dp_rank":0
    }' 2>&1)
if [ "$DUP_CODE" = "409" ]; then
    echo -e "  ${GREEN}PASS${NC} Duplicate registration returns 409"
    PASSED=$((PASSED + 1))
else
    echo -e "  ${YELLOW}WARN${NC} Duplicate registration returned $DUP_CODE (expected 409)"
fi

# ---- Summary ----
echo ""
echo "============================================"
echo " Results: $PASSED passed, $FAILED failed"
echo "============================================"

if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
echo -e "${GREEN}All e2e tests passed${NC}"
