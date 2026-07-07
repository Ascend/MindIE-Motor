#!/bin/bash
# KV Conductor CLI — register publishers, query cache hits, inspect state.
#
# Usage:
#   ./conductor_cli.sh up                          # Deploy multi-medium publishers + conductor
#   ./conductor_cli.sh up --single-port            # Legacy single-port mode
#   ./conductor_cli.sh down                        # Tear down
#   ./conductor_cli.sh register                    # Register all publishers
#   ./conductor_cli.sh status                      # Workers + block counts
#   ./conductor_cli.sh query-tokens --count 256    # Query by token IDs
#   ./conductor_cli.sh bench                       # Benchmark
#   ./conductor_cli.sh quick                       # One-shot: register → wait → status
#   ./conductor_cli.sh health                      # Health check
#
# Environment:
#   KV_NAMESPACE      K8s namespace (default: mindie-motor)
#   CONDUCTOR_ADDR    Conductor address (default: localhost:13333)
#   BLOCK_SIZE        KV block size (default: 128)
#   NUM_PUBLISHERS    Number of publisher pods (default: 8)

set -euo pipefail

_check_deps() {
    local missing=()
    command -v curl    &>/dev/null || missing+=("curl")
    command -v python3 &>/dev/null || missing+=("python3")
    if [[ -z "${SKIP_KUBECTL_CHECK:-}" ]]; then
        command -v kubectl &>/dev/null || missing+=("kubectl")
    fi
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo -e "\033[0;31mMissing dependencies: ${missing[*]}\033[0m" >&2
        echo "Install with: apt install ${missing[*]}" >&2
        exit 1
    fi
}
_check_deps

NAMESPACE="${KV_NAMESPACE:-mindie-motor}"
CONDUCTOR_ADDR="${CONDUCTOR_ADDR:-localhost:13333}"
BASE_URL="http://${CONDUCTOR_ADDR}"

NUM_PUBLISHERS="${NUM_PUBLISHERS:-8}"
MODEL_NAME="opt-125m"
BLOCK_SIZE="${BLOCK_SIZE:-128}"
TENANT_ID="default"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
YAML_FILE="$SCRIPT_DIR/e2e_test.yaml"

usage() {
    cat << 'EOF'
KV Conductor CLI — build, deploy, register, query, all-in-one.

Usage:  ./conductor_cli.sh <command> [options]

Setup:
  build                 Build all Docker images
  up [flags]            Deploy multi-medium publishers + conductor (default)
  down                  Tear down all resources
  logs [filter]         Collect logs (see collect_logs.sh)

Runtime:
  register              Register publishers with conductor
  unregister            Unregister mock publishers
  status                Show workers and block counts
  query-tokens [opts]   Query cache hits by raw token IDs
  bench [opts]          Benchmark query throughput
  quick                 One-shot: register → wait → status
  health                Health check
  smoke                 API smoke test

Flags for 'up':
  --single-port         Single-port legacy mode (default: multi-medium)
  --mooncake-format     Legacy Mooncake wire format (default: vLLM msgspec)
  --no-swa-mixed        Disable SWA attention event mixing
  --block-size N        KV block size (default: 128)
  --initial-blocks N    Initial cache capacity (default: 8192)

Options for 'query-tokens':
  --model NAME          Model name (default: opt-125m)
  --count N             Number of sequential tokens (default: 128)
  --tenant ID           Tenant ID (default: default)

Options for 'bench':
  --block-size N        Block size (default: 128)
  --tokens N            Tokens per query (default: 512)
  --count N             Number of queries (default: 20)
  --throughput          Throughput mode (by hash, 100% hits)

Environment:
  KV_NAMESPACE          K8s namespace (default: mindie-motor)
  CONDUCTOR_ADDR        Conductor address (default: localhost:13333)
  BLOCK_SIZE            KV block size (default: 128)
  NUM_PUBLISHERS        Number of publisher pods (default: 8)

Quick start:
  ./conductor_cli.sh build && ./conductor_cli.sh up
  kubectl -n mindie-motor port-forward deploy/mindie-motor-kv-conductor 13333:13333 &
  ./conductor_cli.sh quick
  ./conductor_cli.sh query-tokens --count 256
EOF
}

# ── HTTP helpers ───────────────────────────────────────────────────────

api_get() {
    curl -s --max-time 3 "${BASE_URL}$1" 2>/dev/null
}

api_post() {
    curl -s --max-time 3 -w "\n%{http_code}" -X POST "${BASE_URL}$1" \
        -H 'Content-Type: application/json' -d "$2" 2>/dev/null
}

# ── K8s helpers ────────────────────────────────────────────────────────

pod_ip() {
    kubectl -n "$NAMESPACE" get pod -l "app=$1" \
        -o jsonpath='{.items[0].status.podIP}' 2>/dev/null
}

# ── Register / Unregister ──────────────────────────────────────────────

cmd_register() {
    echo -e "${BOLD}Registering ${NUM_PUBLISHERS} publishers → ${CONDUCTOR_ADDR}${NC}\n"

    for ((dp=0; dp<NUM_PUBLISHERS; dp++)); do
        local svc="zmq-publisher-${dp}"
        local xpu_port=$((15557 + dp * 2)) cpu_port=$((15557 + dp * 2 + 1))
        local iid="mock-publisher-${dp}"
        local ip; ip=$(pod_ip "$svc")
        [[ -z "$ip" ]] && { echo -e "  ${RED}dp=${dp}: pod not found${NC}"; continue; }

        printf "  dp=%-2d tcp://%s:%d (XPU) + :%d (CPU/DISK) ... " "$dp" "$ip" "$xpu_port" "$cpu_port"

        local medium_endpoints
        medium_endpoints=$(python3 -c "
import json
print(json.dumps({
    'xpu':  'tcp://${ip}:${xpu_port}',
    'cpu':  'tcp://${ip}:${cpu_port}',
    'disk': 'tcp://${ip}:${cpu_port}',
}))
")
        local resp; resp=$(api_post "/register" "{
            \"instance_id\": \"${iid}\",
            \"medium_endpoints\": ${medium_endpoints},
            \"type\": \"Mooncake\",
            \"store_backend\": \"YuanRong\",
            \"modelname\": \"${MODEL_NAME}\",
            \"block_size\": ${BLOCK_SIZE},
            \"dp_rank\": ${dp},
            \"tenant_id\": \"${TENANT_ID}\"
        }")
        local code; code=$(echo "$resp" | tail -1)
        case "$code" in
            201|200) echo -e "${GREEN}OK${NC}" ;;
            409)     echo -e "${YELLOW}REGISTERED${NC}" ;;
            *)       echo -e "${RED}FAIL${NC} ($code)" ;;
        esac
    done
    echo ""
}

cmd_unregister() {
    echo -e "${BOLD}Unregistering${NC}\n"
    for ((dp=0; dp<NUM_PUBLISHERS; dp++)); do
        local iid="mock-publisher-${dp}"
        printf "  dp=%-2d ... " "$dp"
        local resp; resp=$(api_post "/unregister" "{
            \"instance_id\": \"${iid}\", \"type\": \"Mooncake\",
            \"modelname\": \"${MODEL_NAME}\", \"block_size\": ${BLOCK_SIZE},
            \"dp_rank\": ${dp}, \"tenant_id\": \"${TENANT_ID}\"
        }")
        local code; code=$(echo "$resp" | tail -1)
        [[ "$code" == "200" ]] && echo -e "${GREEN}OK${NC}" || echo -e "${YELLOW}N/A${NC}"
    done
    echo ""
}

# ── Status ─────────────────────────────────────────────────────────────

cmd_status() {
    local json; json=$(api_get "/workers")

    if [[ -z "$json" ]]; then
        echo -e "${RED}Cannot reach ${CONDUCTOR_ADDR}${NC}"
        echo "  Start: kubectl -n ${NAMESPACE} port-forward deploy/mindie-motor-kv-conductor 13333:13333 &"
        return 1
    fi

    echo -e "${BOLD}KV Conductor Status${NC}\n"
    echo "$json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
for e in d.get('indexer', []):
    print(f'  model:      {e[\"model_name\"]}/{e[\"tenant_id\"]}')
    print(f'  blocks:     {e[\"total_blocks\"]}')
    print(f'  workers:    {e[\"worker_count\"]}')
    print(f'  block_size: {e[\"block_size\"]}')
print()
ws = d.get('workers', [])
if ws:
    print(f'  {len(ws)} registered worker(s):')
    for w in ws:
        for dp, info in w.get('endpoints', {}).items():
            meps = info.get('medium_endpoints', {})
            if meps:
                ep_media = {}
                for medium, ep in sorted(meps.items()):
                    ep_media.setdefault(ep, []).append(medium.upper())
                ep_strs = [f'{ep} ({\",\".join(media)})' for ep, media in ep_media.items()]
                print(f'    {w[\"instance_id\"]:30s}  dp={dp}  {info[\"engine_type\"]:8s}  {\" | \".join(ep_strs)}')
            else:
                ep = info.get('endpoint', 'N/A')
                print(f'    {w[\"instance_id\"]:30s}  dp={dp}  {info[\"engine_type\"]:8s}  {ep}')
else:
    print('  (no workers)')
" 2>/dev/null || echo "$json"
}

# ── Query ──────────────────────────────────────────────────────────────

cmd_query_tokens() {
    local model="$MODEL_NAME" tenant="$TENANT_ID" bs="$BLOCK_SIZE"
    local cnt=128 tokens=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --model)  model="$2"; shift 2 ;;
            --tenant) tenant="$2"; shift 2 ;;
            --count)  cnt="$2"; shift 2 ;;
            *)        tokens+=("$1"); shift ;;
        esac
    done

    if [[ ${#tokens[@]} -eq 0 ]]; then
        for ((i=0; i<cnt; i++)); do tokens+=("$i"); done
        echo "Using ${#tokens[@]} sequential tokens (0..$((cnt-1)))"
    fi

    local tlist; tlist=$(IFS=','; echo "${tokens[*]}")

    echo -e "${BOLD}Query: ${#tokens[@]} tokens, model=${model}, block_size=${bs}${NC}\n"

    local resp body code
    resp=$(api_post "/query" "{
        \"model\": \"${model}\",
        \"block_size\": ${bs},
        \"token_ids\": [${tlist}],
        \"tenant_id\": \"${tenant}\"
    }")
    code=$(echo "$resp" | tail -1)
    body=$(echo "$resp" | sed '$d')

    if [[ "$code" != "200" ]]; then
        echo -e "${RED}Query failed (${code}):${NC} $body"
        return 1
    fi
    format_hits "$body" "$bs"
}

format_hits() {
    local json="$1" bs="$2"
    echo "$json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
bs = int('$bs')
for tenant_id, instances in d.items():
    if not instances:
        print('  (no matches)'); continue
    print(f'  tenant: {tenant_id}')
    header = f'  {\"instance\":<30s} {\"score\":>6s} {\"XPU\":>8s} {\"CPU\":>8s} {\"DISK\":>8s} {\"match_tok\":>10s}'
    print(header)
    print(f'  {\"-\"*30} {\"-\"*6} {\"-\"*8} {\"-\"*8} {\"-\"*8} {\"-\"*10}')
    for inst_id, imd in sorted(instances.items()):
        best_t = imd.get('longest_matched', 0)
        total_score = imd.get('total_score', 0)
        # Per-DP scoring breakdown with blocks
        dps = imd.get('DP', {})
        if dps:
            for rank, ds in sorted(dps.items()):
                if not isinstance(ds, dict):
                    continue
                x_s = ds.get('XPU', 0)
                c_s = ds.get('CPU', 0)
                d_s = ds.get('DISK', 0)
                total = ds.get('total', 0)
                mt = ds.get('matched_tokens', 0)
                xpu_b = ds.get('XPU_blk', 0)
                cpu_b = ds.get('CPU_blk', 0)
                disk_b = ds.get('DISK_blk', 0)
                blk_parts = []
                if xpu_b: blk_parts.append(f'XPU={xpu_b * bs}t({xpu_b}blk)')
                if cpu_b: blk_parts.append(f'CPU={cpu_b * bs}t({cpu_b}blk)')
                if disk_b: blk_parts.append(f'DISK={disk_b * bs}t({disk_b}blk)')
                blk_str = ' '.join(blk_parts) if blk_parts else '-'
                label = inst_id if rank == sorted(dps.keys())[0] else ''
                print(f'  {label:<30s} {total_score if label else \"\":>6}  {x_s:>4}/{xpu_b}blk {c_s:>4}/{cpu_b}blk {d_s:>4}/{disk_b}blk {mt:>8}t  {blk_str}')
                total_score = ''  # only show once
    print()
" 2>/dev/null || echo "$json"
}

# ── Bench ──────────────────────────────────────────────────────────────

cmd_bench() {
    local model="$MODEL_NAME" tenant="$TENANT_ID" bs="$BLOCK_SIZE"
    local count=20 tokens_per=512 throughput=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --model)  model="$2"; shift 2 ;;
            --tenant) tenant="$2"; shift 2 ;;
            --count)  count="$2"; shift 2 ;;
            --tokens) tokens_per="$2"; shift 2 ;;
            --block-size) bs="$2"; shift 2 ;;
            --throughput) throughput="1"; shift ;;
            *) shift ;;
        esac
    done

    local mode="realistic"
    if [[ -n "$throughput" ]]; then
        mode="throughput"
    fi

    echo -e "${BOLD}Benchmark: ${count} queries, bs=${bs}, mode=${mode}${NC}\n"

    # Pre-extract hashes for throughput mode
    local cached_hashes=""
    if [[ -n "$throughput" ]]; then
        for deploy_name in zmq-publisher-0; do
            cached_hashes=$(kubectl -n "$NAMESPACE" logs "deploy/${deploy_name}" --tail=200 2>/dev/null \
                | grep 'STORE' \
                | grep -oP 'seq_hashes=\[([0-9, ]+)\]' \
                | tail -1 | grep -oP '[0-9]+' | head -"$tokens_per" | tr '\n' ' ')
            [[ -n "$cached_hashes" ]] && break
        done
        if [[ -z "$cached_hashes" ]]; then
            echo -e "  ${YELLOW}Cannot extract hashes — falling back to realistic.${NC}"
            mode="realistic"
        fi
    fi

    python3 -c "
import json, random, time, urllib.request, os

count = $count
block_size = $bs
model = '$model'
tenant = '$tenant'
mode = '$mode'
cached = '$cached_hashes'

# Load shared token pool; fall back to inline pool if import fails.
try:
    pool_dir = '$SCRIPT_DIR'
    if pool_dir not in ('', None):
        os.chdir(pool_dir)
    from token_pool import generate_tokens
except ImportError:
    def generate_tokens(dp, bs, bi):
        return [(abs(hash(f'{dp}:{bi}:{p}')) % 50000 + 100) for p in range(bs)]

all_hashes = [int(h) for h in cached.split() if h.strip()] if cached else []
num_hashes = len(all_hashes)

hits = 0; misses = 0; total_blocks = 0; max_blocks = 0; best_worker = None
latencies = []; total_tok_matched = 0
block_hits_by_worker = {}

for i in range(count):
    if mode == 'throughput' and num_hashes > 0:
        win = min($tokens_per // block_size if block_size else 8, num_hashes)
        win = max(1, win)
        start = random.randint(0, max(0, num_hashes - win))
        query_hashes = all_hashes[start:start + win]
        endpoint = '/query_by_hash'
        data = json.dumps({
            'model': model, 'block_size': block_size,
            'block_hashes': query_hashes, 'tenant_id': tenant,
        }).encode()
    else:
        # Use deterministic token generation matching the mock publisher.
        # Each block gets unique tokens via XXH3 hash — no cycling.
        # Align block_index to batch boundary (multiple of 16) so HBM
        # prefix tree matches from root.children chain heads.
        dp = random.randint(0, 7)
        block_index = random.randint(250, 512) * 16
        blocks_needed = $tokens_per // block_size if block_size else 1
        tokens = []
        for bi in range(block_index, block_index + blocks_needed):
            tokens.extend(generate_tokens(dp, block_size, bi))
        endpoint = '/query'
        data = json.dumps({
            'model': model, 'block_size': block_size,
            'token_ids': tokens[:$tokens_per], 'tenant_id': tenant,
        }).encode()

    t0 = time.time()
    try:
        req = urllib.request.Request(
            f'http://${CONDUCTOR_ADDR}{endpoint}',
            data=data, headers={'Content-Type': 'application/json'}
        )
        resp = urllib.request.urlopen(req, timeout=2)
        result = json.loads(resp.read())
        lat = (time.time() - t0) * 1000
        latencies.append(lat)

        matched = 0; worker = None; best_score = 0
        for tid, instances in result.items():
            for inst_id, imd in instances.items():
                score = imd.get('total_score', 0)
                if score > best_score:
                    best_score = score; worker = inst_id
                # Per-DP scoring: DP contains {XPU, CPU, DISK, total, XPU_blk, CPU_blk, DISK_blk, matched_tokens}
                for rank, ds in imd.get('DP', {}).items():
                    key = f'{inst_id}/dp={rank}'
                    if isinstance(ds, dict):
                        s = ds.get('total', 0)
                        xpu_b = ds.get('XPU_blk', 0)
                        cpu_b = ds.get('CPU_blk', 0)
                        disk_b = ds.get('DISK_blk', 0)
                        block_hits_by_worker[key] = block_hits_by_worker.get(key, 0) + s
                        if i < 3 and (xpu_b or cpu_b or disk_b):
                            print(f'        dp={rank} XPU={xpu_b} CPU={cpu_b} DISK={disk_b} blocks', flush=True)
                    else:
                        # Legacy format: plain integer (backward compat)
                        bk = ds // block_size if block_size else 0
                        block_hits_by_worker[key] = block_hits_by_worker.get(key, 0) + bk
                b = imd.get('longest_matched', 0) // block_size if block_size else 0
                if b > matched:
                    matched = b
        if best_score > 0:
            hits += 1; total_blocks += matched
            total_tok_matched += matched * block_size
            if matched > max_blocks:
                max_blocks = matched; best_worker = worker
        else:
            misses += 1
    except Exception as e:
        misses += 1
        if i < 3:
            print(f'  [{i+1}/{count}] ERROR: {e}', flush=True)

    if (i + 1) % max(1, count // 4) == 0:
        avg_lat = sum(latencies)/len(latencies) if latencies else 0
        print(f'  [{i+1}/{count}] hits={hits} miss={misses} avg_lat={avg_lat:.1f}ms', flush=True)

print()
print('  Results:')
print(f'    queries:         {count}')
hit_pct = hits * 100 // count if count else 0
print(f'    hits:            {hits}  ({hit_pct}%)')
print(f'    misses:          {misses}  ({100-hit_pct}%)')
if hits > 0:
    print(f'    avg blocks hit:  {total_blocks/hits:.1f}')
    print(f'    avg tokens hit:  {total_tok_matched/hits:.0f}')
    print(f'    max blocks:      {max_blocks}  ({best_worker})')
    print(f'    best score:      {best_score}')
    if block_hits_by_worker:
        print('    per-worker (score):')
        for wk, sc in sorted(block_hits_by_worker.items(), key=lambda x: -x[1]):
            print(f'      {wk:35s}  {sc:>6d} pts')
if latencies:
    latencies.sort()
    print('    latency (ms):')
    print(f'      p50={latencies[len(latencies)//2]:.1f}  '
          f'p90={latencies[min(len(latencies)-1, len(latencies)*90//100)]:.1f}  '
          f'p99={latencies[min(len(latencies)-1, len(latencies)*99//100)]:.1f}  '
          f'max={latencies[-1]:.1f}')
"
}

# ── Smoke test ─────────────────────────────────────────────────────────

cmd_smoke() {
    echo -e "${BOLD}=== API Smoke Test ===${NC}\n"
    local pass=0 fail=0 total=7
    local iid="smoke-test-$(date +%s)"

    check() {
        local desc="$1" ok="$2"
        if [[ "$ok" == "1" ]]; then
            echo -e "  ${GREEN}[PASS]${NC} $desc"; pass=$((pass + 1))
        else
            echo -e "  ${RED}[FAIL]${NC} $desc"; fail=$((fail + 1))
        fi
    }

    echo "  1. GET /health"
    local resp; resp=$(api_get "/health")
    check "/health" "$([[ "$resp" == "OK" ]] && echo 1 || echo 0)"

    echo "  2. POST /register"
    local reg_resp reg_code
    reg_resp=$(api_post "/register" "{
        \"instance_id\": \"${iid}\", \"endpoint\": \"tcp://10.0.0.1:5557\",
        \"type\": \"vllm\", \"modelname\": \"smoke-model\",
        \"block_size\": 4, \"dp_rank\": 0, \"tenant_id\": \"default\"
    }")
    reg_code=$(echo "$reg_resp" | tail -1)
    check "/register (201)" "$([[ "$reg_code" == "201" ]] && echo 1 || echo 0)"

    echo "  3. GET /workers"
    local wk; wk=$(api_get "/workers")
    check "/workers" "$(echo "$wk" | python3 -c "import json,sys; d=json.load(sys.stdin); print(1 if d.get('workers') else 0)" 2>/dev/null || echo 0)"

    echo "  4. POST /events"
    local ev_body; ev_body=$(api_post "/events" "{
        \"instance_id\": \"${iid}\",
        \"model_name\": \"smoke-model\", \"tenant_id\": \"default\",
        \"block_size\": 4,
        \"events\": [{
            \"event_id\": 1,
            \"data\": {
                \"type\": \"stored\", \"parent_hash\": null,
                \"blocks\": [{\"block_hash\": 100, \"tokens_hash\": 1}]
            }, \"dp_rank\": 0
        }], \"shutdown\": false
    }" | sed '$d')
    local ev_ok; ev_ok=$(echo "$ev_body" | python3 -c "import json,sys; d=json.load(sys.stdin); print(1 if d.get('events_applied',0)>0 else 0)" 2>/dev/null || echo 0)
    check "/events (applied=$ev_ok)" "$ev_ok"

    echo "  5. POST /query"
    local q_code; q_code=$(api_post "/query" '{
        "model": "smoke-model", "block_size": 4,
        "token_ids": [1,2,3,4,5,6,7,8], "tenant_id": "default"
    }' | tail -1)
    check "/query (200)" "$([[ "$q_code" == "200" ]] && echo 1 || echo 0)"

    echo "  6. POST /query_by_hash"
    local qh_code; qh_code=$(api_post "/query_by_hash" '{
        "model": "smoke-model", "block_size": 4,
        "block_hashes": [1], "tenant_id": "default"
    }' | tail -1)
    check "/query_by_hash (200)" "$([[ "$qh_code" == "200" ]] && echo 1 || echo 0)"

    echo "  7. POST /unregister"
    local unreg_code; unreg_code=$(api_post "/unregister" "{
        \"instance_id\": \"${iid}\", \"type\": \"vllm\",
        \"modelname\": \"smoke-model\", \"block_size\": 4,
        \"dp_rank\": 0, \"tenant_id\": \"default\"
    }" | tail -1)
    check "/unregister (200)" "$([[ "$unreg_code" == "200" ]] && echo 1 || echo 0)"

    echo ""
    echo -e "  ${BOLD}Result:${NC} ${GREEN}$pass passed${NC}, ${RED}$fail failed${NC}, $total total"
    echo ""
}

# ── Health ─────────────────────────────────────────────────────────────

cmd_health() {
    local resp; resp=$(api_get "/health")
    if [[ "$resp" == "OK" ]]; then
        echo -e "${GREEN}OK${NC} — ${CONDUCTOR_ADDR}"
    else
        echo -e "${RED}UNREACHABLE${NC} — ${CONDUCTOR_ADDR}"; return 1
    fi
}

# ── Build ──────────────────────────────────────────────────────────────

cmd_build() {
    echo -e "${BOLD}Building images...${NC}\n"

    echo "  [1/3] kv-conductor"
    (cd "$KV_DIR" && cargo build --release) || {
        echo -e "${RED}  cargo build failed${NC}"; return 1; }
    (cd "$KV_DIR" && docker build -q -t kv-conductor:latest .) || {
        echo -e "${RED}  docker build kv-conductor failed${NC}"; return 1; }
    echo -e "  ${GREEN}kv-conductor:latest${NC}"

    if docker image inspect zmq-publisher-base:latest &>/dev/null; then
        echo "  [2/3] zmq-publisher-base (cached)"
    else
        echo "  [2/3] zmq-publisher-base (python:3.11-slim + deps, first-time build)"
        (cd "$KV_DIR" && docker build -q -t zmq-publisher-base:latest -f mock/Dockerfile.base .) || {
            echo -e "${RED}  docker build zmq-publisher-base failed${NC}"; return 1; }
    fi
    echo -e "  ${GREEN}zmq-publisher-base:latest${NC}"

    echo "  [3/3] zmq-publisher"
    (cd "$KV_DIR" && docker build -q -t zmq-publisher:latest -f mock/Dockerfile.e2e .) || {
        echo -e "${RED}  docker build zmq-publisher failed${NC}"; return 1; }
    echo -e "  ${GREEN}zmq-publisher:latest${NC}"

    echo -e "\n${GREEN}All images built.${NC}"
}

# ── Deploy / Teardown ──────────────────────────────────────────────────

cmd_up() {
    local single_port="" vllm_fmt="1" swa_mix="1"
    local bs="$BLOCK_SIZE" init_blocks="8192"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --single-port)     single_port="1"; shift ;;
            --mooncake-format) vllm_fmt=""; shift ;;
            --no-swa-mixed)    swa_mix=""; shift ;;
            --block-size)      bs="$2"; shift 2 ;;
            --initial-blocks)  init_blocks="$2"; shift 2 ;;
            *) shift ;;
        esac
    done

    local mode="multi-medium"
    [[ -n "$single_port" ]] && mode="single-port"

    echo -e "${BOLD}Deploying: ${NUM_PUBLISHERS} publishers, mode=${mode}, block_size=${bs}${NC}"
    echo -e "  two-phase offload: XPU emits cpu-blocks → cached → CPU port confirms → tree insert"
    [[ -z "$vllm_fmt" ]] && echo -e "  format: Mooncake (legacy)"
    [[ -z "$swa_mix" ]] && echo -e "  SWA:    disabled"

    kubectl create ns "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null

    # ConfigMap with scripts (token_pool.py + zmq_publisher.py)
    kubectl -n "$NAMESPACE" create configmap zmq-publisher-script \
        --from-file=zmq_publisher.py="$SCRIPT_DIR/zmq_publisher.py" \
        --from-file=token_pool.py="$SCRIPT_DIR/token_pool.py" \
        --dry-run=client -o yaml 2>/dev/null | kubectl apply -f - 2>/dev/null

    # Apply base YAML (configmap + kv-conductor)
    kubectl apply -f "$YAML_FILE"

    for ((dp=0; dp<NUM_PUBLISHERS; dp++)); do
        local xpu_port=$((15557 + dp * 2)) cpu_port=$((15557 + dp * 2 + 1))
        local iid="mock-publisher-${dp}" svc="zmq-publisher-${dp}"
        local port=$((5557 + dp))

        if [[ -n "$single_port" ]]; then
            kubectl apply -f - <<PUB
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${svc}
  labels:
    app: ${svc}
  namespace: ${NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${svc}
  template:
    metadata:
      labels:
        app: ${svc}
    spec:
      terminationGracePeriodSeconds: 5
      containers:
      - name: publisher
        image: zmq-publisher:latest
        imagePullPolicy: IfNotPresent
        command:
        - sh
        - -c
        - |
          exec python3 /scripts/zmq_publisher.py \\
            --single-port --port ${port} \\
            --model "\$MODEL_NAME" --dp-rank ${dp} \\
            --block-size ${bs} --initial-blocks ${init_blocks} \\
            --interval "\$INTERVAL" --tenant-id "\$TENANT_ID" \\
            --instance-id "${iid}" \\
            \${MOONCAKE:+--mooncake-format} \\
            \${NO_SWA:+--no-swa-mixed}
        env:
        - name: MODEL_NAME
          valueFrom:
            configMapKeyRef:
              name: mock-zmq-config
              key: model
        - name: INTERVAL
          valueFrom:
            configMapKeyRef:
              name: mock-zmq-config
              key: interval
        - name: TENANT_ID
          valueFrom:
            configMapKeyRef:
              name: mock-zmq-config
              key: tenant_id
        - name: MOONCAKE
          value: "$([ -z "$vllm_fmt" ] && echo 1 || echo '')"
        - name: NO_SWA
          value: "$([ -z "$swa_mix" ] && echo 1 || echo '')"
        - name: POD_IP
          valueFrom:
            fieldRef:
              fieldPath: status.podIP
        stdin: true
        tty: true
        ports:
        - containerPort: ${port}
          protocol: TCP
        resources:
          requests: {memory: "128Mi", cpu: "100m"}
          limits:   {memory: "256Mi", cpu: "500m"}
        volumeMounts:
        - name: script
          mountPath: /scripts
      volumes:
      - name: script
        configMap:
          name: zmq-publisher-script
          defaultMode: 0555
---
apiVersion: v1
kind: Service
metadata:
  name: ${svc}
  namespace: ${NAMESPACE}
spec:
  ports:
  - port: ${port}
    protocol: TCP
    targetPort: ${port}
  selector:
    app: ${svc}
  type: ClusterIP
PUB
        else
            kubectl apply -f - <<PUB
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${svc}
  labels:
    app: ${svc}
  namespace: ${NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${svc}
  template:
    metadata:
      labels:
        app: ${svc}
    spec:
      terminationGracePeriodSeconds: 5
      automountServiceAccountToken: false
      initContainers:
      - name: wait-for-conductor
        image: busybox:1.36
        command:
        - sh
        - -c
        - |
          until wget -q -O- http://kv-conductor:13333/health 2>/dev/null; do
            sleep 2
          done
          echo "kv-conductor ready"
      containers:
      - name: publisher
        image: zmq-publisher:latest
        imagePullPolicy: IfNotPresent
        command:
        - sh
        - -c
        - |
          exec python3 /scripts/zmq_publisher.py \\
            --xpu-port ${xpu_port} --cpu-disk-port ${cpu_port} \\
            --model "${MODEL_NAME}" --dp-rank ${dp} \\
            --block-size ${bs} --initial-blocks ${init_blocks} \\
            --interval 2.0 --instance-id "${iid}" \\
            --store-backend YuanRong --conductor-url kv-conductor:13333 \\
            \${MOONCAKE:+--mooncake-format} \\
            \${NO_SWA:+--no-swa-mixed}
        env:
        - name: MOONCAKE
          value: "$([ -z "$vllm_fmt" ] && echo 1 || echo '')"
        - name: NO_SWA
          value: "$([ -z "$swa_mix" ] && echo 1 || echo '')"
        - name: POD_IP
          valueFrom:
            fieldRef:
              fieldPath: status.podIP
        ports:
        - containerPort: ${xpu_port}
          protocol: TCP
          name: xpu
        - containerPort: ${cpu_port}
          protocol: TCP
          name: cpu-disk
        resources:
          requests: {memory: "64Mi", cpu: "50m"}
          limits:   {memory: "128Mi", cpu: "200m"}
        volumeMounts:
        - name: script
          mountPath: /scripts
      volumes:
      - name: script
        configMap:
          name: zmq-publisher-script
          defaultMode: 0555
---
apiVersion: v1
kind: Service
metadata:
  name: ${svc}
  namespace: ${NAMESPACE}
spec:
  ports:
  - port: ${xpu_port}
    protocol: TCP
    targetPort: ${xpu_port}
    name: xpu
  - port: ${cpu_port}
    protocol: TCP
    targetPort: ${cpu_port}
    name: cpu-disk
  selector:
    app: ${svc}
  type: ClusterIP
PUB
        fi
    done

    echo "Waiting for pods (up to 120s)..."
    kubectl -n "$NAMESPACE" wait --for=condition=ready pod --all --timeout=120s 2>/dev/null || true
    echo ""
    kubectl -n "$NAMESPACE" get pods,svc
    echo ""
    echo -e "${GREEN}Deployment ready (${NUM_PUBLISHERS} publishers, ${mode}).${NC}"
}

cmd_down() {
    echo -e "${BOLD}Tearing down...${NC}"
    kubectl delete -f "$YAML_FILE" 2>/dev/null || true
    for ((dp=0; dp<${NUM_PUBLISHERS:-8}; dp++)); do
        kubectl -n "$NAMESPACE" delete deploy,svc "zmq-publisher-${dp}" 2>/dev/null || true
    done
    echo -e "${GREEN}Done.${NC}"
}

# ── Logs ───────────────────────────────────────────────────────────────

cmd_logs() {
    exec "$SCRIPT_DIR/collect_logs.sh" "$@"
}

cmd_logs_profile() {
    local out="${1:-/tmp/conductor_profile.log}"
    echo "Writing profiling logs to ${out} ..."
    kubectl -n "$NAMESPACE" logs deploy/mindie-motor-kv-conductor --since=10m 2>&1 \
        | grep -E "hash_computed|find_matches|query profile|latency" \
        | tee "$out"
    echo ""
    echo "=== Summary ==="
    echo "hash_computed: $(grep -c hash_computed "$out") lines"
    echo "find_matches:  $(grep -c find_matches "$out") lines"
    echo "Saved to: $out"
}

# ── Quick E2E ──────────────────────────────────────────────────────────

cmd_quick() {
    echo -e "${BOLD}=== Quick E2E Test ===${NC}\n"
    cmd_health || return 1; echo ""
    cmd_register
    echo "Waiting for first events to arrive..."
    sleep 3
    cmd_status
}

# ── dispatch ───────────────────────────────────────────────────────────

[[ $# -eq 0 ]] && { usage; exit 0; }

CMD="$1"; shift
case "$CMD" in
    build)        cmd_build ;;
    up|deploy)    cmd_up "$@" ;;
    down|delete)  cmd_down ;;
    logs)         cmd_logs "$@" ;;
    logs-profile) cmd_logs_profile "$@" ;;
    register)     cmd_register ;;
    unregister)   cmd_unregister ;;
    status|-s)    cmd_status ;;
    query-tokens) cmd_query_tokens "$@" ;;
    bench)        cmd_bench "$@" ;;
    smoke)        cmd_smoke ;;
    quick|-q)     cmd_quick ;;
    health)       cmd_health ;;
    help|--help)  usage ;;
    *)            echo -e "${RED}Unknown: $CMD${NC}\n"; usage; exit 1 ;;
esac
