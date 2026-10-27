# Request Rescheduling

## Feature Introduction

Request rescheduling is used to reduce the impact of inference node faults or abnormal disconnections between the Coordinator and inference nodes on requests. When a retryable fault occurs, the Coordinator reselects an available inference instance and sends the request again.

For requests for which no response body has been returned to the client, the Coordinator can re-initiate inference with the original request. For streaming requests whose partial content has already been returned, after request rescheduling is enabled, the Coordinator caches the input token IDs and the generated output token IDs, concatenates them into a new input when the continuation conditions are met, and has another available instance continue generating, avoiding returning the content already received by the client again.

> [!NOTE]NOTE
>
> The request rescheduling function is disabled by default. This switch controls token continuation after the output of streaming requests, and does not control the recomputation capability inside the inference engine.

### How It Works

1. After a streaming request reaches the Coordinator, the Coordinator requests the inference engine to return token IDs, and caches the `prompt_token_ids` of the original input and the `token_ids` in the streaming response.
2. When an inference node fails or a retryable exception occurs on the transport link, the Coordinator releases the resources occupied by this attempt and applies for an available inference instance again.
3. If the fault occurs before the client receives the response body, the Coordinator retries with the original request; if the fault occurs after the client has received part of the output, the Coordinator constructs a continuation request using "input token IDs + output token IDs already generated".
4. For `/v1/chat/completions` requests, the continuation request is converted to the `/v1/completions` format, with the token ID list used as `prompt`. At the same time, the Coordinator deducts the remaining `max_tokens` or `max_completion_tokens` according to the number of tokens already output.
5. The Coordinator restores the continuation response to the response format originally used by the client. When the client does not explicitly request `return_token_ids`, the internally used token ID fields are not returned to the client.

When token IDs are incomplete, the request does not meet the continuation constraints, the stream has ended, or the number of retries is exhausted, the Coordinator does not perform unsafe continuation. Instead, it ends the request and returns the corresponding error.

### Constraints and Limitations

| Constraint Dimension | Requirement |
| --- | --- |
| Hardware | <ul><li>Atlas 800I A2</li><li>Atlas 800I A3</li><li>Ascend 950PR products</li></ul> |
| Deployment scenario | P/D disaggregation routing and P/D aggregation routing are supported. After a fault occurs, healthy instances that meet the scheduling conditions must still exist; otherwise, the request cannot be rescheduled. |
| Inference engine | vLLM and SGLang are supported.<br>Request retry before the response body is output applies to both engines. Token continuation after a streaming request has output content additionally requires the engine to support `return_token_ids` and to return `prompt_token_ids` and `choices[].token_ids` in the response. Chat Completions continuation also requires the `/v1/completions` interface of the engine to support a token ID list as `prompt`. |
| Request type | Continuation after output applies only to streaming requests.<ul><li>Completions requests with plain text, and Chat Completions requests that can be converted to the Completions format, are supported.</li><li>The parallel sampling parameter `n` must be `1`.</li><li>The current continuation implementation depends on `choices[].token_ids`.</li><li>It does not apply to native SSE events that do not return this structure, such as the streaming events of `/v1/responses`.</li></ul>|
| Chat Completions requests | Continuation after output is not supported for requests that contain `tools`, `logprobs`, `top_logprobs`, non-text multimodal content, or a non-text `response_format`. Continuation removes Chat-specific fields, so continuation after output should not be enabled for requests that depend on Chat semantics, such as tool calling, function calling, and structured output. |
| Fault scope | Rescheduling is performed only for scenarios such as node faults and retryable transport exceptions. Client-initiated cancellation, non-retryable engine errors, and streams that have already ended normally do not trigger continuation. Not all timeouts or service errors can be recovered through rescheduling. |
| Resource usage | After the feature is enabled, the Coordinator caches the request body, input token IDs, and output token IDs during the lifecycle of a streaming request. Memory must be reserved according to the maximum concurrency, context length, and request body size. |

## Feature Usage

### Environment Preparation

- The inference service has been deployed properly, and other healthy instances are still available for the Coordinator to schedule after a fault occurs.
- The inference engine meets the token ID return and token ID input requirements described in "Constraints and Limitations". Before enabling the feature, it is recommended to use the actually deployed engine to confirm that `prompt_token_ids` and `choices[].token_ids` can be obtained continuously in the response.
- The Coordinator has reserved sufficient memory according to the maximum concurrency, context length, and request body size of the service.

### Usage Examples

The request rescheduling function uses the following parameters in the [user_config.json](../../configuration/config_reference.md#motor_coordinator_config) configuration file:

- reschedule_config.enable: whether to enable token continuation after the output of streaming requests. The default value is `false`.
  - false: disables token continuation after output.
  - true: enables token continuation after output.
- transport_max_retry: maximum number of transport request attempts, including the first request. When it is configured as `null`, `max_retry` is used.
- max_retry: maximum number of attempts used when `transport_max_retry` is `null`. The default value is `5`.
- retry_delay: waiting time before the first retry, in seconds. Subsequent retries use exponential backoff, and the waiting time before the `i`-th retry is `retry_delay × 2^(i-1)` seconds.

For example, when `transport_max_retry` is `5`, a request makes at most 5 attempts, that is, after the first request fails, rescheduling is performed at most 4 more times.

The following shows a configuration example:

```json
{
  "motor_coordinator_config": {
    "exception_config": {
      "reschedule_config": {
        "enable": true
      },
      "max_retry": 5,
      "transport_max_retry": null,
      "retry_delay": 0.2
    }
  }
}
```

After the configuration is modified, update the service according to the current deployment mode so that the configuration takes effect in the Coordinator.

## Tuning Suggestions

When the request rescheduling function is enabled, the input token IDs of streaming requests and the output token IDs of streaming responses are cached in the Coordinator until the inference task ends and they are released. The Coordinator also caches the request body, so the memory usage of both parts must be considered.

The following estimates the memory usage limit of the Coordinator in a scenario with 10000 concurrent requests and a 1M context. Based on the estimate that 1M tokens occupy approximately 3 to 6 MB of memory, factor 6 can be used for extreme cases.

- Memory usage limit of the token ID cache:
  - Memory usage limit ≈ concurrency × context length × factor 6
  - With 10000 concurrent requests and a 1M context, the memory usage limit is approximately `10000 × 1M × 6 ≈ 60GB`.
- Memory usage limit of the request body cache:
  - Memory usage limit ≈ concurrency × message length × factor 6
  - With 10000 concurrent requests and 1M long messages, the memory usage limit is approximately `10000 × 1M × 6 ≈ 60GB`.

The two caches in the preceding scenario total more than 120 GB. Considering the basic memory overhead of the Coordinator and the memory usage of other functions, it is recommended to set the memory limit of the Coordinator container to `128Gi`. The actual configuration should be based on service stress test and monitoring results, and headroom should be reserved for peak traffic.

- The maximum concurrency is configured through `max_requests` in [user_config.json](../../configuration/config_reference.md#motor_coordinator_config).
- The Coordinator memory limit is configured through `resources.limits.memory` of the Coordinator container in the deployment YAML.
  - For the CRD mode, see [examples/deployer/yaml_template/infer_service_template.yaml](../../../../../examples/deployer/yaml_template/infer_service_template.yaml).
  - For the Multi mode, see [examples/deployer/yaml_template/coordinator_template.yaml](../../../../../examples/deployer/yaml_template/coordinator_template.yaml).

The following is a reference configuration:

```yaml
containers:
- name: mindie-motor-coordinator
  # ...
  resources:
    requests:
      memory: "4Gi"
      cpu: "16"
    limits:
      memory: "128Gi"
      cpu: "64"
```

## FAQs

### A Streaming Request Cannot Continue from the Position Already Output

**Symptom**

After request rescheduling is enabled, a streaming request still cannot continue generating from the position already output when a fault occurs.

**Cause Analysis**

The possible causes are as follows:

- The inference engine did not return `prompt_token_ids` before the fault, or the response containing visible output did not return the corresponding `choices[].token_ids`.
- The request does not meet `n=1` or the continuation constraints of Chat Completions requests.
- No healthy instance that meets the scheduling conditions exists after the fault occurs.
- The number of attempts configured in `transport_max_retry` or `max_retry` has been exhausted.

As long as a gap exists in the token IDs corresponding to the visible output, the Coordinator stops continuation after output to avoid duplicate or misaligned content.

**Solution**

1. Confirm that the inference engine returns `prompt_token_ids` before the fault and returns the corresponding `choices[].token_ids` in each response containing visible output.
2. Confirm that the request meets `n=1` and the continuation constraints of Chat Completions requests.
3. Confirm that a healthy instance that meets the scheduling conditions still exists after the fault occurs.
4. Confirm that the number of attempts configured in `transport_max_retry` or `max_retry` has not been exhausted.

### A Chat Completions Request Returns HTTP 502 During Rescheduling

**Symptom**

A Chat Completions streaming request has returned part of the content, and HTTP 502 is returned by rescheduling after a fault occurs.

**Cause Analysis**

Continuation after output for Chat Completions needs to be converted into a Completions token ID request. If the request uses `n>1`, tool calling, Logprobs, non-text multimodal input, or a structured response format, the Coordinator cannot guarantee semantic consistency before and after the conversion, so it aborts rescheduling and returns HTTP 502.

**Solution**

1. Set the parallel sampling parameter `n` to `1`.
2. Remove fields or content that do not support continuation, such as `tools`, `logprobs`, `top_logprobs`, non-text multimodal content, and a non-text `response_format`.
3. If the service must use the preceding capabilities, do not rely on continuation after output, and handle streaming request interruptions on the client.

### The Client Cannot See the Token ID Fields

**Symptom**

The client response does not contain the `prompt_token_ids` and `token_ids` fields.

**Cause Analysis**

These fields are used only internally by the Coordinator for continuation by default. When the client does not explicitly request them, the Coordinator removes the token ID fields before returning the response.

**Solution**

Set `return_token_ids: true` in the original request, and confirm that the inference engine supports returning the token ID fields.
