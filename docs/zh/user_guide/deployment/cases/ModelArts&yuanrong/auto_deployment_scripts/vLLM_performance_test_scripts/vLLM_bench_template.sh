#!/usr/bin/env bash
# 脚本执行指令：nohup bash vLLM_bench_template.sh  </dev/null > /mnt/obs/scripts/test/vllm_bench/output/runtime.log 2>&1 &
set -euo pipefail

# ==========================================
# 1. 配置测试环境与模型服务信息
# ==========================================
BENCH_HOST="${BENCH_HOST:-172.16.0.148}"
BENCH_PORT="${BENCH_PORT:-1026}"
METRICS_PORT="${METRICS_PORT:-4026}"
TOKENIZER_PATH="${TOKENIZER_PATH:-/mnt/cache/GLM-5.1-W8A8}"

OUTPUT_DIR="/mnt/obs/scripts/test/vllm_bench/output/vLLM_bench_test_$(date +%Y%m%d_%H%M)"
mkdir -p ${OUTPUT_DIR}

METRIC_DIR="${OUTPUT_DIR}/bench_metrics"
CSV_FILE="${OUTPUT_DIR}/bench_prefix_cache_summary.csv"

mkdir -p "${METRIC_DIR}"
METRICS_URL="http://${BENCH_HOST}:${METRICS_PORT}/metrics"

MODEL_NAME="${MODEL_NAME:-glm51}"

# ==========================================
# 2. 配置测试矩阵与固定控制参数
# ==========================================
# 并发请求数组合设置
CONCURRENCY_LIST=(8 16 32 48 64)
# 输入长度组合设置
INPUT_LENGTH_LIST=(32768 65536 131072 163840 190000)
# 输出长度组合设置
OUTPUT_LENGTH_LIST=(1024 2048 4096 8192)
# 输出长度组合
TOTAL_ROUNDS=$(( ${#CONCURRENCY_LIST[@]} * ${#INPUT_LENGTH_LIST[@]} * ${#OUTPUT_LENGTH_LIST[@]} ))
CURRENT_ROUND=0

# 重复前缀比例设置（整数，范围 1-99，默认 90 表示 90%）
PREFIX_RATIO="${PREFIX_RATIO:-90}"
# 动态计算后缀比例
SUFFIX_RATIO=$(( 100 - PREFIX_RATIO ))
# 每轮测试数据集大小设置
NUM_PROMOTS="${NUM_PROMOTS:-200}"
# 数据集重复前缀种类数量设置
PREFIX_REPETITION_NUM="${PREFIX_REPETITION_NUM:-10}"

echo "=========================================================="
echo " 开始 vLLM 90% 前缀复用矩阵压测 | 总计划测试轮数: ${TOTAL_ROUNDS}"
echo " 并发维度: [${CONCURRENCY_LIST[*]}]"
echo " 输入长度: [${INPUT_LENGTH_LIST[*]}]"
echo " 输出长度: [${OUTPUT_LENGTH_LIST[*]}]"
echo "=========================================================="

# ==========================================
# 3. 双重循环执行测试
# ==========================================
for CONC in "${CONCURRENCY_LIST[@]}"; do
    for TOTAL_LEN in "${INPUT_LENGTH_LIST[@]}"; do
        for OUT_LEN in "${OUTPUT_LENGTH_LIST[@]}"; do
            CURRENT_ROUND=$((CURRENT_ROUND + 1))

            # 根据PREFIX_RATIO动态计算前缀长度与后缀长度
            PREFIX_LEN=$(( TOTAL_LEN * PREFIX_RATIO / 100 ))
            SUFFIX_LEN=$(( TOTAL_LEN - PREFIX_LEN ))

            # 为每一轮生成唯一的 SEED 隔离标识
            SEED=$(date +%s)_${CONC}_${TOTAL_LEN}_${OUT_LEN}

            BEFORE_FILE="${METRIC_DIR}/metrics_before_round_${CURRENT_ROUND}.txt"
            AFTER_FILE="${METRIC_DIR}/metrics_after_round_${CURRENT_ROUND}.txt"

            echo ""
            echo ">> [第 ${CURRENT_ROUND}/${TOTAL_ROUNDS} 轮] 并发=${CONC} | 输入长度=${TOTAL_LEN} | 输出长度=${OUT_LEN}"
            echo "   -> [${PREFIX_RATIO}%] 公共前缀: ${PREFIX_LEN} | [${SUFFIX_RATIO}%] 动态后缀: ${SUFFIX_LEN}"
            echo ""

            # 采集压测前指标
            if ! curl -s --max-time 5 "${METRICS_URL}" > "${BEFORE_FILE}"; then
                echo "❌ 错误: 无法连接到 vLLM 监控, 跳过本轮测试..."
                continue
            fi

            TIMESTAMP=$(date +%H%M%S)

            # 执行当前维度的压测
            vllm bench serve \
              --backend openai-chat \
              --endpoint /v1/chat/completions \
              --dataset-name prefix_repetition \
              --prefix-repetition-prefix-len "${PREFIX_LEN}" \
              --prefix-repetition-suffix-len "${SUFFIX_LEN}" \
              --prefix-repetition-output-len "${OUT_LEN}" \
              --num-prompts "${NUM_PROMOTS}" \
              --prefix-repetition-num-prefixes "${PREFIX_REPETITION_NUM}" \
              --ignore-eos \
              --model "${MODEL_NAME}" \
              --tokenizer "${TOKENIZER_PATH}" \
              --seed "$(date +%s)" \
              --host "${BENCH_HOST}" \
              --port "${BENCH_PORT}" \
              --max-concurrency "${CONC}" \
              > ${OUTPUT_DIR}/bench_${TIMESTAMP}_${SEED}.log 2>&1

            echo "⏳ 等待 5 秒确保 Prometheus 异步计数器落盘..."
            sleep 5

            # 采集压测后指标
            curl -s --max-time 5 "${METRICS_URL}" > "${AFTER_FILE}"

# ==========================================
# 4. 解析指标并追加到统一的 CSV 报表
# ==========================================
python3 - <<EOF
import re
import csv
from pathlib import Path

before_file = Path("${BEFORE_FILE}")
after_file = Path("${AFTER_FILE}")
csv_file = Path("${CSV_FILE}")

def collect_metric(path):
    values = {
        "local_queries": 0.0, "local_hits": 0.0,
        "ext_queries": 0.0, "ext_hits": 0.0
    }
    if not path.exists(): return values

    text = path.read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue

        # 过滤掉特殊的 _created 时间戳指标，只看数据本身
        if "_created" in line:
            continue

        # 提取行尾的浮点数/整数
        match_val = re.search(r'([0-9.]+(?:[eE][+-]?[0-9]+)?)\s*$', line)
        if not match_val: continue
        val = float(match_val.group(1))

        # 兼容冒号和下划线，精准匹配 vLLM 核心的 _total 指标
        if re.search(r'vllm[:_]prefix_cache_queries(_total)?', line):
            values["local_queries"] += val
        elif re.search(r'vllm[:_]prefix_cache_hits(_total)?', line):
            values["local_hits"] += val
        elif re.search(r'vllm[:_]external_prefix_cache_queries(_total)?', line):
            values["ext_queries"] += val
        elif re.search(r'vllm[:_]external_prefix_cache_hits(_total)?', line):
            values["ext_hits"] += val

    return values

before = collect_metric(before_file)
after = collect_metric(after_file)

# 计算压测期间的纯增量 (Delta)
prefix_queries = max(0.0, after["local_queries"] - before["local_queries"])
prefix_hits = max(0.0, after["local_hits"] - before["local_hits"])
external_queries = max(0.0, after["ext_queries"] - before["ext_queries"])
external_hits = max(0.0, after["ext_hits"] - before["ext_hits"])

prefix_hit_rate = round(prefix_hits / prefix_queries, 4) if prefix_queries > 0 else "N/A"
external_hit_rate = round(external_hits / external_queries, 4) if external_queries > 0 else "N/A"

row = {
    "test_round": "${CURRENT_ROUND}",
    "concurrency": "${CONC}",
    "max_model_length": "${TOTAL_LEN}",
    "prefix_len": "${PREFIX_LEN}",
    "suffix_len": "${SUFFIX_LEN}",
    "output_len": "${OUT_LEN}",
    "prefix_queries_delta": int(prefix_queries),
    "prefix_hits_delta": int(prefix_hits),
    "prefix_hit_rate": prefix_hit_rate,
    "external_prefix_queries_delta": int(external_queries),
    "external_prefix_hits_delta": int(external_hits),
    "external_prefix_hit_rate": external_hit_rate,
}

write_header = not csv_file.exists()
with csv_file.open("a", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=row.keys())
    if write_header: writer.writeheader()
    writer.writerow(row)

print(f"📊 [Round ${CURRENT_ROUND}] 本地命中率: {row['prefix_hit_rate']} | 外部命中率: {row['external_prefix_hit_rate']}")
print(f"   (DEBUG 绝对值 -> 本地Q/H: {int(after['local_queries'])}/{int(after['local_hits'])} | 外部Q/H: {int(after['ext_queries'])}/{int(after['ext_hits'])})")
EOF

            # 针对长文本的防御性休眠策略
            if [ $CURRENT_ROUND -lt $TOTAL_ROUNDS ]; then
                echo "💤 显存保护: 深度休眠 10 秒, 避免超长文本显存残留导致 OOM..."
                sleep 10
            fi

        done
    done
done

echo ""
echo "=========================================================="
echo "  ✅ ${PREFIX_RATIO}% 前缀复用比例矩阵测试全部完成!"
echo "  📁 结果汇总报表已保存至: ${CSV_FILE}"
echo "=========================================================="
