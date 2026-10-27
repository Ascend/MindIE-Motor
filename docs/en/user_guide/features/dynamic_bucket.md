# Dynamic Bucket Scheduling

## Feature Introduction

**Dynamic bucket scheduling is a Decode-oriented scheduling policy that dynamically selects a short bucket or a long bucket according to the real number of input tokens of a request and the load within the buckets, so as to reduce TPOT and improve output throughput in mixed long/short request scenarios.**

Dynamic bucket scheduling (`dynamic_bucket`) stably divides Decode endpoints into a short-sequence bucket and a long-sequence bucket, then selects the target bucket based on both the number of input tokens of the request and the real-time load of the two buckets, and finally selects the endpoint with the lowest load within the bucket. This policy applies to scenarios with mixed long and short requests where different sequence lengths have an obvious impact on TPOT (Attention duration grows linearly with sequence length). Its goal is to retain the benefit of long/short sequence isolation while allowing requests to be dynamically migrated to the other bucket when the load difference is large, avoiding hotspots caused by fixed bucketing. The algorithm is designed to reduce TPOT and improve Decode output throughput.

The policy does not depend on the KV Conductor:

- Only the Decode role supports dynamic bucket selection, and cross-node Decode nodes are supported.

- The Prefill, Encode, and Union roles are controlled by `prefill_scheduler_type` and cannot be configured with `dynamic_bucket`.

- When the valid token count of a request cannot be computed, the policy falls back to `load_balance`; when load balancing cannot select an endpoint either, it falls back to round robin.

### How It Works

A single Decode scheduling decision executes the following steps in order:

1. Obtain the request length. The token IDs already present on the request are reused first; otherwise the model tokenizer processes `messages` or `prompt`, and the result is cached on the request.

2. Sort the endpoints by `(instance_id, endpoint_id)` and stably divide them into the short bucket and the long bucket according to the configuration.

3. Compute the average `active_tokens` of the endpoints in the short bucket and the long bucket separately.

4. Use `dynamic_bucket_border` to obtain the initial bucket according to the request length, and then decide whether to switch to the other bucket according to the length factor and the load factor.

5. Select the endpoint with the smallest `active_tokens` in the target bucket; if the target bucket is empty, try the other bucket.

6. The Worker passes all candidate endpoints in the target bucket to the CAS allocation path. When the workload view changes, the CAS slow path re-selects within the same bucket using the latest load, avoiding hotspots caused by a stale snapshot.

After a successful allocation, the number of input tokens of the request is used as the `active_tokens` increment of the request; the actually committed workload is released when the request ends. `get_request_token_length` reuses the existing token IDs first and otherwise calls the tokenizer to obtain the token count, for use in bucket selection and accounting. Dynamic bucket scheduling measures Decode load by the number of input tokens, which differs from the existing `load_balance` accounting by request body length `req_info.req_len`.

#### Endpoint Bucket Rules

Let the total number of available endpoints be `N` and the number of short-bucket endpoints be `N_short`:

- When both `dynamic_bucket_short_bucket_count` and `dynamic_bucket_long_bucket_count` are `0`, `N_short = max(1, floor(N / 2))`.

- When both are greater than `0`, the short bucket count is computed from their ratio:

  ```text
  N_short = round(N × dynamic_bucket_short_bucket_count / (dynamic_bucket_short_bucket_count + dynamic_bucket_long_bucket_count))
  ```

- When only `dynamic_bucket_short_bucket_count` is configured, it is used as the expected number of short-bucket endpoints.

- When only `dynamic_bucket_long_bucket_count` is configured, `N - dynamic_bucket_long_bucket_count` is used as the expected number of short-bucket endpoints.

- When `N >= 2`, the final result is limited to `[1, N - 1]`, ensuring that both buckets are non-empty; when there is only one endpoint, it is entirely assigned to the short bucket.

Sorting and bucketing do not depend on the request, so the same endpoint set always produces a stable bucketing result.

#### Target Bucket Algorithm

##### Basic Definitions

Define the decreasing Sigmoid function:

```text
sigmoid_down(x) = 1 / (1 + exp(x))
```

Let:

- `L`: number of input tokens of the request.

- `B`: `dynamic_bucket_border`.

- `M_short`: `dynamic_bucket_short_median`.

- `M_long`: `dynamic_bucket_long_median`.

- `Q_short`, `Q_long`: average `active_tokens` of the endpoints in the short bucket and the long bucket.

- `alpha`: `dynamic_bucket_length_scale`.

- `beta`: `dynamic_bucket_load_scale`.

When `L <= B`, the short bucket is initially selected, and the following values are computed:

```text
distance        = L
bucket_span     = M_short
length_factor   = sigmoid_down(alpha / M_short × (distance - M_short))
load_ratio      = Q_long / Q_short
load_factor     = sigmoid_down(beta × (load_ratio - 1 / beta))
```

When `L > B`, the long bucket is initially selected, and the following values are computed:

```text
distance        = L - B
bucket_span     = M_long - B
length_factor   = sigmoid_down(alpha / (M_long - B) × (distance - (M_long - B)))
load_ratio      = Q_short / Q_long
load_factor     = sigmoid_down(beta × (load_ratio - 1))
```

If the load of the initially selected bucket in the denominator is `0`, the implementation treats `load_ratio` as positive infinity, making the load factor approach `0`, so that the request is preferentially kept in the currently idle bucket.

The final decision is:

```text
length_factor + load_factor > 1  →  switch to the other bucket
length_factor + load_factor <= 1 →  keep the initial bucket
```

The length factor expresses how flexible the request is with respect to migrating to the other bucket: when the request length is below the corresponding characteristic length, the factor is greater than `0.5`, and when it is above the characteristic length, the factor is less than `0.5`. Therefore, shorter requests are more likely to be migrated when the initially selected bucket is overloaded, while requests exceeding the characteristic length of the long bucket tend to stay in the long bucket. The load factor expresses how congested the initially selected bucket is relative to the other bucket. Together they determine whether the initial long/short classification given by `B` is overridden.

### Usage Scenarios

**Applicable to Decode workloads with mixed long and short requests where sequence length has an obvious impact on TPOT. The benefit has been verified on Atlas 800I A3s with Qwen3.5-397B-A17B-W8A8 and CPU offload enabled for Decode under long-tail mixed workloads; other models, workloads, and configurations should be confirmed through service-level stress tests.**

| Scenario | Applicable Condition |
|------|----------|
| Mixed long and short requests | Decode workloads with mixed long and short requests, where the distribution of request input lengths spans a wide range |
| Obvious TPOT impact from sequence length | Different sequence lengths have an obvious impact on TPOT, and Attention duration grows linearly with sequence length |
| Models with expected benefit | Qwen3-MoE, Qwen3.5-Qwen3.8 |
| Models verified to have no benefit | DeepSeek-V4, GLM-5.1 (compressed KV and Lightning Indexer reduce the Attention computation amount and reduce the I/O volume of reading KV Cache in the Attention phase) |

### Constraints and Limitations

| Dimension | Constraint Description |
|------|----------|
| Deployment scenario | Only the Decode role supports dynamic bucket selection, and cross-node Decode nodes are supported; the Prefill, Encode, and Union roles are controlled by `prefill_scheduler_type` and cannot be configured with `dynamic_bucket`. |
| Feature mutual exclusion | Decode selects one scheduling policy through `decode_scheduler_type`; Prefill can be configured independently. Dynamic bucket scheduling itself does not depend on the KV Conductor, and the dependencies of other policies are enabled according to their configuration requirements. |
| Hardware | The verified environment is an Atlas 800I A3; the benefit on other hardware environments must be confirmed through stress tests of the target service. |
| Engine | Dynamic bucket scheduling is enabled only for the Decode role; circuit breaker, engine type, and routing capability filtering occur before bucketing. |
| Software dependency | The Coordinator must be able to access the model directory and load a tokenizer consistent with the engine, and all nodes that may run the Coordinator must mount this directory; `kv-events-config` does not need to be enabled and the KV Conductor does not need to be deployed. |
| Solution constraints | Bucket selection and accounting use the real number of input tokens of the request; at least two available endpoints are required to guarantee that both the short and long buckets are non-empty, and when there is only one endpoint it is entirely assigned to the short bucket. The actual benefit must be verified against the request length distribution, concurrency, and bucketing parameters. |
| Other limitations | <ul><li>When valid token IDs cannot be reused, and the tokenizer is not loaded, the model directory is inaccessible, or tokenization fails, the policy falls back to `load_balance`.</li><li>When the target bucket has no available endpoint, the other bucket is tried.</li><li>When neither bucket has an available endpoint, the policy falls back to `load_balance` and then round robin.</li><li>Circuit breaker, engine type, and routing capability filtering occur before bucketing.</li><li>Bucketing is performed again after the number of endpoints changes.</li><li>When `N >= 2`, the short bucket count is limited to `[1, N - 1]`; when there is only one endpoint, it is entirely assigned to the short bucket.</li></ul> |

## Using the Feature

### Usage Example

1. In `user_config.json`, set `motor_coordinator_config.scheduler_config.decode_scheduler_type` to `dynamic_bucket` to enable dynamic bucket scheduling, and configure the following bucketing parameters.

   Adjust the sibling `dynamic_bucket_*` parameters as needed; the length and load parameters can use the default values first, and the bucket counts are set according to the cluster capacity.
   Set `motor_engine_prefill_config.engine_config.model` to the actual model directory, and keep the remaining deployment configurations unchanged.

   The following example divides the short bucket and the long bucket at a ratio of `2:6`; when there are 8 endpoints in total, the short bucket and the long bucket contain 2 and 6 endpoints respectively:

   ```json
   {
     "motor_coordinator_config": {
       "scheduler_config": {
         "prefill_scheduler_type": "load_balance",
         "decode_scheduler_type": "dynamic_bucket",
         "dynamic_bucket_short_median": 16384,
         "dynamic_bucket_long_median": 98304,
         "dynamic_bucket_border": 32768,
         "dynamic_bucket_length_scale": 1.0,
         "dynamic_bucket_load_scale": 4.0,
         "dynamic_bucket_short_bucket_count": 2,
         "dynamic_bucket_long_bucket_count": 6
       }
     },
     "motor_engine_prefill_config": {
       "engine_config": {
         "model": "/mnt/weight/your-model"
       }
     }
   }
   ```

    All parameters reside in motor_coordinator_config.scheduler_config and are validated when the configuration is loaded. Lengths and bucket counts must be integers,
    and boolean values are not accepted; scaling factors do not accept boolean values, NaN, or infinity.

    | Parameter | Type | Default Value | Mandatory | Value Range | Description |
    |------|------|--------|------|----------|------|
    | decode_scheduler_type | string | `load_balance` | No | load_balance / round_robin / kv_cache_affinity / dynamic_bucket | Must be explicitly set to `dynamic_bucket` to enable this feature. `prefill_scheduler_type` does not support this value; when Decode is configured with `kv_cache_affinity`, `load_balance` is actually used. |
    | dynamic_bucket_short_median | int | `16384` | No | > 0 | Characteristic length of the short bucket (token), which is the Sigmoid midpoint of the short bucket length factor. It is not a median obtained from runtime statistics. |
    | dynamic_bucket_long_median | int | `98304` | No | > dynamic_bucket_border | Characteristic length of the long bucket (token). `long_median - border` is the normalized span of the long bucket length factor. |
    | dynamic_bucket_border | int | `32768` | No | > 0 and < dynamic_bucket_long_median | Initial long/short classification boundary (token). `L <= border` initially selects the short bucket; otherwise the long bucket is initially selected. |
    | dynamic_bucket_length_scale | float | `1.0` | No | Finite value > 0 | Length factor scaling coefficient. A larger value makes the Sigmoid change steeper near the characteristic length, and it can be adjusted through service-level stress tests. |
    | dynamic_bucket_load_scale | float | `4.0` | No | Finite value > 0 | Load factor scaling coefficient. This value also makes the midpoint of the load ratio on the short-bucket initial selection path equal to `1 / load_scale`; it is not an ordinary linear weight. |
    | dynamic_bucket_short_bucket_count | int | `0` | No | >= 0 | Number of short-bucket endpoints, or the numerator of the ratio. For details, see [Endpoint Bucket Rules](#endpoint-bucket-rules). |
    | dynamic_bucket_long_bucket_count | int | `0` | No | >= 0 | Number of long-bucket endpoints, or the numerator of the ratio. When both it and the short bucket count are positive, the two represent the bucketing ratio. |

2. Deploy the service.

   Deployment requirements:

   - The Coordinator must be able to access the model directory pointed to by `engine_config.model` in order to load a tokenizer consistent with the engine.

   - The model directory should be mounted to the Coordinator during deployment, and all nodes that may run the Coordinator must be able to read this directory.

   - `kv-events-config` does not need to be enabled and the KV Conductor does not need to be deployed.

   K8s deployment example:

   ```bash
   cd examples/deployer

   # PD disaggregation
   python deploy.py --config_dir ../infer_engines/vllm
   ```

### Verifying the Feature

**You can check the request length, target bucket, selected endpoint, and candidate loads in the dynamic bucket selection log output by the Coordinator; when a request cannot obtain a valid token count, the policy falls back to load balancing.**

Set `motor_coordinator_config.logging_config.log_level` to `DEBUG` and make the configuration take effect. The dynamic bucket selection log is at the DEBUG level and is not output at the default INFO level.

After deployment is complete, send inference requests to the service. The `<ip>`, `<port>`, and `model` values in the following example must be replaced with the actual service address, port, and model name:

```bash
curl -X POST "http://<ip>:<port>/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "who are you?"}
    ],
    "max_tokens": 100,
    "stream": true
  }'
```

Verify whether dynamic bucket scheduling takes effect by checking the Coordinator logs:

```bash
kubectl logs <coordinator-pod> | grep "Dynamic bucket selected"
```

Expected output:

```text
Dynamic bucket selected req_length=... target_bucket=... selected=...-... loads=...
```

## Tuning Suggestions

It is recommended to first use the default length and load parameters and set only the bucket counts according to the cluster capacity; after obtaining the real request length distribution and bucket load data, adjust
`short_median`, `long_median`, `border`, `length_scale`, and `load_scale`.

The following results are the medians of valid rounds of two configuration groups in the Atlas 800I A3 environment, and represent tuning conclusions only for this model, data distribution, and deployment configuration; they are not general default values for other models or hardware environments.

### Experimental Model and Service Configuration

- Model: `Qwen3.5-397B-A17B-W8A8`.

- Deployment: 1P1D, with Decode using DP16 / TP1, 16 endpoints in total, and CPU offload enabled.

- Stress test: both groups use 1000 long-tail mixed requests, concurrency 384, and a request rate of 400 req/s; within each group, the same dataset and matching runtime configuration are used to compare DB and LB.

| Parameter | Configuration A | Configuration B |
|---|---:|---:|
| burstiness | 1.0 | 0 |
| Short bucket / long bucket configuration ratio | 12:4 | 11:5 |
| Bucketing boundary (token) | 65,536 | 65,536 |
| Representative length of short requests (token) | 32,768 | 32,768 |
| Representative length of long requests (token) | 196,608 | 98,304 |
| length_scale | 1 | 1 |
| load_scale | 3.5 | 2.5 |
| Valid DB / LB rounds | 3 / 3 | 2 / 2 |

The short bucket / long bucket configuration ratio corresponds to `dynamic_bucket_short_bucket_count` and `dynamic_bucket_long_bucket_count`.
When both are positive, they represent a ratio, and the actual number of endpoints is converted from the total number of available Decode endpoints according to the bucketing rules above.
There are 16 Decode endpoints in total here, so Configuration A is actually divided into 12 short-bucket endpoints and 4 long-bucket endpoints, and Configuration B into 11 short-bucket endpoints and 5 long-bucket endpoints.

In this workload, 91% of the inputs are less than 16K tokens, 3% reach or exceed 64K tokens, and 2% reach or exceed 100,000 tokens.
The maximum input length counted in the two experimental groups is 134,691 tokens, and the target output length ranges from 256 to 4,096 tokens.
The actual total output of each round is 962,527 tokens, ensuring that the same output workload is compared.

### Two Test Configurations

The table above lists the bucketing parameters of Configuration A and Configuration B. The following is an example of the scheduling configuration of Configuration B, whose stress test burstiness is 0;
when using Configuration A, change the long bucket characteristic length to 196608, the load coefficient to 3.5, the short and long bucket configuration to 12 and 4, and set the stress test burstiness to 1.0.

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "prefill_scheduler_type": "load_balance",
      "decode_scheduler_type": "dynamic_bucket",
      "dynamic_bucket_short_median": 32768,
      "dynamic_bucket_long_median": 98304,
      "dynamic_bucket_border": 65536,
      "dynamic_bucket_length_scale": 1.0,
      "dynamic_bucket_load_scale": 2.5,
      "dynamic_bucket_short_bucket_count": 11,
      "dynamic_bucket_long_bucket_count": 5
    }
  }
}
```

### Median of the Two Test Groups

All metrics in the following table are the medians of valid rounds. TPOT P90 is the median of the P90 values of each round; the peak-to-average ratio is the median of the mean peak-to-average ratio of the D request length load of each round.

| Metric | Configuration A: LB → DB | Relative Change | Configuration B: LB → DB | Relative Change |
|---|---|---:|---|---:|
| Output throughput (token/s) | 1466.17 → 1497.06 | **Increased by 2.11%** | 1455.57 → 1503.39 | **Increased by 3.29%** |
| TPOT Mean (ms) | 82.80 → 81.80 | **Reduced by 1.21%** | 82.60 → 80.75 | **Reduced by 2.24%** |
| TPOT P90 (ms) | 90.00 → 89.50 | **Reduced by 0.56%** | 89.60 → 88.15 | **Reduced by 1.62%** |
| D request length load peak-to-average ratio | 1.4748 → 1.3857 | Reduced by 6.04% | 1.4773 → 1.5102 | Increased by 2.22% |

The throughput improvement is calculated as `DB / LB − 1`, and the metric reduction ratio is calculated as `1 − DB / LB`.

Under the Atlas 800I A3, Decode CPU offload, and long-tail mixed workload configuration described above, both the output throughput and TPOT P90 of DB are improved.
The improvement in the load peak-to-average ratio is not consistent. In separate verifications outside this section, positive benefits were also observed in some configurations with offload disabled;
the related comparison data is not listed in this section and is not included in the conclusions of the two experimental groups above. Negative throughput benefits exist in other parameter combinations,
so actual use still requires verification against the target workload, concurrency, and bucketing parameters.
