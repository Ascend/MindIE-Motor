# Automatic Elastic Scaling Feature

## Feature Introduction

The automatic elastic scaling feature supports automatically adjusting the number of Prefill and Decode instances based on the real-time load of inference instances. It scales out automatically when the request volume rises and scales in when the load falls back, improving resource utilization while ensuring service SLA.

### Working Principle

Infer Operator creates Horizontal Pod Autoscaler (HPA) resources for inference instances. HPA obtains engine-level load metrics aggregated by MindIE Motor (such as the number of queued requests, TPS, and KV Cache usage) through the External Metrics Adaptor or Prometheus + Prometheus Adapter, and automatically adjusts the number of instance replicas according to the scaling thresholds configured by users.

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

2. The metric integration component converts the Coordinator metrics into the Kubernetes External Metrics API: the External Metrics Adaptor connects directly to the Coordinator; or Prometheus scrapes the metrics first and then the Prometheus Adapter registers them as External metrics.

3. The HPA obtains load data from the External Metrics API and compares it with the user-configured target thresholds.

4. When a metric continuously exceeds the threshold, the HPA notifies the Infer Operator to increase the number of replicas; when it falls below the threshold, the number of replicas is decreased.

### Constraints and Limitations

| Constraint Dimension | Requirement |
|----------|------|
| Hardware | <ul><li>Atlas 800I A2</li><li>Atlas 800I A3</li></ul> |
| Deployment scenario | Automatic scaling requires the `infer_service_set` deployment mode (the HPA is created by Infer Operator).<ul><li>PD disaggregation: configure `scalingPolicy` for the `prefill` and `decode` roles respectively;</li><li>PD aggregation: configure it for the `union` role, and the `metric` **must be explicitly specified** because this role has no default metric and configuration generation fails directly if it is missing;</li><li>`multi_deployment` and `single_container`: automatic scaling described in this document is not supported, and the number of instances can only be adjusted manually.</li></ul>The Controller and Coordinator are not within the scaling scope of this example. |
| Software dependency | Infer Operator ≥ 26.1.0, and role-level `scalingPolicy` must be supported; the metric integration can use either the External Metrics Adaptor or Prometheus + Prometheus Adapter. |
| Other restrictions | <ul><li>Scale-in has a stabilization window (5 minutes by default) to prevent frequent scaling caused by short-term load fluctuations;</li><li>Newly scaled-out instances have no KV Cache. The Prefix Cache feature rebuilds the cache gradually, so the inference performance of new instances may degrade slightly and recover after a period of time;</li><li>It is recommended that `minReplicas` be set to at least 1 to avoid scaling in to 0, which would make the service completely unavailable;</li><li>Counter-type metrics (such as the total number of tokens) are not reset by `/metrics` requests. It is recommended to prioritize Gauge-type metrics or the TPS metric calculated by MindIE Motor as the basis for scaling;</li><li>If the `/metrics` endpoint without the `type` parameter (default `full`) is used, HPA obtains globally aggregated values. To scale the Prefill/Decode roles independently, the External Metrics Adaptor must request the `type=role` view separately, and the Prometheus path must aggregate by role during scraping or in PromQL.</li></ul> |

## Feature Usage

### Environment Preparation

- Infer Operator has been installed and deployed. For the installation procedure, see [Infer Operator Installation Guide](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/en/scheduling/05_developer_guide/00_installation_deployment/00_manual_installation/07_infer_operator.md); for the elastic scaling policy configuration, see [Configuring Load-Based Elastic Scaling](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/en/scheduling/04_usage/10_infer_operator_best_practice/05_configuring_elastic_scaling.md).

- A PD disaggregation or PD aggregation deployment configuration in the `infer_service_set` mode has been prepared, and idle NPU resources required for adjusting instances are available. When enabling scaling for an existing service, keep the original namespace, model, mounts, and port configuration.

- A metric integration component has been deployed (choose one of the two options; both convert MindIE Motor metrics into Kubernetes External Metrics): [External Metrics Adaptor](https://gitcode.com/Ascend/mindcluster-deploy/tree/master/infer-operator-metrics-adaptor) (mindcluster-deploy provides an example that can be deployed directly), or Prometheus + Prometheus Adapter.

### Usage Example

1. Configure MindIE Motor to expose Metrics.

   The observation port of the MindIE Motor Coordinator exposes metrics through `/metrics` by default. The default port 1027 is used below. When accessing from outside the cluster, replace it with an actually reachable Service/NodePort address, and use HTTPS and certificates accordingly when TLS is enabled.

   The Coordinator /metrics endpoint provides multiple aggregation views, which can be switched using the `type` parameter:

   | type Value | Description | Applicable Scenarios |
   |---------|------|---------|
   | full (default) | Global aggregation, where metrics of all instances are aggregated into a single value | Prometheus scraping, HPA global scaling |
   | instance | Instance-level metrics, with `instance_id` and `role` labels injected | Single-instance troubleshooting |
   | role | Aggregation by role (Prefill / Decode) | Independent scaling by role |

   ```bash
   # View the globally aggregated metrics (default)
   curl http://{coordinator-ip}:1027/metrics

   # View metrics separately by role
   curl "http://{coordinator-ip}:1027/metrics?type=role&role=prefill"
   curl "http://{coordinator-ip}:1027/metrics?type=role&role=decode"
   ```

2. Deploy the metric integration (choose one of the two options).

   Both paths ultimately provide the Kubernetes External Metrics API to the HPA. After deployment, verify that the metrics are available as described in [step 3](#step-2-3), and then configure `scalingPolicy` as described in [step 4](#step-3).

   <a id="step-2-1"></a>

   - **External Metrics Adaptor**

      Link: `Coordinator → External Metrics Adaptor → External Metrics API`

      The External Metrics Adaptor periodically pulls Prometheus-format metrics from the Coordinator and registers them as External metrics. You can deploy it directly using the [adaptor example provided by mindcluster-deploy](https://gitcode.com/Ascend/mindcluster-deploy/tree/master/infer-operator-metrics-adaptor), or implement it on your own as needed.

      Key configuration points:

      - Configure the Coordinator metrics endpoint address and the scraping interval correctly.

      - When scaling the Prefill/Decode roles independently, the Adaptor must request `/metrics?type=role&role=prefill` and `/metrics?type=role&role=decode` separately (the HPA generated by Infer Operator carries role labels).

   <a id="step-2-2"></a>

   - **Prometheus + Prometheus Adapter**

      Link: `Coordinator → Prometheus → Prometheus Adapter → External Metrics API`

      1. Prometheus scrapes metrics from the `/metrics` endpoint of the Coordinator.

      2. The Prometheus Adapter queries Prometheus according to the `metricsQuery` in `externalRules`, and registers the results in `external.metrics.k8s.io`.

      3. The HPA consumes the metric through `type: External` and `external.metric.name`.

      `metricsQuery` is the place to combine multiple metrics or perform time-window aggregation; the Motor deployer only passes through the `scalingPolicy` of the HPA and does not participate in PromQL computation. The metric name exposed by the Adapter must be consistent with `external.metric.name` in [step 4](#step-3); if the HPA is configured with the `kubernetes_namespace` or `infer_huawei_com_inferservice_name` selector, the Prometheus time series and the Adapter rules must retain the corresponding labels.

3. <a id="step-2-3"></a>Use the following methods to verify whether the metrics are available.

   1. Query the External Metrics API with the metric name that the HPA will actually use. For how to select the metric name, see [Which External Metric Name to Use](#which-external-metric-name-to-use).

   2. Compare the query result with the value on the Coordinator `/metrics`; proceed to [step 4](#step-3) only after a non-empty and reasonable value is returned.

   3. External Metrics Adaptor path: the query must carry `labelSelector` (the InferService name and role labels); without the selector, the Adaptor cannot determine the InferService:

      ```bash
      kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/{namespace}/{metric-name}?labelSelector=infer.huawei.com%2Finferservice-name%3D{is-name}%2Cinfer.huawei.com%2Frole-name%3D{prefill|decode}"
      ```

   4. Prometheus Adapter path: query by the metric name registered by the Adapter and the HPA selector, for example:

      ```bash
      kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/{namespace}/{metric-name}"
      ```

4. <a id="step-3"></a>Configure an elastic scaling policy.

   The following example is based on the External Metrics Adaptor path: Prefill scales based on the number of queued requests, and Decode scales based on the generation token rate.

   In examples/deployer/yaml_template/infer_service_template.yaml, add the `scalingPolicy` field to the role configuration block (use `prefill`/`decode` for PD disaggregation and `union` for PD aggregation).

   When generating YAML, the deployer updates `kubernetes_namespace` to `motor_deploy_config.job_id` and `infer_huawei_com_inferservice_name` to `<InferServiceSet name>-0`.

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
                 # External metric name: enter a name whose value can be obtained by the metric integration component (in this example, the Adaptor uses the original name directly)
                 name: "vllm:num_requests_waiting"
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
                 # External metric name: enter a name whose value can be obtained by the metric integration component (in this example, the Adaptor uses the original name directly)
                 name: "motor:generation_tokens_per_second"
               target:
                 type: AverageValue
                 averageValue: "10"
       metadata:
         labels:
           infer.huawei.com/gang-schedule: 'true'
       spec:
         # ... Keep the remaining configuration unchanged ...
   ```

   >[!NOTE]NOTE
   >The Prometheus Adapter path requires adding `selector.matchLabels` under `external.metric` (see [Prometheus + Prometheus Adapter](#step-2-2)). Example:
   >
   >```yaml
   >metrics:
   >- type: External
   >  external:
   >    metric:
   >      name: motor_prefill_utilization
   >      selector:
   >        matchLabels:
   >          infer_huawei_com_inferservice_name: vllm-0
   >          kubernetes_namespace: mindie-motor
   >    target:
   >      type: Value
   >      value: "0.8"
   >```

   scalingPolicy parameter description:

   | Parameter | Description | Value |
   |------|------|------|
   | scalingPolicy.type | Type of the elastic scaling policy | Currently only HPA is supported |
   | scalingPolicy.spec.minReplicas | Lower limit for scale-in. The number of instances will not fall below this value. | Positive integer |
   | scalingPolicy.spec.maxReplicas | Upper limit for scale-out. The number of instances will not exceed this value. | Positive integer, and ≥ `minReplicas` |
   | scalingPolicy.spec.metrics[].type | Metric type | External (provided by the External Metrics Adaptor or Prometheus Adapter) |
   | scalingPolicy.spec.metrics[].external.metric.name | External metric name | Must be consistent with the metric name exposed by the selected metric integration component; for the verification method, see [step 3](#step-2-3) |
   | scalingPolicy.spec.metrics[].external.metric.selector | Metric selector | Required only in the Prometheus Adapter scenario; no additional setting is needed in the External Metrics Adaptor scenario |
   | scalingPolicy.spec.metrics[].external.metric.selector.matchLabels.kubernetes_namespace | Namespace to which the metric belongs | Configured only in the Prometheus Adapter scenario; if the template contains this label, the YAML generated by the deployer updates it to `motor_deploy_config.job_id` |
   | scalingPolicy.spec.metrics[].external.metric.selector.matchLabels.infer_huawei_com_inferservice_name | InferService to which the metric belongs | Configured only in the Prometheus Adapter scenario; if the template contains this label, the deployer updates it to `<InferServiceSet name>-0` automatically |
   | scalingPolicy.spec.metrics[].external.target.type | Target value type | Value / AverageValue. Use AverageValue for the total number of requests and the total TPS; use Value for the already averaged KV usage and capacity utilization to avoid dividing by the number of replicas again |
   | scalingPolicy.spec.metrics[].external.target.value / averageValue | Target threshold | Use value for Value and averageValue for AverageValue; set it according to the dimension of the final output of the metric integration component |

   The HPA is managed by Infer Operator: an HPA modified directly with `kubectl patch` is rolled back by the Operator, so it must be modified through `scalingPolicy` in the template or `scaling_policy` in `user_config.json`.

   - Typical configuration example of the elastic scaling policy:

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
                    name: motor_prefill_utilization
                    selector:
                      matchLabels:
                        infer_huawei_com_inferservice_name: vllm-0
                        kubernetes_namespace: mindie-motor
                  target:
                    type: Value
                    value: "0.8"
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
                    name: motor_decode_utilization
                    selector:
                      matchLabels:
                        infer_huawei_com_inferservice_name: vllm-0
                        kubernetes_namespace: mindie-motor
                  target:
                    type: Value
                    value: "0.8"
          metadata:
            labels:
              infer.huawei.com/gang-schedule: 'true'
          spec:
            # ... Keep the remaining configuration unchanged ...
      ```

5. Generate and apply the scaling configuration.

   <a id="step-4"></a>

   Modifying a local template does not update deployed resources. Generate the YAML first with the same configuration as the original deployment, and check the namespace, roles, replica limits, metric names, and selectors in the output:

   ```bash
   cd examples/deployer
   python deploy.py --config_dir ../../model_configs/vllm --dry-run
   ```

   The generated file is located at `output_yamls/infer_service.yaml`. After confirming that it is correct, apply the configuration through the deployment entry in the planned change window:

   ```bash
   python deploy.py --config_dir ../../model_configs/vllm
   ```

   The second command above changes cluster resources. `--update_instance_num` is only used to manually adjust the number of instances and does not re-read the template, so it cannot replace the generation step for the policy added in this section. After HPA is enabled, the role replicas are managed by HPA; avoid having another automation program continuously writing the replicas of the same role at the same time.

### Verification

1. Check the HPA status.

   ```bash
   kubectl get hpa -n {namespace}
   ```

   The following is an example of the output format. The resource names and values are subject to the actual results in your cluster:

   ```text
   NAME                     REFERENCE                    TARGETS          MINPODS   MAXPODS   REPLICAS   AGE
   vllm-0-prefill-scaler    InstanceSet/vllm-0-prefill   116m/800m        1         4         1          34m
   vllm-0-decode-scaler     InstanceSet/vllm-0-decode    897m/800m        1         4         2          34m
   ```

   - The REFERENCE column points to the role workload generated by Infer Operator. The `workload` written for the role in the template is not necessarily the same as the actual resource type, so the actual output of your cluster prevails.

   - The TARGETS column displays the current value/target value in Kubernetes milli-units (`800m` means 0.8). Actual scaling is also subject to tolerance, the stabilization window, replica limits, and resource supply. If `<unknown>` appears, troubleshoot the metric link and the Infer Operator version first.

   - The REPLICAS column displays the current actual number of replicas.

2. Simulate load to trigger scale-out.

   Send a large number of concurrent inference requests and observe whether the HPA automatically scales out:

   ```bash
   # Send requests concurrently
   for i in {1..100}; do
     curl -X POST "http://{service-ip}:31015/v1/chat/completions" \
       -H "Content-Type: application/json" \
       -d '{"model": "your-model", "max_tokens": 100, "messages": [{"role": "user", "content": "Hello"}]}' &
   done
   ```

   A single short request burst may be shorter than the scraping/HPA synchronization period; during verification, maintain the load long enough and confirm that the actual metrics exceed the target. Then check the HPA status and the new engine Pods (the continuous watch and the Pod query can run in two separate terminals):

   ```bash
   kubectl get hpa -n {namespace} --watch
   kubectl get pod -n {namespace} | grep -E "prefill|decode"
   ```

3. Verify scale-in.

   After stopping the load, observe the metrics and the number of replicas falling back. The default stabilization window for scale-in is 5 minutes; if the policy overrides the window, the actual configuration prevails. The number of replicas can gradually return to `minReplicas` only when the recommendations of all valid metrics allow scale-in; if obtaining the value of a metric fails or metrics such as KV remain high, returning to the lower limit is not guaranteed:

   ```bash
   kubectl get hpa -n {namespace} --watch
   ```

## Tuning Suggestions

The MindIE Motor /metrics endpoint provides a rich set of engine-level metrics. The following tables list the key metrics recommended for automatic scaling:

### Recommended Metrics for Prefill Scaling

| Metric Name | Type | Description | Recommended Threshold |
|--------|------|------|-------------|
| vllm:num_requests_waiting | Gauge | Number of requests waiting for scheduling. | A single target value determines both directions; scale-out and scale-in thresholds are not set separately. In the example, target=5 is used on a per-Pod average basis: above the target triggers scale-out and below the target triggers scale-in; deviations within the default 10% HPA tolerance do not trigger any action |
| vllm:num_requests_running | Gauge | Number of currently running requests | Depend on the NPU specifications and model. |
| vllm:kv_cache_usage_perc | Gauge | KV Cache usage (0-1) | In the example, target=0.8: above the target triggers scale-out and below the target triggers scale-in |
| motor:prompt_tokens_per_second | Gauge | Prompt token processing rate (calculated by Motor) | Set according to the SLA target. |
| vllm:time_to_first_token_seconds | Histogram | Time to first token (TTFT) | Set according to the SLA target (e.g., p95 < 500ms). |

### Recommended Metrics for Decode Scaling

| Metric Name | Type | Description | Recommended Threshold |
|--------|------|------|-------------|
| vllm:num_requests_waiting | Gauge | Number of requests waiting for scheduling. | Same as Prefill: a single target value determines both directions; in the example, target=5 on a per-Pod average basis |
| vllm:num_requests_running | Gauge | Number of currently running requests | Depend on the NPU specifications and model. |
| motor:generation_tokens_per_second | Gauge | Generation token rate (calculated by Motor) | Set according to the SLA target. |
| vllm:e2e_request_latency_seconds | Histogram | End-to-end request latency | Set according to the SLA target (e.g., p95 < 2s). |
| vllm:time_per_output_token_seconds | Histogram | Time per output token (TPOT) | Set according to the SLA target (e.g., p95 < 50ms). |

> [!NOTE]NOTE
>
>- `motor:prompt_tokens_per_second` and `motor:generation_tokens_per_second` are service-level metrics calculated by the MindIE Motor Coordinator. They are derived by computing the delta rate from the raw vLLM counters, which more accurately reflects real-time throughput.
>- Histogram-type metrics (such as `vllm:e2e_request_latency_seconds`) need to be exposed as independent metrics after the quantiles (p50/p95/p99) are calculated on the metric integration component side.
>- It is recommended to configure different scaling metrics for Prefill and Decode respectively to match their respective computational characteristics (Prefill is compute-intensive, while Decode is memory-access-intensive).
>- Before using the metrics in the tables, confirm that they are actually discriminative under the current model, `max_model_len`, and load: when no requests are queued, `vllm:num_requests_waiting` stays at 0 for a long time, and with short sequences `vllm:kv_cache_usage_perc` is also close to 0. Such metrics cannot drive scaling, so use [Capacity Planning Utilization](#capacity-planning-utilization-recommended) instead.

### Multi-Metric Combination Strategy

You can configure multiple metrics in the HPA. The HPA calculates the desired number of replicas for each metric separately and uses the maximum value: for example, if the metrics suggest 2 and 6 replicas respectively, 6 is used, and the result is then subject to constraints such as the limits. If obtaining a metric fails and the other metrics only suggest scale-in, the HPA skips scale-in; if valid metrics suggest scale-out, scale-out can still be performed. For details, see [Kubernetes HPA algorithm](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/).

In the following example, the KV usage is aggregated into an average by the Coordinator, and the metric integration component must preserve this semantics, so `Value` is used:

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
          name: "vllm:num_requests_waiting"
        target:
          type: AverageValue
          averageValue: "5"
    - type: External
      external:
        metric:
          name: "vllm:kv_cache_usage_perc"
        target:
          type: Value
          value: "0.8"
```

### Capacity Planning Utilization (Recommended)

Motor also provides `motor_prefill_utilization` and `motor_decode_utilization`, which respectively represent the demand/supply ratio of each role. The formula is `demand_tps / (number of active instances × per-instance capacity capacity_tps)`, and both the original colon name and the alias without a colon are output. They are already normalized by the current number of instances, so the HPA should use `Value`, and the initial target can be set to `0.8`. For the related capacity prior, calibration status, and calculation method, see [Metrics Interfaces](../api/metrics_interfaces.md). When calibration has not been performed, the utilization is not output (it is also not output when the capacity is 0 or there is no active instance), so do not treat "metric missing" as zero load; conversely, **a non-zero metric does not mean there is current load**: the demand side is an EMA and does not decay to 0 when idle (in actual tests, the value can remain at the same non-zero value for a long time when idle), and when the capacity estimate is too small in the early calibration stage, the utilization is inflated, which may trigger a scale-out even without load, and the capacity is then raised and scaled back according to the stabilization window. You can first use `motor:capacity_calibrated{role="prefill"}` and `motor:capacity_calibrated{role="decode"}` to confirm whether the corresponding role has been calibrated (1 means calibrated); this metric is output only in the default `full` view and is not available in the `type=role` view requested by role, and it appears in pairs by the role label, so it is also missing when the role has never appeared.

In addition to modifying the YAML template, you can also add the following configuration at the top level of `user_config.json` and let the deployer generate the policy; this configuration for the same role overrides the scalingPolicy in the template, so choose only one way to maintain it:

```json
{
  "scaling_policy": {
    "prefill": {"min_replicas": 1, "max_replicas": 4, "target": 0.8},
    "decode": {"min_replicas": 1, "max_replicas": 4, "target": 0.8}
  }
}
```

The default metrics are the P/D utilizations described above, and the default type is `Value`. The generator also sets the `kubernetes_namespace` and `infer_huawei_com_inferservice_name` selectors, and the Adaptor/Prometheus time series and query rules must provide the corresponding labels with values matching the generated result; otherwise, even if the metrics are registered successfully, the HPA may not be able to query the values. When only the template is modified, the generator only fills in these two labels if they are already configured in the template, and does not automatically add missing labels.

## FAQs

### Template Modified but No HPA Is Created

**Symptom**

`infer_service_template.yaml` or `user_config.json` has been modified and deploy has been executed, but no HPA resource appears in the cluster.

**Cause Analysis**

1. The deployment mode is `multi_deployment` or `single_container`, which does not support HPA automatic scaling.

2. Only the local template was modified, and the YAML was not generated and applied as described in [step 4](#step-4).

3. The Infer Operator version is too old, or `scalingPolicy` was not created correctly.

4. The HPA was modified directly with `kubectl patch` and then rolled back by the Operator.

**Solution**

1. Confirm that the service runs in the `infer_service_set` mode; for unsupported modes, see [Manual Scaling](../features/manual_scaling.md).

2. Generate and apply the configuration as described in [step 4](#step-4), and check whether each role in `output_yamls/infer_service.yaml` contains `scalingPolicy`.

3. Check the actual resources of the InferServiceSet and the Infer Operator events and logs.

### Which External Metric Name to Use

**Symptom**

You are not sure whether `scalingPolicy.spec.metrics[].external.metric.name` should be set to the original name on the Coordinator `/metrics` or the external metric name exposed by the Adapter.

**Cause Analysis**

1. The External Metrics Adaptor and the Prometheus Adapter may map or rename the original Prometheus names.

2. Motor contract metrics have aliases without a colon (such as `motor_prefill_utilization`), while non-contract metrics usually only have original series with a colon.

3. The Adaptor example of mindcluster-deploy looks up names literally and does not map colons to underscores; an incorrect name may silently return 0.

**Solution**

1. After deployment, query the External Metrics API as described in [step 2.3](#step-2-3), and fill in `external.metric.name` according to the actual returned value.

2. For the set of contract metrics, see [Metrics Interfaces](../api/metrics_interfaces.md); when the mindcluster-deploy Adaptor example is used, non-contract metrics must be filled in with the original names containing a colon (such as `vllm:num_requests_waiting`).

3. When scaling by role, the External Metrics Adaptor uses the `type=role` view; the Prometheus path must aggregate by role during scraping or in PromQL, and the HPA must limit the scope to the current service with a selector.

### TARGETS Shows unknown or FailedGetExternalMetric Is Reported

**Symptom**

The TARGETS column of the HPA displays `<unknown>`, or `FailedGetExternalMetric` appears in events.

**Cause Analysis**

1. The External Metrics API is unavailable, or the APIService is not registered.

2. The `labelSelector` is missing in the External Metrics Adaptor path, and the query fails directly.

3. The scraping, `externalRules`, and HPA `selector.matchLabels` of the Prometheus Adapter path are inconsistent.

4. The external metric name does not match the mapping rules of the integration component (the mindcluster-deploy Adaptor silently returns 0 for incorrect names).

5. Calibration of capacity metrics has not been completed.

**Solution**

1. Confirm that the metrics API is available, and query the specific value by the metric name and selector used by the HPA:

   ```bash
   kubectl get apiservice v1beta1.external.metrics.k8s.io
   kubectl describe hpa "<hpa-name>" -n "<namespace>"
   kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/<namespace>/<metric-name>?labelSelector=infer.huawei.com%2Finferservice-name%3D<is-name>%2Cinfer.huawei.com%2Frole-name%3D<role>"
   ```

2. Confirm that the returned items are not empty, the timestamp is continuously updated, and the labels match the HPA selector.

3. If the value is always 0, check the metric name mapping, service labels, role filtering, and scraping endpoint; for capacity metrics, check the calibration status.

### Load Exists but No Scale-Out Occurs

**Symptom**

The service load has increased, but the HPA does not increase the number of replicas.

**Cause Analysis**

1. The current metric does not exceed the target value and the default 10% HPA tolerance.

2. The load duration is shorter than the metric scraping and HPA synchronization period.

3. The HPA has reached `maxReplicas`.

4. The HPA has increased the desired number of replicas, but the new Pods are Pending due to reasons such as insufficient NPU resources.

**Solution**

1. Maintain the load long enough and confirm that the actual metric exceeds the target; check the External Metrics value against [step 2.3](#step-2-3).

2. Check the TARGETS and REPLICAS columns of the HPA to distinguish "no decision made" from "decision made but Pods Pending".

3. When Pods are Pending, check the NPU resources, node selection, and scheduling events.

### No Scale-In to the Lower Limit After Requests Stop

**Symptom**

After requests stop being sent, the number of instances does not fall back to `minReplicas` for a long time.

**Cause Analysis**

1. The scale-in stabilization window (5 minutes by default) has not ended.

2. In a multi-metric policy, some metric still suggests maintaining a relatively high number of replicas.

3. Obtaining a metric fails, and the HPA skips scale-in.

4. Metrics such as KV Cache usage remain high for a long time due to cache retention, which is not equivalent to "no active requests".

**Solution**

1. Observe for a long enough period and confirm that the stabilization window has passed.

2. Check the TARGETS of each HPA metric and the events, and troubleshoot metric retrieval failures.

3. Select metrics according to the capacity target you want to control, and avoid equating "no requests" with "all metrics are low".

### Multiple Motor Services Read the Same Metrics

**Symptom**

The HPAs of multiple Motor services read the same or mixed metric values, and their scaling behaviors interfere with each other.

**Cause Analysis**

1. The query scope of the metric integration component is too broad and includes metrics of other services or of the opposite role.

2. The HPA has not configured, or has not correctly matched, the `kubernetes_namespace` and `infer_huawei_com_inferservice_name` selectors.

3. The globally aggregated `/metrics` view is used without isolation by service/role.

**Solution**

1. Check the query scope of the metric integration component and the HPA selector.

2. Retain the namespace and InferService labels for each service, and check the actual label values in the generated YAML.

3. When scaling by role, use the `type=role` view or an equivalent PromQL aggregation to prevent global aggregation from mixing in other services.
