---
name: motor-diagnosis-performance-anomaly
description: Internal atomic workflow selected only by motor-diagnosis when an existing Motor online-serving benchmark artifact misses an explicit compatible target or baseline and the cause is unknown. Diagnose high TTFT/TPOT or low throughput from same-episode evidence. Do not invoke directly or use for ordinary performance analysis, benchmark generation, accuracy, deployment/startup, or deep operator/kernel/HCCL profiling.
---

# Motor performance anomaly diagnosis

## Entry guard

Continue only when `motor-diagnosis` has established the current evidence episode, raw benchmark artifact, compatible target/baseline, observed miss, and unknown-cause diagnosis intent. A metric name such as TTFT, TPOT, or throughput is insufficient. Without every entry condition, stop and return to the parent router instead of loading a validation Skill.

Perform read-only diagnosis before deep profiling. Prove the benchmark result is valid, verify effective runtime capacity against deployment intent, then classify supported and excluded factors. Different factors may explain different metrics; do not force one global cause.

## Load references

1. Always read [benchmark-validity.md](references/benchmark-validity.md) before attributing a result to Motor.
2. Read [runtime-capacity.md](references/runtime-capacity.md) after that gate passes, or earlier when config, device, placement, Engine, P/D, Motor, platform, or software evidence is implicated.

## Establish one evidence episode

Record endpoint, kube context, namespace, benchmark window, target or baseline, model, hardware, P/D topology, revisions, warmup/cache state, exact command, and raw artifacts. Derive facts from config, benchmark artifacts, Kubernetes, logs, and metrics before asking the user; redact secrets.

Combine evidence only when the window, service, model, config, and software revision align. A later run or changed deployment is a new episode.

## Diagnose the target miss

1. Establish the target and compare only with an SLO or baseline compatible in workload, model, hardware, topology, software, cache, and client conditions.
2. Validate requested and achieved load, request counts/waves, steady duration, percentile support, actual token distributions, client capacity, failures/retries/timeouts, warmup/cache, and artifact freshness.
3. Trace implicated config through generated resources, live workload/ConfigMap, Pod resources/arguments, visible devices, Engine ranks/world size, and active workers.
4. Align Engine health, queues, per-worker throughput, P/D balance, KV handoff, Motor scheduling/routing, platform resources/network, competing workloads, and revisions to the benchmark window.
5. Read matching Motor, vLLM, or vLLM-Ascend source when metric or config semantics vary by revision; never infer ownership from an unverified name.

Use symptoms only to prioritize evidence:

- **High TTFT:** workload validity, Prefill queue/time/TPS, scheduling/admission, handoff, and network delay.
- **High TPOT:** actual output length, Decode queue/time/TPS, active Decode workers, request distribution, KV pressure, and devices.
- **Low throughput:** load/client limits or overload, then effective P/D, worker/device, and platform capacity.
- **Unknown miss:** run every gate without preselecting a layer.

## Factor rules

Publish a diagnosed factor only with a concrete object/parameter, observed deviation and expected reference, aligned scope/window, measured mechanism or impact, decisive evidence/calculation, and medium/high confidence. If none qualifies, return `当前证据无法确定具体因素` and the smallest missing discriminator.

Exclude a factor only with direct refuting evidence from the same episode and the layer where it would manifest. Missing metrics, error silence, later snapshots, or healthy aggregates that hide a failed worker are insufficient.

List every independently supported factor and the metric it explains. Use `主因`, `叠加因素`, or `贡献未量化`; assign `主因` only with quantitative, comparative, or direct bottleneck evidence. Report the earliest proven deviation and its downstream effect once.

## Output

Return exactly two sections:

```markdown
## 诊断因素结果

| 诊断因素 | 主要解释指标 | 定位 | 具体结果 | 关键证据 | 置信度 |
|---|---|---|---|---|---|
| {domain tag} | {TTFT/TPOT/请求吞吐/输出 token 吞吐/E2EL} | {主因/叠加因素/贡献未量化} | {object + deviation + scope + mechanism/impact} | {artifact, timestamp/labels, calculation} | {高/中} |

## 已排除因素
| 已排除因素 | 排除依据 |
|---|---|
| {factor} | {same-episode direct refuting evidence} |
```

Keep raw metric names/units and concise evidence references; do not paste logs or secrets. When no factor is established, use one `未确定` row; write `无` in the exclusion table when nothing is directly excluded. Do not add candidate, remediation, or generic recommendation sections.

## Disputes, routing, and safety

For a disputed result:

1. Preserve the prior report/artifacts and mark the affected row disputed.
2. Verify whether the correction concerns the same run, window, and config; treat a later run as a new episode.
3. Check the claim against raw evidence rather than reversing it automatically.
4. Identify the failed fact, scope, time alignment, config/default semantic, causal inference, or exclusion rule.
5. Invalidate dependent results, collect the smallest read-only discriminator, and rerun from the earliest affected gate.
6. Regenerate the same two sections and state the revision without overwriting history.

- When controlled reproduction is the smallest missing discriminator, report the required workload and stop. A separately authorized request must start from `motor-validation`; do not load `motor-validation-benchmark` from this Skill.
- Perform revision-correct scheduling, routing, Prefill/Decode, and handoff attribution within this Skill's evidence depth. Report an evidence/capability gap when deeper profiling is required; do not load `motor-validation-performance`.
- Keep collection read-only. Load, profiling, config edits, scaling, restart/redeploy, node maintenance, or fault injection require the owning workflow and explicit authorization.
- Do not claim operator, kernel, HCCL, memory, or vLLM-Ascend root causes from aggregate metrics alone.
