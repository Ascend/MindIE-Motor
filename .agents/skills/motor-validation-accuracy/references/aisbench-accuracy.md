# AISBench native accuracy evaluation contract

Use the selected native AISBench dataset config and evaluator. Dataset metrics, postprocessing, category structure, and merge behavior differ; do not add a custom scorer or normalize them into one generic accuracy value.

## Required contract

The user supplies or confirms the dataset/task list, data source/path/revision, split/subset, reference answers, dataset-specific flags, generation/thinking settings, postprocessor, smoke size, repetitions, and acceptance threshold. Resolve target endpoint/model, `max_model_len`, topology, AISBench runtime/version, model config, load settings, and archive root from current facts before asking.

Accuracy may use sampling when the selected protocol requires it. Preserve the protocol and each seed/run; do not silently replace it with greedy decoding or run every dataset listed below.

## Dataset availability

Verify the task name, actual readable path, labeled scope, sample count, reference-answer mapping, evaluator, and checksums inside the designated AISBench runtime. A resolvable config alone is insufficient.

If no usable dataset is supplied, show this table and wait for the user's selection and any download authorization:

| 选项 | 数据集 | Expected local state | Native evaluator/use |
|---:|---|---|---|
| 1 | GSM8K-160 | 可直接使用 | `Gsm8kEvaluator` |
| 2 | AIME 2024 | 配置已安装，数据缺失 | `MATHEvaluator` |
| 3 | AIME 2025 | 配置已安装，数据缺失 | `MATHEvaluator` |
| 4 | MATH-500 | 配置已安装，数据缺失 | `MATHEvaluator` |
| 5 | GPQA | 配置已安装，数据缺失 | `GPQAEvaluator` |
| 6 | C-Eval | 配置已安装，数据缺失 | `AccEvaluator` |
| 7 | MMLU | 配置已安装，数据缺失 | `AccEvaluator` |
| 8 | LiveCodeBench Lite | 配置已安装，数据缺失 | Code execution evaluation |

Treat the status as an expected baseline until verified. After selection, show the installed task name, official source, expected layout, size/scope, dependencies, and access constraints. Let the user choose a dataset root; on offline hosts provide staging instructions. Never download silently, write data into shared `site-packages`, or substitute synthetic data.

## Execution order

```text
confirm dataset
-> bind remote Motor target
-> reuse or explicitly start Motor and wait for readiness
-> enter designated AISBench runtime
-> verify AISBench, dataset, endpoint, and gates there
-> smoke
-> formal run
-> archive
```

The accuracy Skill does not implement deployment. Route authorized lifecycle actions through `motor-deploy`; restrict all environment conclusions and `python3`, `pip`, `ais_bench`, loader, and endpoint probes to the bound target/runtime.

## Preflight gates

Store redacted command output and a `pass|fail` result under `<RUN_DIR>/preflight/`. Any failure blocks smoke but retains evidence.

- **Service:** from the AISBench runtime, require management `/readiness` HTTP 200 with `ready=true`, reachable inference `/v1/models`, exact AISBench `model=` match, consistent active `served_model_name`, and stable Ready workloads.
- **Context:** require one active-engine `max_model_len` and prove `maximum_rendered_prompt_tokens + max_out_len <= max_model_len` with a documented bound or resolved tokenizer/template.
- **Capability:** record `python3 --version`, `pip show ais-bench-benchmark`, `ais_bench --help`, and `ais_bench --models <MODEL> --datasets <DATASET> --search`; confirm model class, dataset, evaluator, postprocessor, schema, and flags without installing/upgrading.
- **Secret:** use `auth=none` or an approved secret reference; never persist values in commands, configs, logs, or artifacts.
- **Dataset:** require readable labeled data, non-empty references, resolved config/evaluator, and current checksums.

Representative service probes from that runtime are:

```bash
curl --fail --silent --show-error <MANAGEMENT_BASE_URL>/readiness
curl --fail --silent --show-error <INFERENCE_BASE_URL>/v1/models
```

## Run-scoped preparation

Create a unique directory before smoke and place resolved configs where the designated runtime can read them. Prefer installed data/configs; otherwise copy the smallest required trees to run-scoped `--config-dir`. Use a disposable environment or approved run-scoped import path for plugins that cannot load there. Never mutate shared `site-packages`, tracked source, or an existing run.

Use this run layout or its version-compatible equivalent:

```text
.motor-workspace-local/accuracy-runs/<namespace>-<dataset>-<timestamp>/
  command.txt
  manifest.json
  environment.json
  resolved-config/
  dataset-manifest.json
  native-output/
  summary.json
```

Resolve the model config from facts. The testing-team pattern is:

```python
from ais_bench.benchmark.models import VLLMCustomAPIChatStream
from ais_bench.benchmark.utils.model_postprocessors import extract_non_reasoning_content

models = [dict(
    attr="service",
    type=VLLMCustomAPIChatStream,
    abbr="vllm-api-general-chat",
    path="<MODEL_OR_TOKENIZER_PATH>",
    model="<SERVED_MODEL_NAME>",
    request_rate=<USER_CONFIRMED_REQUEST_RATE>,
    retry=<USER_CONFIRMED_RETRY>,
    host_ip="<COORDINATOR_HOST>",
    host_port=<COORDINATOR_PORT>,
    max_out_len=<USER_CONFIRMED_MAX_OUT_LEN>,
    batch_size=<USER_CONFIRMED_BATCH_SIZE>,
    generation_kwargs=dict(
        ignore_eos=<USER_CONFIRMED_IGNORE_EOS>,
        top_p=<USER_CONFIRMED_TOP_P>,
        temperature=<USER_CONFIRMED_TEMPERATURE>,
        chat_template_kwargs={"thinking": <USER_CONFIRMED_THINKING>},
    ),
    pred_postprocessor=dict(type=extract_non_reasoning_content),
)]
```

Treat every value as protocol-sensitive. Confirm version-specific semantics such as `request_rate=0`, the exact thinking keyword, and whether the selected protocol requires the postprocessor. Keep raw predictions when postprocessing and keep secret values out of archived configs.

Use the installed task names found by `--search`. One representative command shape is:

```bash
ais_bench --config-dir <RUN_CONFIG_DIR> --models <MODEL_CONFIG> \
  --datasets <DATASET_CONFIG> --work-dir <UNIQUE_RUN_OUTPUT_DIR> \
  --dump-eval-details [--merge-ds only when required]
```

Testing-team example mappings are:

| Dataset | Installed task example | `--merge-ds` |
|---|---|---|
| AIME 2024 | `aime2024_gen_0_shot_chat_prompt` | No |
| MATH-500 | `math500_gen_0_shot_cot_chat_prompt` | No |
| GPQA | `gpqa_gen_0_shot_cot_chat_prompt` | No |
| GSM8K | `gsm8k_gen_0_shot_cot_chat_prompt` | No |
| C-Eval | `ceval_gen_0_shot_cot_chat_prompt` | When required by config |
| MMLU | `mmlu_gen_0_shot_cot_chat_prompt` | When required by config |
| LiveCodeBench Lite | `livecodebench_code_generate_lite_gen_0_shot_chat` | When required by config |

Verify every name and flag against the installed version; these mappings are examples, not a default suite.

Do not add `--mode perf`. Give every dataset and repetition its own output directory; use a campaign index for multiple child runs.

## Smoke, formal run, and result validation

Before inference, show dataset/path/scope/evaluator, revisions/checksums, resolved model config/postprocessor, smoke/formal commands, paths, expected metrics/denominator, context headroom, and stop conditions.

Use a user-confirmed small subset through a supported flag or run-scoped dataset config while keeping the formal protocol unchanged. Require non-empty inference, evaluation details, and expected metrics before launching one monitored formal job. Do not duplicate a job until its state is known.

Treat native evaluator output as the score of record. Report native metric/value, numerator/denominator when available, request/evaluation counts, failures, missing/empty/truncated/output-cap predictions, raw/postprocessed evidence, time window, and current artifact paths. HTTP success is not accuracy; never guess dataset size, merge unrelated metrics, or hide failed cases in an aggregate.

## Evidence and comparison

The manifest must identify the run, Motor target/runtime, AISBench runtime/version, endpoint/model/tokenizer, topology/config/images/packages, exact redacted command, generation/load settings, dataset/config/scope/checksums, evaluator/metrics/denominator/failures, artifact paths/hashes, and final validity reason. Archive complete timestamped native output and details; create a new run for retries.

Compare only matching dataset bytes/config/scope, evaluator/merge behavior, model/tokenizer, prompt/template/postprocessor, generation/output cap/load/retry policy, Motor topology/software, client environment, and AISBench backend/version. Otherwise report both absolute results and the gap without claiming regression.

## Stop conditions

Stop and preserve evidence when:

- dataset/protocol selection or required authorization is missing;
- Motor never becomes ready or the designated AISBench runtime is unavailable;
- endpoint, served model, context length, or credentials are inconsistent;
- data path, labeled scope, reference answers, config, or evaluator is unresolved;
- planned model class, flags, postprocessor, or output schema is unsupported;
- the command would mutate shared `site-packages` or tracked source;
- smoke/evaluation fails or emits empty/stale metrics or details;
- request failures, parser errors, empty outputs, or output caps bias the score;
- output paths already contain another run or denominators cannot be established.
