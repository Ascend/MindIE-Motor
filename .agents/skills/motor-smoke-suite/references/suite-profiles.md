# Pymotor validation suite

The suite is the collection of concrete Pymotor acceptance cases below. It has
no A/B/C/D stage model. Deployment, readiness, inference probes, restoration,
and cleanup are fixtures or case assertions rather than user-visible cases.

For scheduled/CI runs, use `unattended` mode and an approved profile as defined
in [unattended-authorization.md](unattended-authorization.md). Case ids below
map to profile `cases` / `allowed_actions` tokens. **Owning capability** names
identify the Skill contract owner under `.agents/skills/`.

## Cases

The full suite runs these cases in the established order. A narrower run may
select a subset explicitly.

| Order | Case id | Case | Owning capability | Allowed action | Legacy case | Pass condition |
|---:|---|---|---|---|---|---|
| 1 | `prefill-scale-up-1-to-2` | Prefill online scale 1→2 | `motor-scale` | `update_instance_num` | `pymotor_ras_up_down_scale_0001` | target topology is observed and post-scale inference succeeds |
| 2 | `coordinator-failover` | Coordinator active/standby failover | `motor-reliability` | `reliability:coordinator-failover` | `ras_coordinator_active_passive_0004` | takeover occurs within the bounded wait and inference remains or becomes healthy |
| 3 | `decode-engine-restart` | Decode engine-process restart | `motor-reliability` | `reliability:decode-engine-restart` | `ras_pd_restart_0002` | Decode returns to the expected topology and inference recovers |
| 4 | `prefill-link-isolation` | Prefill link isolation and redundant recovery | `motor-reliability` | `reliability:prefill-link-isolation` | `ras_p_redundant_0001` | isolation, redundant recovery, inference recovery, and link restoration are all proved |
| 5 | `gpqa-accuracy` | GPQA accuracy | `motor-validation-accuracy` | `accuracy:gpqa` | `pymotor_acc_0008` | decimal accuracy is at least `0.797` |
| 6 | `gsm8k-performance` | GSM8K performance | `motor-validation-benchmark` | `benchmark:gsm8k` | `pymotor_performance_0009` | failed requests are zero and the measured workload passes a supplied comparable baseline |

Shared fixtures that create or delete the service require `deploy` and
`delete_owned` in the approved profile even though they are not separate cases.

The GPQA boundary has exactly one canonical rule: **pass iff decimal accuracy
`>= 0.797`**. The legacy mean `0.807` is a historical reference only (the legacy
expression `abs(actual - 0.807) <= 0.01 or actual > 0.807` expands to the same
one-sided `>= 0.797` rule — it is not a two-sided window). Performance must not compare
results from incompatible hardware, topology, model, dataset, generation
parameters, concurrency, request rate, or tool versions.

## Reference workload intent

Record **what** the suite is meant to exercise. Resolve **where** (host paths,
image digests, weight mounts) on the live endpoint when approving a profile or
starting a run. Do not treat absolute paths as part of this Skill.

| Dimension | Reference intent |
|---|---|
| Engine | vLLM Ascend, PD disaggregated |
| Hardware class | Ascend `800I_A2` |
| Model family | DeepSeek-V3.1 Terminus w8a8 + MTP (QuaRot); served name `dsv3` |
| Quant / speculative | `quantization=ascend`; speculative `deepseek_mtp` with 1 draft token |
| KV transfer | MooncakeConnectorV1 — Prefill `kv_producer`, Decode `kv_consumer` |
| Starting topology | Prefill instances `1`, Decode instances `1` |
| Prefill packing | 2 pods / instance × 8 NPU / pod |
| Decode packing | 4 pods / instance × 8 NPU / pod |
| Prefill parallel | TP=`8`, DP=`2`, EP enabled |
| Decode parallel | TP=`1`, DP=`32`, EP enabled |
| Context | `max_model_len=16384` |
| HA fixture | Controller and Coordinator master/standby enabled |
| Scale case | Case 1 mutates Prefill `1→2`; baseline must show `P=1` before mutation |

An approved unattended profile may narrow or substitute this intent only when the
change is explicit (different model family, parallel sizes, or topology). Paths
for `user_config` / `env` / weights remain endpoint-local placeholders.

## Common fixtures

Setup/teardown must keep these outcomes without turning them into cases: prepare
run-scoped config and evaluator workspace; create or reuse only the intended
service; wait on a bounded condition (no unexplained sleep); run each case's
inference probe; restore every injected fault; delete only this run's resources;
preserve raw output, configuration, revisions, commands, metrics, and cleanup.

## Result rules

- `PASS`: the case's complete pass condition is proved by raw evidence.
- `FAIL`: the case ran validly and its assertion failed.
- `BLOCKED`: a required target, permission, input, dataset, tool, or comparable
  baseline is missing.
- `NOT RUN`: the case was not selected or could not safely run after an earlier
  restoration failure.
- `RESTORATION FAILED`: the case changed the target and could not prove it was
  restored. Stop before another mutation or load.

Overall `PASS` requires every selected case and final cleanup to pass. After an
ordinary isolated case failure, preserve evidence and continue only when the
target is restored and the next case is independent.
