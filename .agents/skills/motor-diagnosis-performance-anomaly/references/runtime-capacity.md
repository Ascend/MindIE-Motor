# Runtime capacity diagnosis

Verify that deployed effective capacity matches intent and locate where a valid performance miss manifests. Align every observation to the formal benchmark window, service, role, instance, Pod, rank, and node; a later healthy snapshot does not refute an earlier anomaly.

## Trace configuration to runtime

```text
user_config.json and env.json
-> deployer output
-> live workload and ConfigMap
-> Pod resources, environment, and arguments
-> runtime-visible devices, Engine ranks, and active workers
```

Read matching definitions/source when field defaults vary by revision. For active Prefill and Decode roles, record model, instance count, devices per instance, TP/DP and other parallelism, expected/active workers, and expected/visible devices. When native semantics support it, calculate:

```text
expected_role_devices = sum(instance_count * devices_per_instance)
worker_gap = expected_workers - active_workers
device_gap = expected_devices - runtime_visible_devices
```

Also compare implicated capacity settings: maximum sequences/batched tokens, model length, memory utilization, block/cache settings, dtype/quantization, scheduling policy, speculative mode, and served model. Distinguish insufficient intent, config drift, device visibility loss, unschedulable capacity, and workload-dependent P/D ratio mismatch; consistency does not prove sufficiency.

Build an internal comparison before attribution:

| Item | Intended | Generated/live | Runtime |
|---|---|---|---|
| P/D instances | Native config | Workload replicas | Active registrations |
| Devices | Configured per instance | Pod requests/limits | Visible IDs/world size |
| Parallelism | Native config | Args/env | Engine ranks/logs |
| Capacity knob | Value/default | ConfigMap/args | Startup log/metric |

## Runtime checks

- **Engine:** expected versus active workers, readiness/restarts, model load, registration, endpoint churn, errors/timeouts, ranks/world size, and per-Engine traffic.
- **Queues/service:** per-role running/waiting requests, prompt/generation TPS, latency histograms, and request distribution. Diagnose imbalance only when skew and degradation coincide.
- **P/D and handoff:** require aligned role demand, queue/TPS, idle peer capacity, or transfer traces/metrics/errors; absent transfer evidence leaves the factor unresolved.
- **Motor:** inspect matching-revision source, logs, metrics, and traces in the current diagnosis for scheduling, routing, forwarding, Prefill, Decode, and handoff semantics. Leave the factor unresolved when semantics cannot be proven; do not load a validation Skill as a second owner. Do not label composite first-byte time as pure Motor overhead.
- **Platform:** align NPU/CPU/memory/network, throttling, power/frequency, placement, competing Pods, packet loss/retransmit, and node differences with the run.
- **Software:** record Motor, vLLM, vLLM-Ascend, image/wheel, driver, firmware, CANN, and benchmark revisions. Require compatible A/B or a runtime/source change tied to the delta before calling regression.

Queue growth plus throughput plateau supports saturation, not necessarily a defect. KV-cache, preemption, or recompute requires a consistent combination/trend; one gauge is insufficient.

## Factor and exclusion rules

| Factor | Minimum support | Strong exclusion evidence |
|---|---|---|
| Config capacity/drift | Insufficient intent or causal intended/generated/live/runtime divergence | All layers agree and compatible evidence proves sufficient capacity |
| Device/placement | Requested, allocated, visible, ranked, or active capacity differs | Device and worker layers agree throughout the window |
| Engine degradation | Expected Engine missing, restarting, stalled, unregistered, or not serving | Every expected Engine remains ready, registered, and serving |
| Engine/P-D imbalance | Persistent demand/queue skew with idle peer capacity | Demand and service distribution remain balanced |
| Prefill/Decode limit | Role-specific queue/time/TPS degradation matches the workload phase | The target role is stable while another layer degrades |
| KV/handoff | Aligned transfer trace, metric, error, retry, or bandwidth evidence | Aligned transfer evidence is stable; absence is unresolved |
| Motor scheduling/routing | Material measured stages or correlated routing imbalance | Motor stages are small/stable and routing is balanced |
| Platform contention | Overlapping resource/network contention explains affected capacity | Measured headroom exists across affected nodes |
| Software regression | Compatible A/B isolates a software/runtime change | Same revision reproduces both states or comparison is incompatible |

Report the earliest proven deviation in a causal chain. Exclude config, devices, Engine, transfer, Motor, or platform factors only when all relevant same-window layers provide direct contrary evidence; silence is unresolved. Keep diagnosis read-only: do not edit, restart, scale, reschedule, or profile.
