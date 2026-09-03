# Smoke suite failure routing

Read only after a selected case fails or blocks. This file is a **symptom →
Skill** index. Category definitions and report shape live with
[`motor-diagnosis`](../../motor-diagnosis/SKILL.md) and its specialized
diagnosis Skills.

## First response

1. Record UTC time, endpoint, context, namespace, case id, assertion, expected,
   observed, and elapsed time.
2. Preserve read-only evidence before another mutation, load, or cleanup; do
   not restart, redeploy, delete, edit config, scale, or inject another fault
   until that evidence is saved.

## Symptom → Skill

| Failed observation | Route |
|---|---|
| Workload missing / not Ready; Service missing; `/readiness` bad; inference connect/timeout/5xx/stream break; scale topology or post-scale inference miss; accuracy evaluator invalid/incomplete; reliability case fails without a specialized route | `motor-diagnosis` |
| Inference 4xx | Inspect sanitized request + live model/config first; then `motor-diagnosis` if server-side |
| Benchmark valid but misses throughput/latency baseline | `motor-validation-performance` |
| Benchmark has failed requests / unhealthy Pods / unreachable service | `motor-diagnosis` first (correctness before perf) |
| Controller precision auto-recovery / terminate markers present | `motor-diagnosis`, then `motor-diagnosis-control-plane` |
| GPQA ran validly but misses accuracy baseline | Keep raw evaluator output; no specialized accuracy-attribution Skill yet |

Default collector is `motor-diagnosis`; use its result categories and
symptom / proximate-cause / root-cause / repair rules. This suite never
auto-repairs.
