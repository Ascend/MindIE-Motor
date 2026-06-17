#!/bin/bash
# KV Conductor E2E Log Collector
#
# Usage:
#   ./collect_logs.sh                    # Dump all logs (last 50 lines each)
#   ./collect_logs.sh -f                 # Follow all logs (tail -f)
#   ./collect_logs.sh -n 100             # Last 100 lines each
#   ./collect_logs.sh --grep "ZMQ\|error" # Filter by keyword
#   ./collect_logs.sh conductor          # Only kv-conductor logs
#   ./collect_logs.sh publisher          # Only publisher logs
#   ./collect_logs.sh events             # Only show event-related lines
#   ./collect_logs.sh blocks             # Show current block counts snapshot

set -euo pipefail

NAMESPACE="${KV_NAMESPACE:-mindie-motor}"
CONDUCTOR_LABEL="app=mindie-motor-kv-conductor"
PUBLISHER_LABELS=("app=zmq-publisher-0" "app=zmq-publisher-1")

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ── helpers ───────────────────────────────────────────────────────────

usage() {
    cat << 'EOF'
KV Conductor E2E Log Collector

Usage:  ./collect_logs.sh [options] [filter]

Options:
  -f, --follow        Follow logs (tail -f mode)
  -n N                Show last N lines (default: 50)
  -a, --all           Show all logs (no line limit)
  --grep PATTERN      Filter lines matching PATTERN (grep -E)
  -h, --help          Show this help

Filters (if omitted, shows all pods):
  conductor           Only kv-conductor pod
  publisher           All mock publisher pods
  publisher-0         Only zmq-publisher-0
  publisher-1         Only zmq-publisher-1
  events              Show event processing lines only
  blocks              Show current block counts (snapshot, not logs)
  errors              Show errors and warnings only
  zmq                 Show ZMQ connection related logs

Examples:
  ./collect_logs.sh                          # All logs, last 50 lines
  ./collect_logs.sh -f                       # Follow all logs
  ./collect_logs.sh conductor --grep ZMQ     # Conductor ZMQ-related logs
  ./collect_logs.sh events -n 20             # Last 20 event lines
  ./collect_logs.sh blocks                   # Current block snapshot
EOF
    exit 0
}

color_label() {
    local label="$1"
    case "$label" in
        *conductor*) echo -e "${GREEN}[${label}]${NC}" ;;
        *publisher-0*) echo -e "${CYAN}[${label}]${NC}" ;;
        *publisher-1*) echo -e "${YELLOW}[${label}]${NC}" ;;
        *) echo -e "${BOLD}[${label}]${NC}" ;;
    esac
}

pod_label() {
    local pod="$1"
    # Extract a compact label: zmq-publisher-0-xxxxx → pub-0
    if [[ "$pod" == *"kv-conductor"* ]]; then
        echo "conductor"
    elif [[ "$pod" == *"publisher-0"* ]]; then
        echo "pub-0"
    elif [[ "$pod" == *"publisher-1"* ]]; then
        echo "pub-1"
    else
        echo "${pod:0:20}"
    fi
}

get_pods() {
    local label="$1"
    kubectl -n "$NAMESPACE" get pods -l "$label" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null
}

# ── log functions ─────────────────────────────────────────────────────

dump_pod_logs() {
    local pod="$1"
    local label
    label=$(pod_label "$pod")
    local color_label
    color_label=$(color_label "$label")
    local lines_flag="--tail=$MAX_LINES"
    if [[ "$ALL_LINES" == "true" ]]; then
        lines_flag=""
    fi

    if [[ "$FOLLOW" == "true" ]]; then
        # Foreground follow — show recent lines first, then stream.
        # Ctrl+C to stop. Uses exec so the kubectl process replaces the
        # subshell, keeping signal handling clean.
        echo -e "\n${color_label} === $pod ($label) — following (Ctrl+C to stop) ==="
        exec kubectl -n "$NAMESPACE" logs "$pod" -f --tail=20
    else
        echo -e "\n${color_label} === $pod ($label) ==="
        if [[ -n "$GREP_PATTERN" ]]; then
            kubectl -n "$NAMESPACE" logs "$pod" $lines_flag 2>/dev/null \
                | grep -E "$GREP_PATTERN" --color=always \
                | while IFS= read -r line; do
                    echo -e "  $line"
                  done
        else
            kubectl -n "$NAMESPACE" logs "$pod" $lines_flag 2>/dev/null \
                | while IFS= read -r line; do
                    echo -e "  $line"
                  done
        fi
    fi
}

dump_by_label() {
    local label="$1"
    local pods
    pods=$(get_pods "$label")
    if [[ -z "$pods" ]]; then
        echo -e "${RED}No pods found for label: $label${NC}"
        return
    fi
    for pod in $pods; do
        dump_pod_logs "$pod"
    done
}

show_blocks_snapshot() {
    echo -e "${BOLD}=== KV Blocks Snapshot ===${NC}"
    local conductor_pod
    conductor_pod=$(get_pods "$CONDUCTOR_LABEL" | awk '{print $1}')
    if [[ -z "$conductor_pod" ]]; then
        echo -e "${RED}Conductor pod not found${NC}"
        return
    fi

    # Use port-forward to get worker stats
    kubectl -n "$NAMESPACE" port-forward "$conductor_pod" 13333:13333 &>/dev/null &
    local pf_pid=$!
    sleep 1

    if command -v python3 &>/dev/null; then
        curl -s http://localhost:13333/workers 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
print()
for e in d.get('indexer', []):
    print(f\"  model:     {e['model_name']}/{e['tenant_id']}\")
    print(f\"  blocks:    {e['total_blocks']}\")
    print(f\"  workers:   {e['worker_count']}\")
    print(f\"  block_sz:  {e['block_size']}\")
print()
print('  registered workers:')
for w in d.get('workers', []):
    for dp, info in w.get('endpoints', {}).items():
        print(f\"    {w['instance_id']}  dp={dp}  {info['engine_type']}  {info['endpoint']}\")
" 2>/dev/null || curl -s http://localhost:13333/workers 2>/dev/null
    else
        curl -s http://localhost:13333/workers 2>/dev/null
    fi

    kill $pf_pid 2>/dev/null
    wait $pf_pid 2>/dev/null
    echo ""
}

# ── main ──────────────────────────────────────────────────────────────

FOLLOW="false"
MAX_LINES=50
ALL_LINES="false"
GREP_PATTERN=""
FILTER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -f|--follow)
            FOLLOW="true"
            shift
            ;;
        -n)
            MAX_LINES="$2"
            shift 2
            ;;
        -a|--all)
            ALL_LINES="true"
            shift
            ;;
        --grep)
            GREP_PATTERN="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        conductor)
            FILTER="conductor"
            shift
            ;;
        publisher)
            FILTER="publisher"
            shift
            ;;
        publisher-0)
            FILTER="publisher-0"
            shift
            ;;
        publisher-1)
            FILTER="publisher-1"
            shift
            ;;
        events)
            FILTER="events"
            GREP_PATTERN="${GREP_PATTERN:+${GREP_PATTERN}|}apply_event|normalized|stored block|apply_store"
            shift
            ;;
        errors)
            FILTER="errors"
            GREP_PATTERN="${GREP_PATTERN:+${GREP_PATTERN}|}ERROR|WARN|error|fail|panic"
            shift
            ;;
        zmq)
            FILTER="zmq"
            GREP_PATTERN="${GREP_PATTERN:+${GREP_PATTERN}|}ZMQ|zmq|subscriber"
            shift
            ;;
        blocks)
            show_blocks_snapshot
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            ;;
    esac
done

# ── dispatch ──────────────────────────────────────────────────────────

case "$FILTER" in
    conductor)
        dump_by_label "$CONDUCTOR_LABEL"
        ;;
    publisher)
        for lbl in "${PUBLISHER_LABELS[@]}"; do
            dump_by_label "$lbl"
        done
        ;;
    publisher-0)
        dump_by_label "${PUBLISHER_LABELS[0]}"
        ;;
    publisher-1)
        dump_by_label "${PUBLISHER_LABELS[1]}"
        ;;
    *)
        # All pods
        echo -e "${BOLD}=== KV Conductor E2E Logs ===${NC}"
        dump_by_label "$CONDUCTOR_LABEL"
        for lbl in "${PUBLISHER_LABELS[@]}"; do
            dump_by_label "$lbl"
        done
        ;;
esac

# Wait for background tail processes
if [[ "$FOLLOW" == "true" ]]; then
    echo -e "\n${YELLOW}Following logs... Ctrl+C to stop${NC}"
    wait
fi
