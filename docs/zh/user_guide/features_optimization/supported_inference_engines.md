# 支持的推理引擎

## 推理引擎说明

MindIE Motor采用控制面（Controller/Coordinator）与数据面（推理引擎）解耦的架构，可对接多种大模型推理引擎，当前支持的推理引擎如下所示。

**表 1** 支持的推理引擎

| 推理引擎 | 支持状态 | 说明 |
| --- | --- | --- |
| **vLLM** | 已支持（推荐） | vLLM为当前MindIE Motor推荐的底层推理引擎，已与控制面深度对接。**缺少对vLLM引擎的描述，需要补充** |
| **SGLang** | 已支持（POC） | SGLang在多轮对话、Agent搜索、Few-shot等依赖前缀复用的场景中常能较好利用RadixAttention等机制。 |

## 配置推理引擎

### 配置方法

修改user_config.json配置文件中`engine_type`参数，以及engine_config字段下的参数与对应引擎命令参数保持一致。

- PD分离服务部署：修改配置文件中motor_engine_prefill_config/motor_engine_decode_config字段中的`engine_type`参数，以及engine_config字段。
- PD混部服务部署：修改配置文件中motor_engine_union_config字段中的`engine_type`参数，以及engine_config字段。

### 配置vLLM推理引擎

以motor_engine_prefill_config字段为例，通过"engine_type"参数指定"vllm"，"engine_config"字段下的参数与vLLM引擎命令保持一致，配置示例如下：

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

配置完成后，启动MindIE Motor服务，日志中应出现类似`engine_type: vllm`、`vLLM engine initialized successfully`等关键字，表示vLLM引擎对接成功。

### 配置SGLang推理引擎

以motor_engine_prefill_config字段为例，通过"engine_type"参数指定"sglang"，"engine_config"字段下的参数与SGLang引擎命令保持一致，配置示例如下：

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

>[!NOTE] 说明
>PD分离服务部署中使用SGLang引擎时，bootstrap端口按Pod/NodeManager维度配置在`engine_config.disaggregation_bootstrap_port`（也兼容原生CLI风格的`disaggregation-bootstrap-port`）。NodeManager将该端口作为`bootstrap_port`注册元数据，并由Coordinator的SGLang Adapter用于Prefill/Decode对接；它与推理业务端口`endpoint_config.service_ports`是不同端口。未配置该字段时不生成bootstrap元数据。

配置完成后，启动MindIE Motor服务，日志中应出现类似`engine_type: sglang`、`SGLang engine initialized successfully`等关键字，表示SGLang引擎对接成功。
