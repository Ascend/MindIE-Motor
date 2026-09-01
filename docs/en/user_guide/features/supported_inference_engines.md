# Supported Inference Engines

## Engine Overview

MindIE Motor adopts an architecture that decouples the control plane (Controller/Coordinator) from the data plane (inference engine), and can connect to multiple LLM inference engines. The currently supported engines are as follows:

| Inference Engine | Support Status | Description |
| --- | --- | --- |
| **vLLM** | Supported (recommended) | Used together with `vllm-ascend`; offers the most complete documentation and examples, and is the currently recommended engine. |
| **SGLang** | Supported (POC) | Can be deployed via `engine_type: sglang`. The coverage of some advanced capabilities may differ from that of vLLM; refer to the corresponding feature documentation and examples  for details. |

Set `engine_type` in `motor_engine_prefill_config`/`motor_engine_decode_config` (or `motor_engine_union_config` in the mixed deployment scenario) of `user_config.json` to select the underlying engine. `engine_config` corresponds to the engine startup command parameters. For the conversion method, see [Full Parameter Description of user_config](../configuration/config_reference.md).

## vLLM

vLLM is the underlying inference engine currently recommended by MindIE Motor and has been deeply integrated with the control plane.

### Configuring vLLM

Specify vLLM through `engine_type`:

```json
"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "engine_config": {
    "served_model_name": "qwen3-8B",
    "model": "/mnt/weight/qwen3_8B",
    "tensor_parallel_size": 2,
    ...
  }
}
```

## SGLang

In scenarios that rely on prefix reuse, such as multi-turn conversations, agent search, and few-shot learning, SGLang often leverages mechanisms such as RadixAttention effectively.

### Configuring SGLang

```json
"motor_engine_prefill_config": {
  "engine_type": "sglang",
  "engine_config": {
    "served-model-name": "qwen3-8B",
    "model-path": "/mnt/weight/Qwen3-8B",
    "tp-size": 2,
    ...
  }
}
```
