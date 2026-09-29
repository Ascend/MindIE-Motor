# 故障场景重调度

## 特性介绍

故障场景重调度用于降低推理节点故障或Coordinator与推理节点之间异常断链对请求的影响。发生可重试故障时，Coordinator重新选择可用推理实例并再次发送请求。

对于尚未向客户端返回响应体的请求，Coordinator可以使用原请求重新发起推理。对于已经返回部分内容的流式请求，开启故障场景重调度后，Coordinator缓存输入Token ID和已生成的输出Token ID，并在满足续推条件时将二者拼接为新的输入，交由其他可用实例继续生成，避免向客户端重复返回已经接收的内容。

> [!NOTE] 说明
>
> 故障场景重调度功能默认关闭。该开关控制流式请求输出后的Token续推，不控制推理引擎内部的重计算能力。

### 工作原理

1. 流式请求到达Coordinator后，Coordinator向推理引擎请求返回Token ID，并缓存原始输入的`prompt_token_ids`和流式响应中的`token_ids`。
2. 推理节点故障或传输链路发生可重试异常时，Coordinator释放本次尝试占用的资源，并重新申请可用推理实例。
3. 如果故障发生在客户端收到响应体之前，Coordinator使用原请求重试；如果故障发生在客户端收到部分输出之后，Coordinator使用“输入Token ID + 已输出Token ID”构造续推请求。
4. 对于`/v1/chat/completions`请求，续推请求转换为`/v1/completions`格式，以Token ID列表作为`prompt`。Coordinator同时根据已经输出的Token数量扣减剩余的`max_tokens`或`max_completion_tokens`。
5. Coordinator将续推响应恢复为客户端原来使用的响应格式。客户端未显式请求`return_token_ids`时，内部使用的Token ID字段不会返回给客户端。

当Token ID不完整、请求不满足续推约束、流已经结束或重试次数耗尽时，Coordinator不会进行不安全的续推，而是结束请求并返回相应的错误。

### 约束与限制

| 约束维度 | 要求 |
| --- | --- |
| 硬件 | <ul><li>Atlas 800I A2推理服务器</li><li>Atlas 800I A3超节点服务器</li><li>Ascend 950PR系列产品</li></ul> |
| 部署场景 | 支持P/D分离路由和P/D混部路由。故障发生后必须仍有满足调度条件的健康实例，否则请求无法完成重调度。 |
| 推理引擎 | 支持vLLM和SGLang。输出响应体之前的请求重试适用于两种引擎；流式请求已输出内容后的Token续推，还要求引擎支持`return_token_ids`，并在响应中返回`prompt_token_ids`和`choices[].token_ids`。Chat Completions续推还要求引擎的`/v1/completions`接口支持以Token ID列表作为`prompt`。 |
| 请求类型 | 输出后的续推仅适用于流式请求。支持普通文本的Completions请求，以及可转换为Completions格式的Chat Completions请求。并行采样参数`n`必须为`1`。当前续推实现依赖`choices[].token_ids`，不适用于不返回该结构的原生SSE事件，例如`/v1/responses`的流式事件。 |
| Chat Completions请求 | 不支持在输出后续推包含`tools`、`logprobs`、`top_logprobs`、非文本多模态内容或非文本`response_format`的请求。续推会移除Chat专用字段，因此工具调用、函数调用、结构化输出等依赖Chat语义的请求不应开启输出后续推。 |
| 故障范围 | 仅对节点故障、可重试的传输异常等场景执行重调度。客户端主动取消、不可重试的引擎错误以及已经正常结束的流不会触发续推。并非所有超时或业务错误都可以通过重调度恢复。 |
| 资源占用 | 开启后，Coordinator会在流式请求生命周期内缓存请求体、输入Token ID和输出Token ID。需要根据最大并发数、上下文长度和请求体大小预留内存。 |

## 特性使用

### 环境准备

- 推理服务已正常部署，并且故障后仍有其他健康实例可供Coordinator调度。
- 推理引擎满足“约束与限制”中的Token ID返回和Token ID输入要求。建议在启用前使用实际部署的引擎确认响应中能够连续获得`prompt_token_ids`和`choices[].token_ids`。
- Coordinator已根据业务的最大并发数、上下文长度和请求体大小预留足够内存。

### 使用样例

故障场景重调度功能使用[user_config.json](../../configuration/config_reference.md#motor_coordinator_config)配置文件中的以下参数：

- `reschedule_config.enable`：是否开启流式请求输出后的Token续推，默认为`false`。
  - `false`：关闭输出后的Token续推。
  - `true`：开启输出后的Token续推。
- `transport_max_retry`：传输请求的最大尝试次数，包含首次请求。当配置为`null`时使用`max_retry`。
- `max_retry`：`transport_max_retry`为`null`时使用的最大尝试次数，默认值为`5`。
- `retry_delay`：第一次重试前的等待时间，单位为秒。后续重试采用指数退避，第`i`次重试前等待`retry_delay × 2^(i-1)`秒。

例如，`transport_max_retry`为`5`时，一个请求最多尝试5次，即首次请求失败后最多再重调度4次。

配置示例如下：

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

修改配置后，按照当前部署方式更新服务，使配置在Coordinator中生效。

## 调优建议

故障场景重调度功能开启时，会将流式请求的输入Token ID和流式响应的输出Token ID缓存在Coordinator中，直到推理任务结束后释放。Coordinator还会缓存请求体，因此需要同时考虑两部分内存占用。

以下按照10000并发、1M上下文的场景估算Coordinator的内存占用上限。按照1M Token占用约3～6MB内存估算，考虑极端情况时可按系数6计算。

- Token ID缓存的内存占用上限：
  - 内存占用上限 ≈ 并发数 × 上下文长度 × 系数6
  - 10000并发、1M上下文时，内存占用上限约为`10000 × 1M × 6 ≈ 60GB`。
- 请求体缓存的内存占用上限：
  - 内存占用上限 ≈ 并发数 × 报文长度 × 系数6
  - 10000并发、1M长报文时，内存占用上限约为`10000 × 1M × 6 ≈ 60GB`。

上述场景的两部分缓存合计超过120GB。再考虑Coordinator基础内存开销和其他功能的内存占用，建议将Coordinator容器的内存上限设置为`128Gi`。实际配置应以业务压测和监控结果为准，并为峰值流量保留余量。

- 最大并发数通过[user_config.json](../../configuration/config_reference.md#motor_coordinator_config)中的`max_requests`配置。
- Coordinator内存上限通过部署YAML中Coordinator容器的`resources.limits.memory`配置。
  - CRD模式可参考[examples/deployer/yaml_template/infer_service_template.yaml](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/infer_service_template.yaml)。
  - Multi模式可参考[examples/deployer/yaml_template/coordinator_template.yaml](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/coordinator_template.yaml)。

参考配置如下：

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

## 常见问题

### 流式请求无法从已输出位置继续

**问题描述**

开启故障场景重调度后，流式请求发生故障时仍然无法从已经输出的位置继续生成。

**原因分析**

可能存在以下原因：

- 推理引擎未在故障前返回`prompt_token_ids`，或者包含可见输出的响应未返回对应的`choices[].token_ids`。
- 请求不满足`n=1`以及Chat Completions请求的续推约束。
- 故障发生后没有满足调度条件的健康实例。
- `transport_max_retry`或`max_retry`配置的尝试次数已经耗尽。

只要可见输出对应的Token ID存在缺口，Coordinator就会停止输出后的续推，避免产生重复或错位内容。

**解决步骤**

1. 确认推理引擎在故障前返回`prompt_token_ids`，并在每个包含可见输出的响应中返回对应的`choices[].token_ids`。
2. 确认请求满足`n=1`以及Chat Completions请求的续推约束。
3. 确认故障发生后仍有满足调度条件的健康实例。
4. 确认`transport_max_retry`或`max_retry`配置的尝试次数尚未耗尽。

### Chat Completions请求在重调度时返回HTTP 502

**问题描述**

Chat Completions流式请求已经返回部分内容，发生故障后重调度返回HTTP 502。

**原因分析**

Chat Completions的输出后续推需要转换为Completions Token ID请求。如果请求使用`n>1`、工具调用、Logprobs、非文本多模态输入或结构化响应格式，Coordinator无法保证转换前后的语义一致，因此会中止重调度并返回HTTP 502。

**解决步骤**

1. 将并行采样参数`n`设置为`1`。
2. 移除`tools`、`logprobs`、`top_logprobs`、非文本多模态内容和非文本`response_format`等不支持续推的字段或内容。
3. 如果业务必须使用上述能力，请勿依赖输出后的续推，并在客户端处理流式请求中断。

### 客户端看不到Token ID字段

**问题描述**

客户端响应中没有`prompt_token_ids`和`token_ids`字段。

**原因分析**

这些字段默认仅供Coordinator内部续推使用。客户端未显式请求时，Coordinator会在返回响应前移除Token ID字段。

**解决步骤**

在原请求中设置`return_token_ids: true`，并确认推理引擎支持返回Token ID字段。
