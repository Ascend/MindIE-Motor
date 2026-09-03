# Benchmark validity

Validate the measurement before attributing it to Motor. Prefer native AISBench JSON/JSONL/CSV and complete command output over summaries.

## Required facts

Record the exact command and benchmark version; client host/resources; endpoint and time window; warmup boundary; requested and achieved concurrency/rate; total/success/failed requests; duration; actual input/output token distributions; timeouts/retries; streaming and generation settings; dataset; and cache/prefix conditions.

Reject all-failed traffic, empty results, repeated Bad Request, sentinel values, stale artifacts, or summaries inconsistent with native output. Do not treat `total_req` as success count.

## Quantitative checks

```text
request_waves = successful_requests / max(achieved_average_concurrency, 1)
tail_support(q) = successful_requests * (1 - q)
rate_achievement = achieved_request_rate / requested_request_rate
throughput_gap = observed_throughput - target_or_baseline_throughput
latency_gap = observed_latency - target_or_baseline_latency
```

Use the declared protocol, user SLO, or compatible baseline rather than universal thresholds. Low request waves, a short steady window, or small tail support makes tail percentiles unstable.

## Workload-side factors

| Factor | Required mechanism evidence |
|---|---|
| Insufficient sample | Warmup dominates, too few request waves, or inadequate percentile support |
| Under-driven service | Throughput rises under compatible higher load while queues and device use remain low |
| Overload | Throughput plateaus while queueing and latency grow at the intended stress load |
| Client limited | Achieved load misses target and measured client CPU/network/event-loop/tokenization/pacing explains it |
| Workload mismatch | Actual lengths, EOS, dataset, streaming, sampling, template, tokenizer, or cache profile differs |
| Invalid measurement | Failures, mixed traffic, counter/window errors, warmup contamination, stale output, or bad aggregation |
| Incompatible baseline | Material model, hardware, topology, revision, engine, workload, cache, backend, or client difference |

Exclude a factor only with same-window positive evidence: achieved load plus client headroom, protocol-sufficient steady/tail support, actual token/generation comparison, or aligned cache/warmup state. Missing observations do not exclude it.
