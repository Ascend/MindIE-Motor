---
name: motor-validation-accuracy
description: Internal atomic workflow selected only by motor-validation for repeatable native AISBench answer-correctness evaluation with a user-confirmed labeled dataset, reference answers, and native evaluator, including AIME, MATH-500, GPQA, GSM8K, C-Eval, MMLU, or LiveCodeBench. Do not invoke directly or use for performance-only load、QPS、TTFT、TPOT、throughput benchmarking, deployment, or diagnosis.
---

# motor-validation-accuracy

## Entry guard

Continue only when `motor-validation` selected accuracy for the current validation stage and identified the answer-correctness contract: dataset/scope, reference answers, and native evaluator. `AISBench`, a dataset name, or incidental latency metrics are insufficient. If the goal is performance load or diagnosis, stop and return to the parent router instead of loading another validation Skill.

Read [aisbench-accuracy.md](references/aisbench-accuracy.md) before constructing commands. Use native `ais_bench` and its dataset evaluator; never select a default dataset or add a custom scorer. The user supplies or confirms the dataset, dataset-specific flags, generation protocol, and acceptance threshold.

## Dataset and environment contract

Before service actions, obtain or confirm the AISBench task/config, exact data path, split/subset, and presence of reference answers. If missing, show the selection table in the reference, wait for a dataset/destination choice, and obtain authorization before any download. Verify the path, labels, config, and evaluator inside the designated AISBench runtime; never use synthetic performance data as ground truth.

Bind one remote Motor target and one AISBench container, Pod, or virtual environment. Reuse a ready service or route an explicitly authorized start through `motor-deploy`, wait for readiness, then run every probe and command in the same AISBench runtime.

Resolve from native config, live Kubernetes, and that runtime before asking:

- served model, reachable Coordinator inference endpoint, `max_model_len`, P/D topology, hardware, image/package revision, and tokenizer/model path;
- installed AISBench/Python version, supported flags, model class, dataset config/evaluator, and output schema.

Ask only for unresolved model config, run root, formal generation/load settings, or threshold. Do not invent a formal profile.

## Preflight and preparation

1. Confirm dataset selection, path, scope, reference answers, evaluator, and revision/checksum.
2. Verify readiness, endpoint reachability, `/v1/models`, served-model consistency, and stable workloads from the AISBench runtime.
3. Verify prompt bound plus output cap fits one consistent Motor `max_model_len`.
4. Confirm installed CLI/model/dataset/evaluator/postprocessor capabilities without installing or upgrading anything; keep credentials reference-based and redacted.
5. Create unique run-scoped config and output directories. Copy only required configs; never edit shared `site-packages` or tracked AISBench source.
6. Show resolved configs, smoke/formal commands, dataset identity, output/archive paths, runtime mutations, expected metrics/denominator, and stop conditions before inference.

## Execute

- Run a user-confirmed small subset with the same dataset, model config, prompt, generation, postprocessor, and evaluator as the formal run; do not hard-code its size.
- Run native AISBench without performance-only mode. Include `--dump-eval-details`; use `--merge-ds` only when the installed dataset contract requires it.
- Give each dataset invocation/repetition a unique `--work-dir`. Run long evaluations as one monitored remote job and determine its state before retrying.
- Preserve failed smoke/formal attempts as `invalid` or `failed`; never overwrite a finalized run.

## Result correctness

Treat native evaluator output and dumped details as authoritative. A run is valid only when task/config resolution, inference/evaluation counts, metrics, denominator, and current-run artifacts are explicit. Report request, empty/truncated output, output-cap, parser/postprocessor, and evaluator failures separately from wrong model answers.

Compare runs only when dataset/config/revision/scope, evaluator/merge behavior, prompt/postprocessor, model/tokenizer, generation/output cap, load policy, topology/software, and AISBench backend match. Otherwise report absolute results and the comparability gap, not a regression.

## Evidence and report

Archive under `.motor-workspace-local/accuracy-runs/<namespace>-<dataset>-<timestamp>/`. Preserve a manifest, redacted command, resolved configs, environment/version fingerprint, dataset identity/checksums, native output, evaluation details, and `summary.json` with native metrics, denominators, failures, and validity. Keep invalid attempts with their reason; do not create `report.md`.

Return exactly:

```markdown
## Motor 精度测试结果：
服务：<SERVICE>
模型：<SERVED_MODEL>
数据集：<DATASET_TASK_AND_SCOPE>
原生评估器：<EVALUATOR>
Smoke：<SMOKE_RESULT>
正式结果：<NATIVE_METRIC_VALUE_AND_DENOMINATOR>
请求失败、空输出、解析失败、输出触顶：<FAILURE_COUNTS>
结果状态：<VALID_INVALID_FAILED>
阈值结论：<THRESHOLD_DECISION>
比较结论：<COMPARISON_DECISION>

## 结果目录：
<ABSOLUTE_RUN_DIRECTORY>

下面是结果目录下各个主要文件的用途：

### 汇总：summary.json

<INCORRECT_COUNT> 条错误明细：<INCORRECT_CASES_PATH_OR_未生成>
AISBench 原始结果：<FORMAL_NATIVE_OUTPUT_PATH_OR_未生成>
```

Use native metric names and actual counts. Mention only paths that exist; write `未生成` otherwise.

Latency and throughput emitted during an accuracy run are supporting execution evidence only. Do not turn them into a performance benchmark conclusion or route to `motor-validation-benchmark` from this Skill.
