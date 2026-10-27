# Supported Inference Engines

## Inference Engine Description

MindIE Motor adopts an architecture that decouples the control plane (Controller/Coordinator) from the data plane (inference engine), and can connect to multiple LLM inference engines. The currently supported inference engines are as follows.

**Table 1** Supported inference engines

| Inference Engine | Support Status | Description |
| --- | --- | --- |
| **vLLM** | Supported | vLLM is a high-throughput inference engine for online LLM serving. It manages KV cache in pages with PagedAttention and batches requests of different lengths continuously with continuous batching, thereby improving concurrency and throughput under limited memory. |
| **SGLang** | Supported | In scenarios that rely on prefix reuse, such as multi-turn conversations, agent search, and few-shot learning, SGLang often leverages mechanisms such as RadixAttention effectively. |

## Configuring an Inference Engine

### Configuration Method

Modify the `engine_type` parameter in the user_config.json configuration file, and ensure that the parameters in the engine_config field are consistent with the command parameters of the corresponding engine.

- PD disaggregation service deployment: Modify the `engine_type` parameter and the engine_config field in the motor_engine_prefill_config/motor_engine_decode_config fields of the configuration file.
- PD aggregation service deployment: Modify the `engine_type` parameter and the engine_config field in the motor_engine_union_config field of the configuration file.

### Configuring the vLLM Inference Engine

Taking the motor_engine_prefill_config field as an example, specify "vllm" through the "engine_type" parameter. The parameters in the "engine_config" field must be consistent with the vLLM engine commands. The configuration example is as follows:

```json
"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "engine_config": {
    "served_model_name": "qwen3-8B",
    "model": "/mnt/weight/qwen3_8B",
    "tensor_parallel_size": 2
  }
}
```

After the configuration is complete, start the MindIE Motor service. Keywords such as `engine_type: vllm` and `vLLM engine initialized successfully` should appear in the logs, indicating that the vLLM engine is connected successfully.

### Configuring the SGLang Inference Engine

Taking the motor_engine_prefill_config field as an example, specify "sglang" through the "engine_type" parameter. The parameters in the "engine_config" field must be consistent with the SGLang engine commands. The configuration example is as follows:

```json
"motor_engine_prefill_config": {
  "engine_type": "sglang",
  "engine_config": {
    "served-model-name": "qwen3-8B",
    "model-path": "/mnt/weight/Qwen3-8B",
    "tp-size": 2
  }
}
```

>[!NOTE]NOTE
>
>- For details about PD disaggregation deployment based on the SGLang engine framework, see [SGLang PD Disaggregation Service Deployment Guide](../deployment/k8s/pd_disaggregation_sglang.md). For typical example configurations, see the `model_configs/sglang/` directory (for example, `models/glm5.1/A3/` and `models/qwen_8b/A2/`).
>- When the SGLang engine is used in PD disaggregation service deployment, the bootstrap port is configured per Pod/NodeManager in `engine_config.disaggregation_bootstrap_port` (the native CLI style `disaggregation-bootstrap-port` is also supported). NodeManager registers this port as `bootstrap_port` metadata, and the SGLang Adapter of the Coordinator uses it for Prefill/Decode connection. It is different from the inference service port `endpoint_config.service_ports`. If this field is not configured, no bootstrap metadata is generated.

After the configuration is complete, start the MindIE Motor service. Keywords such as `engine_type: sglang` and `SGLang engine initialized successfully` should appear in the logs, indicating that the SGLang engine is connected successfully.
