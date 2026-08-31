# 故障场景重调度

故障场景重调度功能开启时，在推理节点发生故障，或者`Coordinator`与推理节点之间异常断链，导致推理过程异常中断的故障场景，`Coordinator`将推理请求重调度到其他正常的推理节点，继续完成推理任务。

## 配置参数

故障场景重调度功能使用[`user_config.json`](../../configuration/config_reference.md#motor_coordinator_config)配置文件中的以下配置参数：

- 故障场景重调度功能开关：使用`reschedule_config`中的`enable`配置参数，默认为`false`；
  - `false`：故障场景重调度功能关闭；
  - `true`：故障场景重调度功能开启。
- 重调度次数：使用`transport_max_retry`配置参数；
  - 当`transport_max_retry`配置参数为空时，使用`max_retry`配置参数。
- 重调度间隔：使用`retry_delay`参数，浮点值，单位为秒；
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

## 内存使用

故障场景重调度功能开启时，会将**流式请求**的`prompt_tokens`和流式响应的`tokens`缓存在`Coordinator`，在推理任务中占用`Coordinator`的内存，直到推理任务结束释放内存。

以下按照10000并发+1M上下文示例，在故障场景重调度功能开启时，计算`Coordinator`的内存占用上限。
(按照业界经验值，1M token约占用内存3~6M，考虑极端情况，建议按照**系数6**计算。)

多并发场景下：

- 故障重调度功能的内存占用计算公式为：
  - 故障重调度功能的内存占用上限 ≈ 并发数 × 上下文长度 × 系数6
  - 按照以上公式，10000并发+1M上下文应用场景，故障场景重调度功能的内存占用上限为：
  - 故障重调度功能的内存占用上限 ≈ 10000 × 1M × 系数6 ≈ 60G
- 另外考虑基础能力，请求体缓存需要占用内存，考虑长报文极端场景下，同样按照**系数6**计算，请求体缓存的内存占用计算公式为：
  - 请求体缓存的内存占用上限 ≈ 并发数 × 报文长度 × 系数6
  - 按照以上公式，10000并发+1M长报文应用场景，请求体的内存占用上限为：
  - 请求体缓存的内存占用上限 ≈ 10000 × 1M × 系数6 ≈ 60G

因此，`Coordinator`的内存占用上限`> 60G + 60G = 120G`。考虑`Coordinator`基础内存开销和其他功能的内存占用，`Coordinator`实际的内存上限建议设置为`128G`。

当故障场景重调度功能开启时，建议根据`最大并发数`和`上下文长度`，按照上述公式计算内存占用上限，并修改`Coordinator`的内存配置。

- 最大并发数配置，参考[`user_config.json`](../../configuration/config_reference.md#motor_coordinator_config)配置文件中的`max_requests`配置参数；

- `Coordinator`内存占用上限设置，需要修改`yaml`文件中`coordinator`容器中`resources.limits.memory`配置项；
  - 当使用CRD模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/infer_service_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/infer_service_template.yaml)；
  - 当使用Multi模式部署时，`yaml`文件参考[`examples/deployer/yaml_template/coordinator_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/yaml_template/coordinator_template.yaml)。

参考示例如下：

```yaml
containers:
  name: mindie-motor-coordinator
  ...
  resources:
    requests:
      memory: "4Gi"
      cpu: "16"
    limits:
      memory: "128Gi"
      cpu: "64"
```
