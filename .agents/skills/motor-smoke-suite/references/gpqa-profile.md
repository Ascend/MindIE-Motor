# GPQA accuracy profile contract

Suite-owned contract for the `gpqa-accuracy` case (legacy `pymotor_acc_0008`).
This file fixes the dataset, generation parameters, and validity gates for that
case. Executing AISBench is the generic accuracy executor's job — the suite
routes it to `motor-validation-accuracy`, passing this confirmed profile; it
does not re-implement AISBench execution here.

## Profile `gpqa-diamond-0shot-cot-chat`

### Purpose

Evaluate Graduate-Level Google-Proof Q&A (`GPQA_diamond`) accuracy through
AISBench against a live Motor inference endpoint.

### Resolved runtime values

Create run-scoped copies of the installed AISBench model and dataset configs.
Do not edit the shared installation tree.

| Field | Legacy pymotor value | Notes |
|---|---|---|
| model config | streaming/general chat vLLM API adapter | use installed `--search` to locate the closest general-chat model config |
| dataset | `gpqa_gen_0_shot_cot_chat_prompt` | 0-shot chain-of-thought chat prompt |
| endpoint | Coordinator inference Service | legacy used host IP + port `31015`; derive from live Service/NodePort |
| `max_out_len` | `32768` | legacy record only, not part of the formal contract; formal runs must set a run-scoped `max_out_len <= max_model_len - input_len` (`max_model_len=16384` in the reference workload) |
| evaluator batch size | `32` | AISBench model config field |
| `trust_remote_code` | `true` | required for the served model |
| `temperature` | `1.0` | generation kwargs |
| `top_p` | `0.95` | generation kwargs |
| `chat_template_kwargs.thinking` | `true` | enable thinking-style answers when supported |

### Commands

Discover the installed CLI first. A representative formal shape is:

```bash
ais_bench \
  --config-dir <RUN_CONFIG_DIR> \
  --models <RUN_MODEL_CONFIG> \
  --datasets <RUN_DATASET_CONFIG> \
  --work-dir <RUN_OUTPUT_DIR>
```

Only include flags shown by the installed `--help`. Formal runs must not include
`--debug`. The legacy one-shot command
`ais_bench --models vllm_api_general_chat --datasets gpqa_gen_0_shot_cot_chat_prompt --debug`
is reference only — do not use it verbatim for formal evidence.

### Metric extraction

Read aggregate accuracy from the native result artifact, typically under
`results/.../GPQA_diamond.json`, converting percentage strings to decimal.

Required extracted fields: dataset name/subset (`GPQA_diamond`), aggregate
`accuracy`, result artifact path, evaluator version, and runtime mutation list.

### Shared AISBench rules

Follow the accuracy executor's gates (`motor-validation-accuracy`), plus: readiness and served-model agreement before load; run-scoped
mutable config/output directories; complete artifact archival; stop on empty
results, all-failed requests, or stale artifacts.

Accuracy execution failure is not a performance regression. Preserve raw evaluator
output and report an accuracy-specific diagnosis gap when root-cause attribution is
requested. Suite pass/fail is the canonical rule defined in
[suite-profiles.md](suite-profiles.md) (decimal accuracy `>= 0.797`), asserted
by the suite from the extracted raw value, not by the executor.
