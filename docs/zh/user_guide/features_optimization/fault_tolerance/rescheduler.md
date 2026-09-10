# 故障场景重调度

## 特性介绍

故障场景重调度功能开启时，在推理节点发生故障，或者Coordinator与推理节点之间异常断链，导致推理过程异常中断的故障场景，Coordinator将推理请求重调度到其他正常的推理节点，继续完成推理任务。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 约束维度 | 要求 |
|----------|------|
| 硬件 | **内容缺失，需要人工补齐。**  |
| 部署场景 | **内容缺失，需要人工补齐。**  |
| 引擎 | **内容缺失，需要人工补齐。**  |
| 特性互斥 | **内容缺失，需要人工补齐。** |
| 软件依赖 |**内容缺失，需要人工补齐。**  |
| 其他限制 | 故障场景重调度功能开启时占用`Coordinator`内存，需根据最大并发数和上下文长度计算内存占用上限并修改`Coordinator`的内存配置。 |

## 特性使用

### 环境准备

**内容缺失，需要人工补齐。**

### 使用样例

故障场景重调度功能使用[user_config.json](../../configuration/config_reference.md#motor_coordinator_config)配置文件的以下配置参数：

- 配置故障场景重调度功能开关：使用`reschedule_config`中的`enable`配置参数，默认为`false`。
   - false：故障场景重调度功能关闭；
   - true：故障场景重调度功能开启。
- 配置重调度次数：使用`transport_max_retry`配置参数；当`transport_max_retry`配置参数为空时，使用`max_retry`配置参数。
- 配置重调度间隔：使用`retry_delay`参数，浮点值，单位为秒。
   - 重调度间隔算法：每个推理任务，第一次故障等待`retry_delay`秒后进行重调度，后续每次重调度间隔是上一次重调度间隔的2倍。

故障场景重调度功能配置示例如下所示：

```json
{
  "motor_coordinator_config": {
    "exception_config": {
      "reschedule_config": {
        "enable": false
      },
      "max_retry": 5,
      "transport_max_retry": null,
      "retry_delay": 0.2
    }
  }
}
```

### 验证特性

**内容缺失，需要人工补齐。**

## 调优建议

故障场景重调度功能开启时，会将流式请求的`prompt_tokens`和流式响应的`tokens`缓存在`Coordinator`，在推理任务中占用`Coordinator`的内存，直到推理任务结束释放内存。

以下按照10000并发+1M上下文示例，在故障场景重调度功能开启时，计算`Coordinator`的内存占用上限。
(按照业界经验值，1M token约占用内存3～6MB，考虑极端情况，建议按照**系数6**计算。)

多并发场景下：

- 故障重调度功能的内存占用计算公式为：
  - 故障重调度功能的内存占用上限 ≈ 并发数 × 上下文长度 × 系数6
  - 按照以上公式，10000并发+1M上下文应用场景，故障场景重调度功能的内存占用上限为：
  - 故障重调度功能的内存占用上限 ≈ 10000 × 1M × 系数6 ≈ 60GB
- 另外考虑基础能力，请求体缓存需要占用内存，考虑长报文极端场景下，同样按照**系数6**计算，请求体缓存的内存占用计算公式为：
  - 请求体缓存的内存占用上限 ≈ 并发数 × 报文长度 × 系数6
  - 按照以上公式，10000并发+1M长报文应用场景，请求体的内存占用上限为：
  - 请求体缓存的内存占用上限 ≈ 10000 × 1M × 系数6 ≈ 60GB

因此，`Coordinator`的内存占用上限`> 60GB + 60GB = 120GB`。考虑`Coordinator`基础内存开销和其他功能的内存占用，`Coordinator`实际的内存上限建议设置为`128GB`。

当故障场景重调度功能开启时，建议根据`最大并发数`和`上下文长度`，按照上述公式计算内存占用上限，并修改`Coordinator`的内存配置。

- 最大并发数配置，参考[user_config.json](../../configuration/config_reference.md#motor_coordinator_config)配置文件中的`max_requests`配置参数；

- `Coordinator`内存占用上限设置，需要修改`yaml`文件中`coordinator`容器中`resources.limits.memory`配置项；
  - 当使用CRD模式部署时，`yaml`文件参考[examples/deployer/yaml_template/infer_service_template.yaml](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/infer_service_template.yaml)；
  - 当使用Multi模式部署时，`yaml`文件参考[examples/deployer/yaml_template/coordinator_template.yaml](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/coordinator_template.yaml)。

参考示例如下：

```yaml
containers:
- name: mindie-motor-coordinator
  ...
  resources:
    requests:
      memory: "4Gi"
      cpu: "16"
    limits:
      memory: "128Gi"
      cpu: "64"
```

## 常见问题

**内容缺失，需要人工补齐。**
