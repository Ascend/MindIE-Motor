#!/bin/bash
# KV Conductor CLI — register publishers, query cache hits, inspect state.
#
# Usage:
#   ./conductor_cli.sh register                  # Register all mock publishers
#   ./conductor_cli.sh unregister                # Unregister all
#   ./conductor_cli.sh status                    # Workers + block counts
#   ./conductor_cli.sh query <hash1> <hash2>...  # Query by block hashes
#   ./conductor_cli.sh query --blocks 3          # Auto-detect hashes from publisher
#   ./conductor_cli.sh query-tokens --count 256   # Query by token IDs
#   ./conductor_cli.sh quick                     # One-shot: register → wait → status
#   ./conductor_cli.sh health                    # Health check
#
# Environment:
#   KV_NAMESPACE      K8s namespace (default: mindie-motor)
#   CONDUCTOR_ADDR    Conductor address (default: localhost:13333)

set -euo pipefail

# Pre-flight dependency check
_check_deps() {
    local missing=()
    command -v curl    &>/dev/null || missing+=("curl")
    command -v python3 &>/dev/null || missing+=("python3")
    command -v kubectl &>/dev/null || missing+=("kubectl")
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
BLOCK_SIZE=128
TENANT_ID="default"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

usage() {
    cat << 'EOF'
KV Conductor CLI — build, deploy, register, query, all-in-one.

Usage:  ./conductor_cli.sh <command> [options]

Setup:
  build                 Build all Docker images (kv-conductor + zmq-publisher)
  up                    Deploy N single-port publishers + kv-conductor
  up-multi              Deploy multi-port publisher (XPU:15557 + CPU/DISK:15558) + kv-conductor
  down                  Tear down all resources
  logs [filter]         Collect logs (see collect_logs.sh for filters)

Runtime:
  register [name]       Register single-port publishers with conductor
  register-multi        Register multi-port publisher (medium_endpoints protocol)
  unregister [name]     Unregister mock publishers
  status                Show workers and block counts
  query [hashes...]     Query cache hits by block hash
  query-tokens [tokens] Query cache hits by raw token IDs
  quick                 One-shot: register -> wait -> status (single-port)
  quick-multi           One-shot: health -> register-multi -> status (multi-port)
  health                Health check

Options for 'query':
  --model NAME          Model name (default: opt-125m)
  --blocks N            Auto-detect N hashes from publisher logs
  --tenant ID           Tenant ID (default: default)

Options for 'query-tokens':
  --model NAME          Model name (default: opt-125m)
  --count N             Number of sequential tokens (default: 128)
  --tenant ID           Tenant ID (default: default)

Environment:
  KV_NAMESPACE          K8s namespace (default: mindie-motor)
  CONDUCTOR_ADDR        Conductor address (default: localhost:13333)

Quick start (single-port):
  ./conductor_cli.sh build && ./conductor_cli.sh up
  kubectl -n mindie-motor port-forward deploy/mindie-motor-kv-conductor 13333:13333 &
  ./conductor_cli.sh quick
  ./conductor_cli.sh query --blocks 3

Quick start (multi-port):
  ./conductor_cli.sh build && ./conductor_cli.sh up-multi
  kubectl -n mindie-motor port-forward deploy/mindie-motor-kv-conductor 13333:13333 &
  ./conductor_cli.sh quick-multi
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

split_response() {
    # $1 = "body\nhttp_code"; prints body to stdout, sets RESP_CODE
    RESP_CODE=$(echo "$1" | tail -1)
    echo "$1" | sed '$d'
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
        local iid="mock-publisher-${dp}" svc="zmq-publisher-${dp}" port=$((5557 + dp))
        local ip; ip=$(pod_ip "$svc")
        [[ -z "$ip" ]] && { echo -e "  ${RED}dp=${dp}: pod not found${NC}"; continue; }

        printf "  dp=%-2d tcp://%s:%s ... " "$dp" "$ip" "$port"

        local medium_endpoints
        medium_endpoints=$(python3 -c "
import json
print(json.dumps({'xpu': 'tcp://${ip}:${port}'}))
")
        local resp; resp=$(api_post "/register" "{
            \"instance_id\": \"${iid}\",
            \"medium_endpoints\": ${medium_endpoints},
            \"type\": \"Mooncake\",
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
            # New protocol: medium_endpoints map
            meps = info.get('medium_endpoints', {})
            if meps:
                # Collect unique endpoint -> media mapping
                ep_media = {}
                for medium, ep in sorted(meps.items()):
                    ep_media.setdefault(ep, []).append(medium.upper())
                ep_strs = [f'{ep} ({",".join(media)})' for ep, media in ep_media.items()]
                print(f'    {w[\"instance_id\"]:30s}  dp={dp}  {info[\"engine_type\"]:8s}  {\" | \".join(ep_strs)}')
            else:
                # Legacy: single endpoint
                ep = info.get('endpoint', 'N/A')
                print(f'    {w[\"instance_id\"]:30s}  dp={dp}  {info[\"engine_type\"]:8s}  {ep}')
else:
    print('  (no workers)')
" 2>/dev/null || echo "$json"
}

# ── Query ──────────────────────────────────────────────────────────────

cmd_query() {
    local model="$MODEL_NAME" tenant="$TENANT_ID" bs="$BLOCK_SIZE"
    local hashes=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --model)  model="$2"; shift 2 ;;
            --tenant) tenant="$2"; shift 2 ;;
            *)        hashes+=("$1"); shift ;;
        esac
    done

    if [[ ${#hashes[@]} -eq 0 ]]; then
        echo "Usage: conductor_cli.sh query <hash1> <hash2> ..."
        echo "       conductor_cli.sh query-tokens --count 512  (recommended)"
        return 1
    fi

    local hlist; hlist=$(IFS=','; echo "${hashes[*]}")
    echo -e "${BOLD}Query: ${#hashes[@]} hashes, model=${model}, block_size=${bs}${NC}\n"

    local resp body code
    resp=$(api_post "/query_by_hash" "{
        \"model\": \"${model}\", \"block_size\": ${bs},
        \"block_hashes\": [${hlist}], \"tenant_id\": \"${tenant}\"
    }")
    code=$(echo "$resp" | tail -1); body=$(echo "$resp" | sed '$d')
    [[ "$code" != "200" ]] && { echo -e "${RED}Query failed (${code})${NC}"; return 1; }
    format_hits "$body" "$bs"
}

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
    print(f'  {\"instance\":<30s} {\"tier\":>6s} {\"blocks\":>8s} {\"tokens\":>8s}')
    print(f'  {\"-\"*30} {\"-\"*6} {\"-\"*8} {\"-\"*8}')
    for inst_id, imd in sorted(instances.items()):
        for tier in ['XPU', 'CPU', 'DISK']:
            t = imd.get(tier, 0)
            if t > 0:
                print(f'  {inst_id:<30s} {tier:>6s} {t//bs:>8d} {t:>8d}')
        for rank, t in sorted(imd.get('DP', {}).items()):
            print(f'  {\"dp=\"+rank:<30s} {\"DP\":>6s} {t//bs:>8d} {t:>8d}')
        best = imd.get('longest_matched', 0)
        print(f'  {\"\":30s} {\"best\":>6s} {best//bs:>8d} {best:>8d}')
    print()
" 2>/dev/null || echo "$json"
}

# --- Bench -----------------------------------------------------------

# Common token IDs mimicking real LLM tokenizer output.
# Mix of frequent (100-3000), mid (3000-25000), rare (25000-50000).
# These are used as "seeds" — each bench query picks a random window of
# block_size tokens from this pool.
COMMON_TOKENS=(
    101 2023 318 559 234 1039 640 1024 286 562 317 859 1053 1288 2000
    345 678 901 1234 1567 1890 2123 2456 2789 3012 3345 3678 4001
    4334 4667 4999 5321 5654 5987 6319 6652 6985 7318 7650 7983
    8316 8649 8981 9314 9647 9980 10313 10646 10979 11312 11645 11978
    12311 12644 12977 13310 13643 13976 14309 14642 14975 15308 15641
    15974 16307 16640 16973 17306 17639 17972 18305 18638 18971 19304
    19637 19970 20303 20636 20969 21302 21635 21968 22301 22634 22967
    23300 23633 23966 24299 24632 24965 25298 25631 25964 26297 26630
    26963 27296 27629 27962 28295 28628 28961 29294 29627 29960 30293
    30626 30959 31292 31625 31958 32291 32624 32957 33290 33623 33956
    34289 34622 34955 35288 35621 35954 36287 36620 36953 37286 37619
    37952 38285 38618 38951 39284 39617 39950 40283 40616 40949 41282
    41615 41948 42281 42614 42947 43280 43613 43946 44279 44612 44945
    45278 45611 45944 46277 46610 46943 47276 47609 47942 48275 98306
)

cmd_bench() {
    local model="$MODEL_NAME" tenant="$TENANT_ID" bs="$BLOCK_SIZE"
    local count=20 tokens_per=512 throughput=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --model)  model="$2"; shift 2 ;;
            --tenant) tenant="$2"; shift 2 ;;
            --count)  count="$2"; shift 2 ;;
            --tokens) tokens_per="$2"; shift 2 ;;
            --throughput) throughput="1"; shift ;;
            *) shift ;;
        esac
    done

    local mode="realistic"
    local desc="common LLM token IDs"
    if [[ -n "$throughput" ]]; then
        mode="throughput"
        desc="by hash (100% hits, measures latency/QPS)"
    fi

    local token_pool
    token_pool=$(IFS=','; echo "${COMMON_TOKENS[*]}")

    # Pre-extract hashes for throughput mode
    local cached_hashes=""
    if [[ -n "$throughput" ]]; then
        # Try multi-port publisher first, then single-port
        for deploy_name in zmq-publisher-multi-0 zmq-publisher-0; do
            cached_hashes=$(kubectl -n "$NAMESPACE" logs "deploy/${deploy_name}" --tail=100 2>/dev/null \
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

    echo -e "${BOLD}Benchmark: ${count} queries, bs=${bs}, mode=${mode}${NC}"
    echo "  ${desc}"
    echo ""

    python3 -c "
import json, random, time, subprocess, re

count = $count
block_size = $bs
model = '$model'
tenant = '$tenant'
mode = '$mode'
token_pool = [$token_pool]
cached = '$cached_hashes'

# Parse cached hashes
all_hashes = [int(h) for h in cached.split() if h.strip()] if cached else []
num_hashes = len(all_hashes)

hits = 0; misses = 0; total_blocks = 0; max_blocks = 0; best_worker = None
latencies = []; total_tok_matched = 0
block_hits_by_worker = {}

import urllib.request

for i in range(count):
    if mode == 'throughput' and num_hashes > 0:
        # Use query_by_hash with random hash windows
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
        # Realistic: POST /query with common tokens, simulating a
        # real prompt that might overlap with any DP's cached prefix.
        # Try dp_rank-based offsets (same logic as publisher) to get
        # non-zero hit rates when blocks were stored from the same pool.
        dp = random.randint(0, 7)
        pool_size = len(token_pool)
        offset = (dp * 1000) % pool_size
        start = (offset + random.randint(0, max(0, pool_size - $tokens_per))) % pool_size
        tokens = []
        for j in range($tokens_per):
            tokens.append(token_pool[(start + j) % pool_size])
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

        matched = 0; worker = None
        for tid, instances in result.items():
            for inst_id, imd in instances.items():
                b = imd.get('longest_matched', 0) // block_size if block_size else 0
                if b > matched:
                    matched = b; worker = inst_id
                for rank, tok in imd.get('DP', {}).items():
                    bk = tok // block_size if block_size else 0
                    key = f'{inst_id}/dp={rank}'
                    block_hits_by_worker[key] = block_hits_by_worker.get(key, 0) + bk
        if matched > 0:
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
    if block_hits_by_worker:
        print('    per-worker:')
        for wk, bk in sorted(block_hits_by_worker.items()):
            print(f'      {wk:35s}  {bk:>6d} blocks')
if latencies:
    latencies.sort()
    print('    latency (ms):')
    print(f'      p50={latencies[len(latencies)//2]:.1f}  '
          f'p90={latencies[min(len(latencies)-1, len(latencies)*90//100)]:.1f}  '
          f'p99={latencies[min(len(latencies)-1, len(latencies)*99//100)]:.1f}  '
          f'max={latencies[-1]:.1f}')
" 2>/dev/null
}

# --- Smoke test ----------------------------------------------------

cmd_smoke() {
    echo -e "${BOLD}=== API Smoke Test ===${NC}\n"
    local pass=0 fail=0 total=7
    local iid="smoke-test-$(date +%s)"

    check() {
        local desc="$1" ok="$2"
        if [[ "$ok" == "1" ]]; then
            echo -e "  ${GREEN}[PASS]${NC} $desc"
            pass=$((pass + 1))
        else
            echo -e "  ${RED}[FAIL]${NC} $desc"
            fail=$((fail + 1))
        fi
    }

    # 1. Health
    echo "  1. GET /health"
    local resp; resp=$(api_get "/health")
    check "/health" "$([[ "$resp" == "OK" ]] && echo 1 || echo 0)"

    # 2. Register
    echo "  2. POST /register"
    local reg_resp reg_code
    reg_resp=$(api_post "/register" "{
        \"instance_id\": \"${iid}\", \"endpoint\": \"tcp://10.0.0.1:5557\",
        \"type\": \"vllm\", \"modelname\": \"smoke-model\",
        \"block_size\": 4, \"dp_rank\": 0, \"tenant_id\": \"default\"
    }")
    reg_code=$(echo "$reg_resp" | tail -1)
    check "/register (201)" "$([[ "$reg_code" == "201" ]] && echo 1 || echo 0)"

    # 3. GET /workers
    echo "  3. GET /workers"
    local wk; wk=$(api_get "/workers")
    check "/workers" "$(echo "$wk" | python3 -c "import json,sys; d=json.load(sys.stdin); print(1 if d.get('workers') else 0)" 2>/dev/null || echo 0)"

    # 4. POST /events (HTTP injection)
    echo "  4. POST /events"
    local ev_resp ev_code
    ev_resp=$(api_post "/events" "{
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
    }")
    ev_code=$(echo "$ev_resp" | tail -1)
    local ev_body; ev_body=$(echo "$ev_resp" | sed '$d')
    local ev_ok
    ev_ok=$(echo "$ev_body" | python3 -c "import json,sys; d=json.load(sys.stdin); print(1 if d.get('events_applied',0)>0 else 0)" 2>/dev/null || echo 0)
    check "/events ($ev_code, applied=$ev_ok)" "$ev_ok"

    # 5. POST /query (by tokens)
    echo "  5. POST /query"
    local q_resp q_code q_body
    q_resp=$(api_post "/query" '{
        "model": "smoke-model", "block_size": 4,
        "token_ids": [1,2,3,4,5,6,7,8], "tenant_id": "default"
    }')
    q_code=$(echo "$q_resp" | tail -1)
    q_body=$(echo "$q_resp" | sed '$d')
    check "/query (200)" "$([[ "$q_code" == "200" ]] && echo 1 || echo 0)"

    # 6. POST /query_by_hash
    echo "  6. POST /query_by_hash"
    local qh_resp qh_code
    qh_resp=$(api_post "/query_by_hash" '{
        "model": "smoke-model", "block_size": 4,
        "block_hashes": [1], "tenant_id": "default"
    }')
    qh_code=$(echo "$qh_resp" | tail -1)
    check "/query_by_hash (200)" "$([[ "$qh_code" == "200" ]] && echo 1 || echo 0)"

    # 7. POST /unregister
    echo "  7. POST /unregister"
    local unreg_resp unreg_code
    unreg_resp=$(api_post "/unregister" "{
        \"instance_id\": \"${iid}\", \"type\": \"vllm\",
        \"modelname\": \"smoke-model\", \"block_size\": 4,
        \"dp_rank\": 0, \"tenant_id\": \"default\"
    }")
    unreg_code=$(echo "$unreg_resp" | tail -1)
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

# --- Build ----------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cmd_build() {
    echo -e "${BOLD}Building images...${NC}\n"

    echo "  [1/2] kv-conductor"
    (cd "$KV_DIR" && cargo build --release) || {
        echo -e "${RED}  cargo build failed${NC}"; return 1; }
    (cd "$KV_DIR" && docker build -q -t kv-conductor:latest .) || {
        echo -e "${RED}  docker build kv-conductor failed${NC}"; return 1; }
    echo -e "  ${GREEN}kv-conductor:latest${NC}"

    echo "  [2/3] zmq-publisher-base (python:3.11-slim + deps)"
    (cd "$KV_DIR" && docker build -q -t zmq-publisher-base:latest -f mock/Dockerfile.base .) || {
        echo -e "${RED}  docker build zmq-publisher-base failed${NC}"; return 1; }
    echo -e "  ${GREEN}zmq-publisher-base:latest${NC}"

    echo "  [3/3] zmq-publisher"
    (cd "$KV_DIR" && docker build -q -t zmq-publisher:latest -f mock/Dockerfile.e2e .) || {
        echo -e "${RED}  docker build zmq-publisher failed${NC}"; return 1; }
    echo -e "  ${GREEN}zmq-publisher:latest${NC}"

    echo -e "\n${GREEN}All images built.${NC}"
}

# --- Deploy / Teardown -----------------------------------------------

YAML_FILE="$SCRIPT_DIR/e2e_test.yaml"

cmd_up() {
    echo -e "${BOLD}Deploying kv-conductor + ${NUM_PUBLISHERS} publishers...${NC}\n"

    kubectl create ns "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null

    kubectl -n "$NAMESPACE" create configmap zmq-publisher-script \
        --from-file=zmq_publisher.py="$SCRIPT_DIR/zmq_publisher.py" \
        --dry-run=client -o yaml 2>/dev/null | kubectl apply -f - 2>/dev/null

    # Apply base YAML (configmap + kv-conductor)
    kubectl apply -f "$YAML_FILE"

    # Create N publisher Deployments + Services
    for ((dp=0; dp<NUM_PUBLISHERS; dp++)); do
        local port=$((5557 + dp)) iid="mock-publisher-${dp}" svc="zmq-publisher-${dp}"
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
        command: ["python3", "/scripts/zmq_publisher.py"]
        args:
        - "--model=\$(MODEL_NAME)"
        - "--dp-rank=\$(DP_RANK)"
        - "--initial-blocks=\$(NUM_BLOCKS)"
        - "--port=\$(PUB_PORT)"
        - "--block-size=\$(BLOCK_SIZE)"
        - "--interval=\$(INTERVAL)"
        - "--tenant-id=\$(TENANT_ID)"
        - "--instance-id=\$(INSTANCE_ID)"
        env:
        - name: INSTANCE_ID
          value: "${iid}"
        - name: MODEL_NAME
          valueFrom:
            configMapKeyRef:
              name: mock-zmq-config
              key: model
        - name: DP_RANK
          value: "${dp}"
        - name: NUM_BLOCKS
          valueFrom:
            configMapKeyRef:
              name: mock-zmq-config
              key: initial_blocks
        - name: PUB_PORT
          value: "${port}"
        - name: BLOCK_SIZE
          valueFrom:
            configMapKeyRef:
              name: mock-zmq-config
              key: block_size
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
    done

    echo "Waiting for pods..."
    kubectl -n "$NAMESPACE" wait --for=condition=ready pod --all --timeout=120s
    echo ""
    kubectl -n "$NAMESPACE" get pods,svc
}

cmd_down() {
    echo -e "${BOLD}Tearing down...${NC}"
    kubectl delete -f "$YAML_FILE" 2>/dev/null || true
    kubectl delete -f "$YAML_MULTI" 2>/dev/null || true
    for ((dp=0; dp<${NUM_PUBLISHERS:-8}; dp++)); do
        kubectl -n "$NAMESPACE" delete deploy,svc "zmq-publisher-${dp}" 2>/dev/null || true
        kubectl -n "$NAMESPACE" delete deploy,svc "zmq-publisher-multi-${dp}" 2>/dev/null || true
    done
    echo -e "${GREEN}Done.${NC}"
}

# --- Logs -----------------------------------------------------------

cmd_logs() {
    exec "$SCRIPT_DIR/collect_logs.sh" "$@"
}

# --- Quick E2E ------------------------------------------------------

cmd_quick() {
    echo -e "${BOLD}=== Quick E2E Test ===${NC}\n"
    cmd_health || return 1; echo ""
    cmd_register "all"
    echo "Waiting for first events to arrive..."
    sleep 3
    cmd_status
}

# ── Multi-Port ────────────────────────────────────────────────────────

YAML_MULTI="$SCRIPT_DIR/e2e_multi_port.yaml"

cmd_up_multi() {
    local np="${NUM_PUBLISHERS:-8}"
    echo -e "${BOLD}Deploying multi-port: kv-conductor + ${np} publisher(s)...${NC}\n"

    kubectl create ns "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null

    # Apply base YAML (kv-conductor + service only)
    kubectl apply -f "$YAML_MULTI"

    # Create N multi-port publisher Deployments
    for ((dp=0; dp<np; dp++)); do
        local xpu_port=$((15557 + dp * 2))
        local cpu_port=$((15557 + dp * 2 + 1))
        local iid="mock-publisher-multi-${dp}" svc="zmq-publisher-multi-${dp}"

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
          echo "Waiting for kv-conductor:13333..."
          until wget -q -O- http://kv-conductor:13333/health 2>/dev/null; do
            sleep 2
          done
          echo "kv-conductor ready"
      containers:
      - name: publisher
        image: zmq-publisher:latest
        imagePullPolicy: IfNotPresent
        env:
        - name: POD_IP
          valueFrom:
            fieldRef:
              fieldPath: status.podIP
        command:
        - python3
        - /usr/local/bin/zmq_publisher.py
        - --multi-port
        - --xpu-port
        - "${xpu_port}"
        - --cpu-disk-port
        - "${cpu_port}"
        - --model
        - "${MODEL_NAME}"
        - --dp-rank
        - "${dp}"
        - --block-size
        - "${BLOCK_SIZE}"
        - --initial-blocks
        - "8"
        - --interval
        - "2.0"
        - --instance-id
        - "${iid}"
        - --store-backend
        - YuanRong
        - --conductor-url
        - kv-conductor:13333
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
PUB
    done

    echo "Waiting for pods (up to 120s)..."
    kubectl -n "$NAMESPACE" wait --for=condition=ready pod --all --timeout=120s 2>/dev/null || true
    echo ""
    kubectl -n "$NAMESPACE" get pods,svc
    echo ""
    echo -e "${GREEN}Multi-port deployment ready (${np} publishers).${NC}"
    echo "  Each publisher: XPU + CPU/DISK ports, auto-registers with medium_endpoints"
}

cmd_register_multi() {
    local np="${NUM_PUBLISHERS:-8}"
    echo -e "${BOLD}Registering ${np} multi-port publisher(s) → ${CONDUCTOR_ADDR}${NC}\n"

    for ((dp=0; dp<np; dp++)); do
        local svc="zmq-publisher-multi-${dp}"
        local xpu_port=$((15557 + dp * 2))
        local cpu_port=$((15557 + dp * 2 + 1))
        local iid="mock-publisher-multi-${dp}"
        local ip; ip=$(pod_ip "$svc")
        [[ -z "$ip" ]] && { echo -e "  ${RED}dp=${dp}: pod not found${NC}"; continue; }

        local medium_endpoints
        medium_endpoints=$(python3 -c "
import json
print(json.dumps({
    'xpu':  'tcp://${ip}:${xpu_port}',
    'cpu':  'tcp://${ip}:${cpu_port}',
    'disk': 'tcp://${ip}:${cpu_port}',
}))
")

        printf "  dp=%-2d tcp://%s:%d (XPU) + tcp://%s:%d (CPU/DISK) ... " "$dp" "$ip" "$xpu_port" "$ip" "$cpu_port"

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
            409)     echo -e "${YELLOW}ALREADY REGISTERED${NC}" ;;
            *)       echo -e "${RED}FAIL${NC} ($code)" ;;
        esac
    done
    echo ""
}

cmd_quick_multi() {
    cmd_health
    echo ""
    cmd_register_multi
    sleep 3
    echo ""
    cmd_status
}

# ── dispatch ───────────────────────────────────────────────────────────

[[ $# -eq 0 ]] && { usage; exit 0; }

CMD="$1"; shift
case "$CMD" in
    build)        cmd_build ;;
    up|deploy)    cmd_up ;;
    up-multi)     cmd_up_multi ;;
    down|delete)  cmd_down ;;
    logs)         cmd_logs "$@" ;;
    register)     cmd_register ;;
    register-multi) cmd_register_multi ;;
    unregister)   cmd_unregister ;;
    status|-s)    cmd_status ;;
    query)        cmd_query "$@" ;;
    query-tokens) cmd_query_tokens "$@" ;;
    bench)        cmd_bench "$@" ;;
    smoke)        cmd_smoke ;;
    quick|-q)     cmd_quick ;;
    quick-multi)  cmd_quick_multi ;;
    health)       cmd_health ;;
    help|--help)  usage ;;
    *)            echo -e "${RED}Unknown: $CMD${NC}\n"; usage; exit 1 ;;
esac
