# 支持的推理引擎

## 引擎一览

MindIE PyMotor（以下简称 PyMotor）采用控制面（Controller/Coordinator）与数据面（推理引擎）解耦的架构，可对接多种大模型推理引擎。当前支持的引擎如下：

| 推理引擎 | 支持状态 | 说明 |
| --- | --- | --- |
| **vLLM** | 已支持（推荐） | 配合 `vllm-ascend` 使用；文档与示例最完整，为当前主推引擎。 |
| **SGLang** | 已支持 | 可通过 `engine_type: sglang` 部署。部分高级能力与 vLLM 的覆盖范围可能不同，以对应特性文档与示例为准。 |

在 `user_config.json` 的 `motor_engine_prefill_config` / `motor_engine_decode_config`（或混部场景的 `motor_engine_union_config`）中设置 `engine_type`，即可选择底层引擎。`engine_config` 与引擎启动命令参数对应，转换方法见 [user_config 全量参数说明](../configuration/config_reference.md)。

---

## vLLM

vLLM 是当前 PyMotor 推荐的底层推理引擎，已与控制面深度对接。

### 配置 vLLM

通过 `engine_type` 指定 vLLM：

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

---

## SGLang

SGLang 在多轮对话、Agent 搜索、Few-shot 等依赖前缀复用的场景中，常能较好利用 RadixAttention 等机制。

### 配置 SGLang

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
