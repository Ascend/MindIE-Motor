# max_tokens Adaptation

## Feature Introduction

The max_tokens adaptation feature uses the Coordinator to calculate the actual number of input tokens with the model tokenizer before request routing, and reduces `max_tokens` or `max_completion_tokens` in the request when necessary, so that the sum of the input token count and the expected output token count of a request does not exceed the model context window.

**Benefits**: it prevents requests from being rejected or failing with errors in the inference engine due to exceeding the context window at the source, reducing invalid requests and retries and improving the availability of the inference service.

The feature has the following characteristics:

- It takes effect for all three scheduling policies: `load_balance`, `round_robin`, and `kv_cache_affinity`.

- It clamps only when the output token upper limit requested by the client exceeds the remaining context, and never increases the value specified by the client.

- It is disabled by default. When it is disabled, the Coordinator does not modify requests, and the backend inference engine validates the context length according to its own rules.

- It does not depend on KV Cache affinity scheduling, and does not require the kv-conductor service to be deployed.

### How It Works

After receiving an inference request, the Coordinator completes tokenization first, and then calculates the actual output upper limit according to the following rules:

```text
Model context upper limit = min(p_max_seqlen, d_max_seqlen)
Remaining context         = model context upper limit - number of input tokens
Actual output upper limit = min(output upper limit requested by the client, remaining context)
```

When only one of `p_max_seqlen` and `d_max_seqlen` is a valid positive integer, the valid value is used; when both are valid, the smaller value is used, ensuring that the request satisfies the context constraints of both Prefill and Decode instances.

For example, the maximum sequence lengths of Prefill and Decode are `131072` and `114688` respectively, the request contains `100000` input tokens after tokenizer calculation, and the client specifies `max_tokens: 20000`:

```text
Model context upper limit = min(131072, 114688) = 114688
Remaining context         = 114688 - 100000 = 14688
Actual max_tokens         = min(20000, 14688) = 14688
```

The Coordinator changes the `max_tokens` of the request to `14688` before scheduling and forwarding it.

**Parameter Selection Rules**

| Request Form | Parameter Subject to Clamping |
|---------|---------------|
| Chat request that contains a valid max_completion_tokens | max_completion_tokens |
| Chat request that does not provide a valid max_completion_tokens but provides a valid max_tokens | max_tokens |
| Completion request | max_tokens |

When a Chat request carries both `max_completion_tokens` and `max_tokens`, only `max_completion_tokens`, which has the higher priority, is adjusted, and `max_tokens` is not modified.

**Token Calculation**

- For Chat requests, tokens are calculated using the chat template of the model, and fields that affect the engine input, such as `messages`, `tools`, `chat_template_kwargs`, and `reasoning_effort`, are included.

- Completion requests support a string `prompt` and a `prompt` in the form of a token ID list.

- The token IDs calculated for the same request are reused, avoiding repeated calculation between context clamping and KV Cache affinity scheduling.

### Constraints and Limitations

| Dimension | Constraint Description |
|------|---------|
| Hardware | <ul><li>Atlas 800I A2s</li><li>Atlas 800I A3s</li><li>Ascend 950PR products</li></ul> |
| Deployment scenario | PD disaggregation (the Prefill and Decode engines are separated) and PD aggregation (Union engine) deployment are supported. |
| Engine | Only vLLM is supported |
| Feature mutual exclusion | None |
| Software dependency | For each software, see the version compatibility in its release notes. |
| Other constraints | <ul><li>The Coordinator must have at least the read permission on the tokenizer files in the model directory. To ensure that the same tokenizer version as the inference engine is used, it is recommended to mount the complete model directory in read-only mode.</li><li>This feature adjusts only a valid positive integer `max_tokens` or `max_completion_tokens`. If the request does not carry these parameters, or the parameters are not positive integers, the Coordinator does not make adjustments.</li><li>When a request carries a `max_tokens` or `max_completion_tokens` that is not a positive integer (for example, 0, a negative number, a boolean, or a non-int type), the Coordinator does not return 400. Instead, it removes the parameter and records a WARNING log, and the request is handled as if the parameter were not carried (that is, it continues with the default output upper limit of the model). To detect such configuration errors, check the `Invalid max_tokens=` / `Invalid max_completion_tokens=` warnings in the Coordinator log.</li><li>When the number of input tokens has reached or exceeded the model context upper limit, the Coordinator does not modify the output upper limit, and the request is still subject to the context validation performed by the backend inference engine, which returns the corresponding error.</li><li>When TokenizerManager cannot obtain valid token IDs, the Coordinator does not clamp the request based on estimated values. Instead, it keeps the original output upper limit and leaves the validation to the backend inference engine.</li><li>Prefill and Decode should use compatible tokenizers. When the `max_model_len` values at both ends are inconsistent, the feature always clamps according to the smaller value.</li></ul> |

## Using the Feature

### Environment Preparation

- The `model` in the Prefill (PD disaggregation) or Union (PD aggregation) engine configuration must point to a model directory accessible to the Coordinator, which is used to load the tokenizer consistent with the inference engine.

- When deploying with `examples/deployer/deploy.py`, `motor_deploy_config.weight_mount_path` is mounted to the P/D (or Union) engine and the Coordinator at the same container path. Configure it as a host directory that contains `engine_config.model`. For example, when the model path is `/data/weights/model-a`, you can configure `weight_mount_path: "/data/weights"`. The Coordinator must have at least the read permission on the tokenizer files in the model directory. To ensure that the same tokenizer version as the inference engine is used, it is recommended to mount the complete model directory in read-only mode.

- `weight_mount_path` uses the Kubernetes `hostPath`. Therefore, the host path and the corresponding model files must exist on every node to which the Coordinator may be scheduled. If the weights exist only on some nodes, use `coordinator_node_selector` to schedule the Coordinator to a node that can access the weights. P/D (or Union) engine nodes must also meet the same condition.

- When custom YAML, legacy deployment artifacts, or other deployment tools are used, you need to explicitly add a model directory mount consistent with `engine_config.model` in the Coordinator Pod. Mounting the weights only to the P/D (or Union) engine Pod is not sufficient to enable this feature.

- A valid context upper limit must be provided. The PD disaggregation configuration automatically fills the `max_model_len` of the Prefill and Decode engines with `aigw.p_max_seqlen` and `aigw.d_max_seqlen` respectively. When automatic filling is not possible, `aigw` must be explicitly configured.

- When this feature is enabled, each Coordinator Inference worker initializes an in-process `TokenizerManager` singleton during startup and loads the tokenizer from the model directory; no independent tokenizer service process is created. If the model directory is inaccessible or the tokenizer fails to load, the corresponding Inference worker cannot start properly.

### Usage Example

Set the `context_budget_mode` parameter to `on` in the `motor_coordinator_config` field of user_config.json. The following example shows only the related configuration:

```json
{
  "motor_coordinator_config": {
    "context_budget_mode": "on",
    "scheduler_config": {
      "scheduler_type": "load_balance"
    }
  },
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "engine_config": {
      "model": "/mnt/weight/your-model",
      "served_model_name": "your-model",
      "max_model_len": 131072
    }
  },
  "motor_engine_decode_config": {
    "engine_type": "vllm",
    "engine_config": {
      "model": "/mnt/weight/your-model",
      "served_model_name": "your-model",
      "max_model_len": 114688
    }
  }
}
```

>[!NOTE]NOTE
>
>- The deployment process automatically obtains the `model_path` of the tokenizer from the Prefill (PD disaggregation) or Union (PD aggregation) engine configuration.
>- In the PD disaggregation scenario, the context upper limits are also obtained from the Prefill and Decode engine configurations. You do not need to enable `kv-events-config` or configure `conductor_service` for this feature.
>- In the PD aggregation scenario or other scenarios where the Prefill and Decode context upper limits cannot be extracted at the same time, you need to explicitly configure them in `motor_coordinator_config.aigw`. For example, the maximum sequence length of a Union engine is `114688`.
>
>   ```json
>   {
>     "motor_coordinator_config": {
>       "context_budget_mode": "on",
>       "aigw": {
>         "p_max_seqlen": 114688,
>         "d_max_seqlen": 114688
>       }
>     }
>   }
>   ```

The configuration parameters are described as follows:

| Parameter | Type | Default Value | Mandatory | Value Range | Description |
|-----|------|-------|------|---------|------|
| motor_coordinator_config.context_budget_mode | string | off | No | `off`, `on` | <ul><li>off: does not modify the output token upper limit in the request;</li><li>on: clamps the output token upper limit according to the remaining context of the model.</li></ul> |
| engine_config.model of Prefill or Union | string | None | Yes | Model directory | Path from which TokenizerManager in the Inference worker loads the tokenizer |
| motor_engine_prefill_config.engine_config.max_model_len | int | None | Yes | Positive integer | Maximum sequence length of the Prefill end |
| motor_engine_decode_config.engine_config.max_model_len | int | None | Yes | Positive integer | Maximum sequence length of the Decode end |

For the complete configuration fields, see [Full Parameter Description](../configuration/config_reference.md).

### Verifying the Feature

Send a request whose output upper limit is greater than the remaining context of the model:

```bash
curl -X POST http://{coordinator-ip}:1025/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-model",
    "messages": [{"role": "user", "content": "your-long-prompt"}],
    "max_tokens": 20000
  }'
```

When clamping occurs, the Coordinator outputs the following INFO log:

```text
Context budget clamped req_id=<request-id> parameter=max_tokens requested=20000 effective=14688 prompt_tokens=100000 max_model_len=114688
```

The meaning of each field is as follows:

| Field | Description |
|------|------|
| `parameter` | The request parameter adjusted this time. |
| `requested` | The output token upper limit requested by the client. |
| `effective` | The output token upper limit actually forwarded to the engine by the Coordinator. |
| `prompt_tokens` | The number of input tokens calculated by the Coordinator using the model tokenizer. |
| `max_model_len` | The smaller value of the Prefill and Decode context upper limits. |

If the output upper limit of the request does not exceed the remaining context, the clamping log is not printed, and the request parameters remain unchanged.

## FAQs

### The Coordinator Fails to Start After the Feature Is Enabled

**Symptom**

After the `max_tokens` adaptation is enabled, the Coordinator Inference worker fails to start.

**Cause Analysis**

The Prefill engine configuration lacks a valid `model` or `max_model_len`, the Decode engine configuration lacks a valid `max_model_len`, or the Coordinator container cannot access the model directory. The startup configuration validation requires that the tokenizer path and at least one valid `p_max_seqlen` or `d_max_seqlen` can be obtained.

**Solution**

1. Check whether the Prefill engine configuration contains a valid `model` and `max_model_len`.

2. Check whether the Decode engine configuration contains a valid `max_model_len`.

3. Confirm that the Coordinator container can access the model directory.

4. For a `hostPath` mount, check whether `weight_mount_path` exists on the node where the Coordinator actually resides.

### The Request Is Not Clamped

**Symptom**

Although `context_budget_mode` is set to `on`, the `max_tokens` or `max_completion_tokens` of the request is not clamped.

**Cause Analysis**

The feature may not be enabled correctly, the request may not carry a valid positive integer parameter, the output upper limit may not exceed the remaining context, or TokenizerManager may not be initialized.

**Solution**

1. Check whether `motor_coordinator_config.context_budget_mode` is `on`.

2. Confirm whether the request carries a valid positive integer `max_tokens` or `max_completion_tokens`.

3. Confirm whether the output upper limit specified by the client is indeed greater than "the model context upper limit minus the number of input tokens".

4. Check in the Inference worker log whether TokenizerManager is initialized, and whether the Coordinator can use the model tokenizer to process the current request.

### Why Is max_tokens Not Changed to 1 for an Overly Long Input

**Symptom**

When the input itself has already used up the model context, the Coordinator does not forcibly set `max_tokens` to `1`, but keeps the original request.

**Cause Analysis**

When the input itself has already used up the model context, no positive output budget is available. The Coordinator does not mask an overly long input by forcibly setting `max_tokens` to `1`.

**Solution**

Keep the original request so that the backend inference engine returns a context length error, and troubleshoot the overly long input based on that error.
