# Automatic Elastic Scaling

## Feature Introduction

The automatic elastic scaling feature supports automatically adjusting the number of Prefill and Decode instances based on the real-time load of inference instances. It scales out automatically when the request volume rises and scales in when the load falls back, improving resource utilization while ensuring service SLA.

Core mechanism: Infer Operator creates Horizontal Pod Autoscaler (HPA) resources for inference instances. HPA obtains engine-level load metrics aggregated by MindIE Motor (such as the number of queued requests, TPS, and KV Cache usage) through the External Metrics Adaptor, and automatically adjusts the number of instance replicas according to the scaling thresholds configured by users.

## Working Principle

```text
┌──────────────────────────────────────────────────────────────────┐
│                         K8s Cluster                              │
│                                                                  │
│   ┌────────────┐     ┌───────────┐     ┌───────────────────┐     │
│   │    HPA     │     │  Infer    │     │    Engine Pods    │     │
│   │(autoscaler)|────>│ Operator  │────>│ (Prefill / Decode)│     │
│   └────────────┘     └───────────┘     └─────────┬─────────┘     │
│         ^                                        │               │
│         │                                        │               │
│         │        ┌──────────────────┐            │               │
│         │        │   MindIE Motor   │            │               │
│         └────────│   Coordinator    │<───────────┘               │
│   (External      │ (aggregation/TPS)│   /metrics                 │
│    Metrics API)  └──────────────────┘                            │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

1. The MindIE Motor Coordinator collects Prometheus metrics from all engine pods, aggregates them semantically, and exposes them through the `/metrics` endpoint.

2. The External Metrics Adaptor periodically pulls metrics from the Coordinator and converts them into the Kubernetes External Metrics API.

3. The HPA obtains load data from the External Metrics API and compares it with the user-configured target thresholds.

4. When a metric continuously exceeds the threshold, the HPA notifies the Infer Operator to increase the number of replicas; when it falls below the threshold, the number of replicas is decreased.

## Supported Product Models

- Atlas 800I A2 Inference Server

- Atlas 800I A3 SuperPoD Server

## Prerequisites

- Infer Operator has been installed and deployed.

- The MindIE Motor inference service has been deployed (PD disaggregation or PD co-location mode).

- External Metrics Adaptor has been deployed to convert MindIE Motor metrics into Kubernetes External Metrics. You can directly use the [Metrics Adaptor example provided by mindcluster-deploy](https://gitcode.com/Ascend/mindcluster-deploy/tree/master/infer-operator-metrics-adaptor) for deployment.

## Configuring MindIE Motor to Expose Metrics

The MindIE Motor Coordinator exposes aggregated engine metrics through the `/metrics` endpoint by default, and no additional configuration is required to use them.

The Coordinator `/metrics` endpoint provides multiple aggregation views, which can be switched using the `type` parameter:

| type Value | Description | Applicable Scenarios |
|---------|------|---------|
| `full` (Default) | Global aggregation, where metrics of all instances are aggregated into a single value | Prometheus scraping, HPA global scaling |
| `instance` | Instance-level metrics, with `instance_id` and `role` labels injected | Single-instance troubleshooting |
| `role` | Aggregation by role (Prefill / Decode) | Independent scaling by role |

```bash
# View the globally aggregated metrics (default)
curl http://{coordinator-ip}:1027/metrics

# View metrics separately by role
curl http://{coordinator-ip}:1027/metrics?type=role&role=prefill
curl http://{coordinator-ip}:1027/metrics?type=role&role=decode
```

## Deploying External Metrics Adaptor

The External Metrics Adaptor converts the Prometheus-format metrics of the Coordinator into the Kubernetes External Metrics API for consumption by the HPA.

You can directly deploy the [adaptor example provided by mindcluster-deploy](https://gitcode.com/Ascend/mindcluster-deploy/tree/master/infer-operator-metrics-adaptor), or implement it on your own as needed.

Before deployment, ensure that the Adaptor is configured with the correct Coordinator metrics endpoint address and scraping interval. After deployment, run the following command to verify that the metrics are available:

```bash
kubectl get --raw /apis/external.metrics.k8s.io/v1beta1 | grep -E "num_requests_waiting|motor:generation_tokens_per_second"
```

## Configuring the Elastic Scaling Policy

In `examples/deployer/yaml_template/infer_service_template.yaml`, add `scalingPolicy` under the configuration blocks of the Prefill and Decode roles.

The following example scales the Prefill role based on the number of queued requests and scales the Decode role based on the generation token rate:

```yaml
roles:
  # ========== Prefill Role ==========
  - name: prefill
    replicas: 4
    workload:
      apiVersion: apps/v1
      kind: StatefulSet
    scalingPolicy:            # New: Elastic scaling policy
      type: HPA
      spec:
        minReplicas: 1
        maxReplicas: 4
        metrics:
        - type: External
          external:
            metric:
              name: vllm:num_requests_waiting
            target:
              type: AverageValue
              averageValue: "5"
    metadata:
      labels:
        infer.huawei.com/gang-schedule: 'true'
    spec:
      # ... Keep the remaining configuration unchanged ...

  # ========== Decode Role ==========
  - name: decode
    replicas: 4
    workload:
      apiVersion: apps/v1
      kind: StatefulSet
    scalingPolicy:            # New: Elastic scaling policy
      type: HPA
      spec:
        minReplicas: 1
        maxReplicas: 4
        metrics:
        - type: External
          external:
            metric:
              name: motor:generation_tokens_per_second
            target:
              type: AverageValue
              averageValue: "10"
    metadata:
      labels:
        infer.huawei.com/gang-schedule: 'true'
    spec:
      # ... Keep the remaining configuration unchanged ...
```

### `scalingPolicy` Parameter Description

| Parameter | Description | Value |
|------|------|------|
| `scalingPolicy.type` | Type of the elastic scaling policy | Currently only `HPA` is supported |
| `scalingPolicy.spec.minReplicas` | Lower limit for scale-in. The number of instances will not fall below this value. | Positive integer |
| `scalingPolicy.spec.maxReplicas` | Upper limit for scale-out. The number of instances will not exceed this value. | Positive integer, and ≥ minReplicas |
| `scalingPolicy.spec.metrics[].type` | Metric type | `External` (provided by the External Metrics Adaptor) |
| `scalingPolicy.spec.metrics[].external.metric.name` | External metric name | Must be consistent with the metric name exposed by the Adaptor |
| `scalingPolicy.spec.metrics[].external.target.type` | Target value type | `AverageValue` (average value across Pods) |
| `scalingPolicy.spec.metrics[].external.target.averageValue` | Target average value threshold | Set according to the metric dimension |

> [!NOTE]NOTE
> `scalingPolicy.spec.metrics[].external.metric.name` must be set to the metric name exposed by the External Metrics Adaptor. The Adaptor may map or rename the original Prometheus metric names of MindIE Motor (such as `vllm:num_requests_waiting`). After deploying the Adaptor, you can run the following command to view the list of actually exposed metrics:
>
> ```bash
> kubectl get --raw /apis/external.metrics.k8s.io/v1beta1 | grep -E "vllm:|motor:"
> ```
>
> If the metric name exposed by the Adaptor differs from the examples in this document, use the actual returned value.
>
> This feature depends on MindCluster Infer Operator version ≥ 26.1.0. You need to manually add the `scalingPolicy` configuration in `infer_service_template.yaml` as shown in the preceding example.

## Scaling Recommendation Metrics

The MindIE Motor `/metrics` endpoint provides a rich set of engine-level metrics. The following table lists the key metrics recommended for autoscaling:

### Prefill Scaling Recommendation Metrics

| Metric Name | Type | Description | Recommended Threshold |
|--------|------|------|-------------|
| `vllm:num_requests_waiting` | Gauge | Number of requests waiting for scheduling. | > 5 triggers scale-out, < 2 triggers scale-in |
| `vllm:num_requests_running` | Gauge | Number of currently running requests | Depend on the NPU specifications and model. |
| `vllm:kv_cache_usage_perc` | Gauge | KV Cache usage (0-1) | > 0.8 triggers scale-out. |
| `motor:prompt_tokens_per_second` | Gauge | Prompt token processing rate (calculated by Motor) | Set according to the SLA target. |
| `vllm:time_to_first_token_seconds` | Histogram | Time to first token (TTFT) | Set according to the SLA target (e.g., p95 < 500 ms). |

### Decode Scaling Recommendation Metrics

| Metric Name | Type | Description | Recommended Threshold Recommendation |
|--------|------|------|-------------|
| `vllm:num_requests_waiting` | Gauge | Number of Requests Waiting for Scheduling | > 5 trigger scale-out. |
| `vllm:num_requests_running` | Gauge | Number of Currently Running Requests | Depend on the NPU specifications and model. |
| `motor:generation_tokens_per_second` | Gauge | Generation token rate (calculated by Motor) | Set according to the SLA target. |
| `vllm:e2e_request_latency_seconds` | Histogram | End-to-end request latency | Set according to the SLA target (e.g., p95 < 2s). |
| `vllm:time_per_output_token_seconds` | Histogram | Time per output token (TPOT) | Set according to the SLA target (e.g., p95 < 50ms). |

> [!NOTE]NOTE
>
>- `motor:prompt_tokens_per_second` and `motor:generation_tokens_per_second` are service-level metrics calculated by the MindIE Motor Coordinator. They are derived by computing the delta rate from the raw vLLM counters, which more accurately reflects real-time throughput.
>- Histogram-type metrics (such as `vllm:e2e_request_latency_seconds`) need to be exposed as independent metrics after the quantiles (p50/p95/p99) are calculated on the Adaptor side.
>- It is recommended to configure different scaling metrics for Prefill and Decode respectively to match their respective computational characteristics (Prefill is compute-intensive, while Decode is memory-access-intensive).

### Multi-Metric Combination Strategy

You can configure multiple metrics in the HPA, and the HPA selects the **most conservative scaling decision** (that is, the current replica count is closest to the metric for triggering scale-out or scale-in):

```yaml
scalingPolicy:
  type: HPA
  spec:
    minReplicas: 1
    maxReplicas: 4
    metrics:
    - type: External
      external:
        metric:
          name: vllm:num_requests_waiting
        target:
          type: AverageValue
          averageValue: "5"
    - type: External
      external:
        metric:
          name: vllm:kv_cache_usage_perc
        target:
          type: AverageValue
          averageValue: "0.8"
```

## Verifying the Scaling

### Viewing HPA Status

```bash
kubectl get hpa -n {namespace}
```

The following is an example of the output:

```text
NAME                          REFERENCE                    TARGETS          MINPODS   MAXPODS   REPLICAS   AGE
prefill-my-test               StatefulSet/prefill-my-test  3/5              1         4         2          10m
decode-my-test                StatefulSet/decode-my-test   8/10             1         4         2          10m
```

- The `TARGETS` column displays `current value/target value`. Scale-out is triggered when the current value exceeds the target value.

- The `REPLICAS` column displays the current actual number of replicas.

### Simulating Load to Trigger Scale-Out

Send a large number of concurrent inference requests and observe whether the HPA automatically scales out.

```bash
# Send requests concurrently
for i in {1..100}; do
  curl -X POST "http://{service-ip}:31015/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model": "your-model", "max_tokens": 100, "messages": [{"role": "user", "content": "Hello"}]}' &
done
```

Then check the HPA status to confirm whether `REPLICAS` has increased and whether the newly added engine Pods are running properly.

```bash
kubectl get hpa -n {namespace} --watch
kubectl get pod -n {namespace} | grep -E "prefill|decode"
```

### Verifying Scale-In

After stopping the load, observe for several minutes (5 minutes determined by the HPA `--horizontal-pod-autoscaler-downscale-stabilization` by default) and confirm that the number of instances falls back to `minReplicas`:

```bash
kubectl get hpa -n {namespace} --watch
```

## Notes

- HPA Elastic Scaling currently supports only Prefill and Decode instances; the Router does not participate in scaling.

- Scale-in has a stabilization window (5 minutes by default) to prevent frequent scaling caused by short-term load fluctuations.

- Newly scaled-out instances have no KV Cache. The Prefix Cache feature rebuilds the cache gradually, so the inference performance of new instances may degrade slightly and recover after a period of time.

- The External Metrics Adaptor must run continuously and be configured with the correct Coordinator address. If the Adaptor is abnormal, HPA cannot obtain metrics, which may cause scaling to fail.

- It is recommended that `minReplicas` be set to at least 1 to avoid scaling in to 0, which would make the service completely unavailable.

- Counter-type metrics (such as the total number of tokens) are not reset by `/metrics` requests. It is recommended to prioritize Gauge-type metrics or the TPS metric calculated by MindIE Motor as the basis for scaling.

- If the `/metrics` endpoint without the `type` parameter (default `full`) is used, HPA obtains globally aggregated values. To scale Prefill/Decode roles independently, the Adaptor must request `/metrics?type=role&role=prefill` and `/metrics?type=role&role=decode` separately.

## Reference Documents

- [Configuring Load-Based Elastic Scaling](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/04_usage/09_infer_operator_best_practice/05_configuring_elastic_scaling.md) — Guide to configuring the Infer Operator elastic scaling policy

- [Metrics Interface](../api/metrics_interfaces.md) — Usage instructions for the MindIE Motor `/metrics` endpoint
