# 实例重调度

## 特性介绍

实例重调度用于在推理实例的Pod或所在节点发生不可恢复故障时，自动回收故障实例并在健康节点上创建新实例，减少人工介入和服务容量损失。该能力由MindIE Motor与Kubernetes、MindCluster协同完成：Motor负责发现实例异常、从调度池隔离故障实例并使异常Pod退出，MindCluster负责回收原实例、重新分配NPU资源并创建Pod，Motor再完成新实例的注册、组装和引擎拉起。

实例重调度与[请求重调度](./rescheduler.md)作用于不同层级。实例重调度恢复推理实例容量，请求重调度将受影响的推理请求转发到其他健康实例。两项能力可以同时使用，以缩短故障期间的请求中断时间。

> [!NOTE] ✏️ 说明
> Motor当前部署模板默认为推理Pod配置`fault-scheduling: grace`和`fault-retry-times: "10000"`。使用Infer Operator管理工作负载时，`fault-scheduling`还支持由Infer Operator接管Pod删除的`external-*`模式。实例重调度不需要在`user_config.json`中单独开启。

### 工作原理

1. NodeManager持续监控本Pod内的推理引擎状态，并向Controller上报实例心跳。Controller也会通过MindCluster维护的节点和NPU故障信息感知硬件故障。
2. 当Controller确认实例异常时，将实例标记为`INACTIVE`并通知Coordinator移除该实例，避免新请求继续调度到故障实例。对于部分Pod失联的多Pod实例，Controller还会通知仍可达的NodeManager退出，避免同一集合通信组的新旧Pod并存。
3. 对于引擎进程故障，Motor优先尝试已启用的轻量恢复策略，例如原地重拉引擎。恢复失败或不满足轻量恢复条件时，NodeManager以非正常状态退出，由K8s和MindCluster进入实例重调度流程。
4. MindCluster根据`fault-scheduling`和`fault-retry-times`处理故障实例，删除原实例Pod，重新选择满足资源、亲和性和硬件类型约束的健康节点，并创建新Pod。
5. 新Pod中的NodeManager向Controller注册。Controller按`job_name`和角色重新组装实例，为新实例分配实例ID，并向所有NodeManager下发启动命令。
6. NodeManager拉起推理引擎并恢复心跳。实例全部端点就绪后，状态从`INITIAL`变为`ACTIVE`，Controller将新实例重新发布给Coordinator，恢复请求调度。

如果故障节点上的NPU仍被标记为不可用，新实例不会重新调度到该故障资源。实际重调度位置由Kubernetes和MindCluster根据资源余量、节点标签、亲和性及故障隔离状态决定。

### 约束与限制

| 约束维度   | 要求                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 部署方式   | 仅支持K8s部署，依赖MindCluster管理推理任务。Docker only部署没有实例调度器，不支持实例重调度。                                                                                                                                                                                                                                                                                                                                                                                              |
| 推理引擎   | 支持vLLM和SGLang。实例重调度位于容器和实例生命周期层，不依赖请求协议。                                                                                                                                                                                                                                                                                                                                                                                                                     |
| 软件依赖   | 需要安装与当前Motor版本配套的Kubernetes、Volcano、Ascend Device Plugin、ClusterD和MindCluster Infer Operator。配套版本请参见[版本配套说明](../../../release_note_motor.md)。                                                                                                                                                                                                                                                                                                                |
| 资源要求   | 集群中必须存在满足实例NPU数量、硬件型号、节点标签、亲和性和网络拓扑要求的健康资源。资源不足时，新Pod会保持`Pending`，实例无法恢复。                                                                                                                                                                                                                                                                                                                                                      |
| 多Pod实例  | 实例级重调度需要回收并重建完整实例。直接删除P/D多Pod实例中的单个Pod不保证触发完整实例重调度，也可能导致剩余Pod等待集合通信。验证故障恢复时应通过真实故障或按部署平台支持的实例级方式触发。                                                                                                                                                                                                                                                                                                 |
| RoCE网络   | RoCE网络未配置或网络不健康时，非超节点调度场景下的单机推理实例仍可能正常完成调度，但推理实例之间的KV传输可能异常，导致推理任务无法正常运行。超节点调度场景下，逻辑超节点数量为1时也存在相同风险。实例处于`Running`或已完成重调度不代表KV传输链路正常，详细说明请参见MindCluster 26.0文档《[部署MindIE Motor推理任务](https://www.hiascend.com/document/detail/zh/mindcluster/2600/clustersched/dlug/docs/zh/scheduling/usage/mindie_motor_best_practice/01_deploying_mindie_motor.md)》。 |
| 业务连续性 | 故障实例从调度池移除到新实例`ACTIVE`期间，该实例不承接新请求。若没有其他健康实例，推理接口可能暂时不可用；已在故障实例上执行的请求是否续推取决于请求重调度配置。                                                                                                                                                                                                                                                                                                                         |
| 状态数据   | 实例重调度会创建新Pod并重新拉起引擎，Pod本地的非持久化数据不会保留。模型权重、配置、日志及KV Cache等需要按业务要求使用共享存储或持久卷。                                                                                                                                                                                                                                                                                                                                                   |
| 其他特性   | 容器快照可以缩短重调度后的引擎恢复时间；PR/DT异构部署下，Prefill只能重调度到PR节点，Decode只能重调度到DT节点。对应类型没有空闲节点时无法恢复。                                                                                                                                                                                                                                                                                                                                             |

## 特性使用

### 环境准备

- 已按照[环境准备](../../environment_preparation.md)完成Kubernetes、Volcano和MindCluster组件安装，Infer Operator运行正常。
- 已使用Motor提供的K8s部署方式创建推理服务，Controller、Coordinator和推理实例均已就绪。
- 集群中有满足故障实例资源和调度约束的空闲健康节点；多Pod实例还需要一次性满足完整实例的资源需求。
- 如果需要在恢复期间继续处理请求，至少保留一个同角色的健康实例，并根据业务需要开启[请求重调度](./rescheduler.md)。

### 使用样例

Motor的推理Pod模板默认包含以下标签：

```yaml
spec:
  template:
    metadata:
      labels:
        fault-scheduling: grace
        fault-retry-times: "10000"
```

参数说明如下。完整的版本化定义请参见MindCluster最新《[任务YAML配置说明](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/06_api/15_yaml_configuration.md)》。其中，Deployment及Infer Operator工作负载支持多个`fault-scheduling`取值：

| 参数                  | 取值                          | 说明                                                                                                                                                                                                                                                     |
| --------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fault-scheduling`  | `grace`                     | 由Volcano先优雅删除原Pod。                                                                                                                                                                                                                               |
| `fault-scheduling`  | `force`                     | 由Volcano强制删除原Pod。                                                                                                                                                                                                                                 |
| `fault-scheduling`  | `external-force`            | Volcano只将故障原因写入Pod annotation，由Infer Operator等外部组件感知故障并强制删除原Pod，适用于由Infer Operator负责实例级重调度的场景。                                                                                                                 |
| `fault-scheduling`  | `external-grace`            | Volcano只将故障原因写入Pod annotation，由Infer Operator等外部组件感知故障并优雅删除原Pod。                                                                                                                                                               |
| `fault-scheduling`  | `external-force-pod-failed` | Volcano只标注故障，由Infer Operator等外部组件处理；仅在业务面故障，即Pod内进程非零退出时强制删除原Pod，硬件故障时不删除故障节点上的Pod。Motor在Controller和引擎侧DP故障恢复开关同时开启时，会为对应推理Pod生成该值，并同时配置`pod-rescheduling: on`。 |
| `fault-scheduling`  | `off`、未配置或其他值       | 不启用MindCluster断点续训故障处理，K8s工作负载自身的重试机制仍可能生效。                                                                                                                                                                                 |
| `fault-retry-times` | 大于`0`的整数               | 业务面故障允许无条件重试的次数。使用该能力要求进程发生业务面故障时以非零状态退出；未配置或配置为`0`时，不启用业务面故障无条件重试。Motor模板默认值为`10000`。                                                                                        |

如需确认部署结果，执行以下命令：

```bash
kubectl get pod -n {namespace} \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.fault-scheduling}{"\t"}{.metadata.labels.fault-retry-times}{"\n"}{end}'
```

使用Motor默认模板时，推理实例Pod的标签值应显示为`grace`和`10000`；启用会自动生成Infer Operator外部处理标签的DP故障恢复能力时，`fault-scheduling`应显示为`external-force-pod-failed`，并同时存在`pod-rescheduling: on`。Controller、Coordinator等非推理实例Pod可以没有这些标签。

NodeManager的`fault_tolerance_config.container_restart_managed`用于控制异常引擎持续无法恢复时是否允许NodeManager退出并交给外部调度器恢复。默认值为`null`，在K8s Pod内自动解析为开启，通常无需显式配置。若配置为`false`，Motor会保留NodeManager进程，实例重调度兜底不会由该异常路径触发。

### 验证特性

> [!WARNING] ⚠️ 警告
> 以下操作会中断目标实例上的请求。请仅在测试环境执行，并确保集群有足够的备用资源。不要通过删除多Pod实例中的单个Pod代替实例级故障验证。

1. 记录验证前的实例Pod、Pod IP和所在节点。

   ```bash
   kubectl get pod -n {namespace} -o wide
   ```

2. 按当前MindCluster版本支持的实例级故障注入或实例回收方式触发重调度。故障节点应已被故障管理组件标记为不可调度，避免新实例再次使用同一故障资源。
3. 观察旧Pod退出和新Pod创建过程。

   ```bash
   kubectl get pod -n {namespace} -w
   ```

4. 检查Controller日志。日志中应依次出现旧实例隔离或移除、新NodeManager注册、实例组装以及新实例进入`ACTIVE`的记录。

   ```bash
   kubectl logs -n {namespace} {controller-pod-name} -f
   ```

5. 对比Pod UID或创建时间，确认故障实例已被重新创建。所有新Pod均为`Running`且就绪后，发送推理请求验证服务恢复。

## 常见问题

### 故障后没有创建新Pod

**问题描述**

故障实例已经退出，但长时间没有新Pod创建。

**原因分析**

可能存在以下原因：

- 推理Pod未配置`fault-scheduling`或`fault-retry-times`，或者标签值不受当前MindCluster版本支持。
- Infer Operator、Volcano、ClusterD或Ascend Device Plugin异常。
- 工作负载的重调度次数已经耗尽。
- 使用Docker only方式部署，没有可执行实例重调度的集群调度器。

**解决步骤**

1. 检查推理Pod上的`fault-scheduling`和`fault-retry-times`标签。
2. 检查Infer Operator、Volcano、ClusterD和Ascend Device Plugin的Pod状态及日志。
3. 查看推理工作负载和Pod事件，确认是否存在重试次数耗尽或调度失败信息。
4. 确认使用的是K8s部署方式。

### 新Pod一直处于Pending状态

**问题描述**

MindCluster已经创建新Pod，但Pod长时间处于`Pending`状态。

**原因分析**

集群中没有同时满足NPU数量、硬件型号、节点标签、亲和性、网络拓扑和故障隔离要求的健康资源。PR/DT异构场景还要求对应角色使用匹配的节点类型。

**解决步骤**

1. 执行以下命令查看Pod调度事件。

   ```bash
   kubectl describe pod -n {namespace} {pod-name}
   ```

2. 检查目标节点的NPU余量、污点与容忍、`nodeSelector`、Pod反亲和及故障隔离状态。
3. 为对应角色释放或扩充满足约束的节点资源。Decode资源不足且满足使用条件时，可以参考[ScaleP2D故障恢复](./scale_p2d.md)。

### 删除一个Pod后实例没有完整重调度

**问题描述**

直接删除多Pod Prefill或Decode实例中的一个Pod后，只重建了单个Pod，或者实例一直无法重新就绪。

**原因分析**

直接删除单个Pod属于Pod生命周期操作，不等同于MindCluster的实例级故障恢复。多Pod实例需要保持端点、Rank Table和集合通信组一致，单Pod重建不能保证触发整实例回收。

**解决步骤**

1. 停止使用删除单个Pod的方式验证实例重调度。
2. 按当前MindCluster版本支持的实例级故障注入或实例回收流程重新验证。
3. 检查Controller日志，确认旧实例已经从Coordinator隔离，并且新实例完成全部NodeManager注册和组装。

### 新实例已运行但推理服务仍不可用

**问题描述**

新Pod均为`Running`，但推理接口仍返回服务不可用。

**原因分析**

`Running`只表示容器已启动，不表示推理引擎和完整实例已经就绪。新实例可能仍在加载模型、等待其他Pod注册，或者因端点数量、Rank Table、网络连通性问题停留在`INITIAL`状态。

**解决步骤**

1. 检查新实例所有NodeManager和推理引擎日志，确认模型加载成功。
2. 检查Controller日志，确认实例已从`INITIAL`变为`ACTIVE`。
3. 对多Pod实例确认所有Pod均已注册，端点数量与并行配置一致。
4. 检查Controller到Coordinator、Controller到NodeManager以及实例内部的网络连通性。
