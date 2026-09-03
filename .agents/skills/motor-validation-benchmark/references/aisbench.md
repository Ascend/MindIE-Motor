# AISBench execution contract

AISBench CLI、参数名和产物 schema 会随版本变化。每次运行先读取已安装版本的
`ais_bench --help`，并以该 revision 的官方说明和真实产物为准；本 reference 不固定
可能漂移的完整命令行。

## 后端选择

除非所求结果依赖 wrapper 独有能力，否则使用原生 `ais_bench`。wrapper 为
[`rayn-zzz/aisbench_auto_tools_prefix`](https://github.com/rayn-zzz/aisbench_auto_tools_prefix)，
只覆盖 prefix-cache（公共前缀比例、prefix 池/seed、逐 DP warmup + HBM/external
命中率）。基础流式性能、定长 I/O、request count/concurrency/rate、稳定阶段走原生。

## 参数映射

构造命令前明确记录：endpoint、served model、dataset 或输入长度分布、输出长度、
request count、concurrency、request rate、stream、seed、warmup 和 output directory。
不支持的参数应 BLOCKED，不得静默丢弃。

| 用户意图 | 原生 | wrapper |
|---|---|---|
| 请求数 | synthetic `RequestCount` / `--num-prompts` | `--data_num` |
| 输入/输出长度 | run 级 synthetic；精确输出需 `ignore_eos=True` | `--input_len` / `--output_len` |
| 并发 / 到达速率 | model `batch_size` / `request_rate` | `--concurrency` / `--request_rate` |
| 稳定阶段 | `--summarizer stable_stage` | 原生优先 |
| prefix 比例/池/seed/DP warmup | 未集成 | `--repeat_rate` `--prefix_num` `--seed` `--dp` `--prefix_test` |

`request_rate` 是发送侧目标，不是实测 QPS；当前上游 `< 0.1` 视为不限速。

## 运行约束

- 先执行更小的 smoke workload；正式 workload 必须由用户确认。
- 正式运行不启用会把 client 限制为单核/串行的 debug 模式。
- 长任务使用可监控后台执行，并保存完整 stdout/stderr 和退出码。
- 每次运行使用新目录，验证产物 mtime 落在本次时间窗，避免读取旧结果。
- 不自动下载数据集、安装依赖或修改共享 AISBench 源码。

运行前记录 `python3 --version`、`pip show ais-bench-benchmark`、`ais_bench --help`
和源码 revision；确认 `--mode perf`、`--work-dir`、`--num-prompts`、`--num-warmups`、
summarizer 与输出 schema 受支持。不安装、不升级任何包。wrapper 额外记录
`aisbench_test.py --help` 与 git revision。

Context gate：`input + output <= max_model_len`；smoke 后核对 tokenizer/chat 模板
实际长度，超限或精确输出未达成则停止。服务 gate：Coordinator `/readiness`
HTTP 200 且 `ready=true`；推理端点可达；served model 精确匹配；流式性能用流式
backend；warmup 从正式测量中剔除。

## 原生路径

在用户认可的 runtime root 下用 `--search` 复制最小流式 vLLM model/dataset 配置到
run 级区域，只改副本。model 须记录 tokenizer/served name、stream、endpoint、rate、
batch、超时、max_out_len、generation kwargs、secret 引用；dataset 须记录路径或
synthetic 参数。

```bash
ais_bench --config-dir <RUN_CONFIG_DIR> --models <RUN_MODEL_CONFIG> \
  --datasets <RUN_DATASET_CONFIG> --mode perf \
  --summarizer <default_perf|stable_stage> --work-dir <RUN_OUTPUT_DIR> \
  --num-prompts <DATA_NUM> --num-warmups <CONFIRMED_WARMUPS>
```

只使用已安装 `--help` 中的 flag。正式原生禁止 `--debug`，性能路径不混入精度
flag。归档 dumped config、日志、性能 CSV/JSON、请求级 JSONL。

## Prefix wrapper 路径

仅在需要 prefix 构造或自动 prefix 指标时使用。`repeat_rate` 为每个目标输入的
公共前缀比例（`[0,1]` 或百分比），不是观测命中率。观测命中率：
`(hits_after - hits_before) / (queries_after - queries_before)`。定长时约
`prefix_len = int(input_len * repeat_rate)`，再加 3 个 seed 随机 token + suffix。

wrapper 与 AISBench `WORK_PATH` 必须是专用可变副本，禁止指向 tracked/共享目录。
打流前列出将创建/替换的路径（`temp_api.py`、日志/CSV、数据集、`picked_ids.txt`、
`vllm_api_chat_temp.py`、GSM8K jsonl）。**wrapper 会把 `API_KEY` 持久化到生成的
Python；除非 run 级副本已改为不持久化的 secret 引用，鉴权运行 fail closed。**

```bash
python3 aisbench_test.py --input_len <N> --output_len <N> --data_num <N> \
  --concurrency <N> --request_rate <N> --dataset_type prefix_cache \
  --repeat_rate <R> --prefix_num <N> --seed <N> --prefix_test --dp <N>
```

上游 wrapper 硬编码原生 `--debug`：正式运行只用 run 级副本并仅移除该 flag，保存
diff。`--num-warmups` 不受支持时同样只在 run 级副本移除。禁止 `--repeat > 1`。
绝不修改共享 wrapper 或第三方安装。

Prefix 取证：warmup 与正式负载前后立即保存原始 `/metrics`，分别上报 HBM 与
external 的 `warmup_prefix_hit_rate` / `formal_prefix_hit_rate`。要求精确
Pod/engine 集合且每个 DP warmup 查询增量为正；负增量、计数器重置、无关流量或
序列不完整 → 命中率 unavailable。确认专用 root 后删除本次 `picked_ids.txt`。

## 结果读取

优先从原生结构化产物提取：总/成功/失败请求数、duration、achieved request rate、
concurrency、request/token throughput、TTFT、TPOT/ITL、E2E 和实际 token 分布。
字段缺失时报告 schema gap，不从日志摘要猜值。

原生 JSON/JSONL/CSV 是权威产物；wrapper `aisbench_result.csv` 只是次级汇总，须与
同窗口原生产物交叉核对。`total_req` 不是成功数；wrapper parser 的 `99999`/`9999`
是失败哨兵。字段缺失、哨兵、traceback 或 wrapper 与原生不一致 → 本次失效。

负载机未达到目标 rate/concurrency 时结果描述客户端能力上限，不能作为 Motor 服务端
上限。比较两次运行前逐项核对环境、revision、拓扑、模型、workload 和 cache state。
未达目标时标记 `client-limited`，并记录负载机 CPU/内存/worker/网络。

## 证据与停止

证据复制到 `.motor-local/benchmark-runs/<namespace>-<timestamp>/`，含命令、
revision、测量窗口、脱敏 secret 引用、原生产物；wrapper/prefix 运行另存对应输出。

出现以下任一情况立即停止正式负载并保留失败证据：context/served model 不匹配；
readiness 失败；CLI/schema 不受支持；鉴权 wrapper 缺少非持久化 secret；`WORK_PATH`
为共享或 tracked；100% 4xx/5xx / `RECV=0` / 全失败；缺少成功/失败计数；产物为空、
哨兵、traceback 或陈旧；命令含 `--debug`；`client-limited`；prefix 指标无效。
