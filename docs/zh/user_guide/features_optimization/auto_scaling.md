# 自动弹性扩缩容特性

## 特性介绍

自动弹性扩缩容功能支持根据推理实例的实时负载自动调整 Prefill 和 Decode 实例数量。当请求量上升时自动扩容，当负载回落时自动缩容，在保障服务 SLA 的同时提升资源利用率。

### 工作原理

Infer Operator 为推理实例创建 HPA（Horizontal Pod Autoscaler）资源，HPA 通过 External Metrics Adaptor 或 Prometheus + Prometheus Adapter 获取 MindIE Motor 汇聚的引擎级负载指标（如排队请求数、TPS、KV Cache 使用率等），按用户配置的扩缩容阈值自动调整实例副本数。

```text
┌──────────────────────────────────────────────────────────────────┐
│                         K8s Cluster                              │
│                                                                  │
│   ┌────────────┐     ┌───────────┐     ┌───────────────────┐     │
│   │    HPA     │     │  Infer    │     │    Engine Pods    │     │
│   │(autoscaler)|────>│ Operator  │────>│ (Prefill / Decode)│     │
│   └────────────┘     └───────────┘     └─────────┬─────────┘     │
│         ^                                        │               │
│         │                                        │               │
│         │        ┌──────────────────┐            │               │
│         │        │   MindIE Motor   │            │               │
│         └────────│   Coordinator    │<───────────┘               │
│   (External      │ (aggregation/TPS)│   /metrics                 │
│    Metrics API)  └──────────────────┘                            │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

1. MindIE Motor Coordinator 从所有引擎 Pod 采集 Prometheus 指标，按语义聚合后通过 /metrics 端点暴露。
2. 指标接入组件将 Coordinator 指标转换为 Kubernetes External Metrics API：External Metrics Adaptor 直连 Coordinator；或经 Prometheus 抓取后再由 Prometheus Adapter 注册为 External 指标。
3. HPA 从 External Metrics API 获取负载数据，与用户配置的目标阈值对比。
4. 当指标持续超出阈值时，HPA 通知 Infer Operator 增加副本；低于阈值时减少副本。

### 约束与限制

| 约束维度 | 要求 |
|----------|------|
| 硬件 | <ul><li>Atlas 800I A2推理服务器</li><li>Atlas 800I A3超节点服务器</li></ul> |
| 部署场景 | 自动扩缩容要求 `infer_service_set` 部署模式（由 Infer Operator 创建 HPA）。<ul><li>PD 分离：为 `prefill`、`decode` 角色分别配置 `scalingPolicy`；</li><li>PD 混部：为 `union` 角色配置，且**必须显式指定 `metric`**——该角色没有默认指标，缺失时配置生成直接报错；</li><li>`multi_deployment`、`single_container`：不支持本文自动扩缩容，实例数仅可手动调整。</li></ul>Controller、Coordinator 不在本例的扩缩容范围内。 |
| 软件依赖 | Infer Operator ≥ 26.1.0，且须支持角色级 `scalingPolicy`；指标接入可选择 External Metrics Adaptor 或 Prometheus + Prometheus Adapter。 |
| 其他限制 | <ul><li>缩容存在稳定窗口（默认 5 分钟），避免负载短暂波动导致频繁扩缩；</li><li>扩容的新实例没有 KV Cache 缓存，Prefix Cache 特性会逐步重建缓存，因此新实例的推理性能可能出现小幅度劣化并在一段时间后恢复；</li><li>建议 `minReplicas` 至少设为 1，避免缩容到 0 导致服务完全不可用；</li><li>Counter 类型指标（如 token 总数）不会因 /metrics 请求而重置，建议优先使用 Gauge 类型指标或 MindIE Motor 计算的 TPS 指标作为扩缩容依据；</li><li>若使用不带 `type` 参数的 /metrics 端点（默认 `full`），HPA 获取到的是全局聚合值；按 Prefill/Decode 角色独立扩缩容时，External Metrics Adaptor 需分别请求 `type=role` 视图，Prometheus 路径需在抓取或 PromQL 中按角色聚合。</li></ul> |

## 特性使用

### 环境准备

- 已完成 Infer Operator 的安装部署。安装步骤参见 [Infer Operator 安装指南](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/05_developer_guide/00_installation_deployment/00_manual_installation/07_infer_operator.md)；弹性扩缩容策略配置可参考 [配置基于负载的弹性扩缩容](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/04_usage/10_infer_operator_best_practice/05_configuring_elastic_scaling.md)。
- 已准备 `infer_service_set` 模式的 PD 分离或 PD 混部部署配置，并具备调整实例所需的空闲 NPU 资源。已有服务启用扩缩容时，保持原命名空间、模型、挂载和端口配置。
- 已部署指标接入组件（二选一，均用于将 MindIE Motor 指标转换为 Kubernetes External Metrics）：[External Metrics Adaptor](https://gitcode.com/Ascend/mindcluster-deploy/tree/master/infer-operator-metrics-adaptor)（mindcluster-deploy 提供了可直接部署的示例），或 Prometheus + Prometheus Adapter。

### 使用样例

1. 配置 MindIE Motor 暴露 Metrics。

   MindIE Motor Coordinator 的观测端口默认通过 `/metrics` 暴露指标。以下使用默认端口 1027；从集群外访问时，应换成实际可达的 Service/NodePort 地址，启用 TLS 时同步使用 HTTPS 和证书。

   Coordinator /metrics 端点提供多种聚合视图，通过`type`参数切换：

   | type 值 | 说明 | 适用场景 |
   |---------|------|---------|
   | full（默认） | 全局聚合，所有实例指标聚合为单一值 | Prometheus 抓取、HPA 全局扩缩容 |
   | instance | 实例级指标，注入 `instance_id`、`role` 标签 | 单实例排障 |
   | role | 按角色（Prefill / Decode）聚合 | 按角色独立扩缩容 |

   ```bash
   # 查看全局聚合指标（默认）
   curl http://{coordinator-ip}:1027/metrics

   # 按角色分别查看指标
   curl "http://{coordinator-ip}:1027/metrics?type=role&role=prefill"
   curl "http://{coordinator-ip}:1027/metrics?type=role&role=decode"
   ```

2. 部署指标接入（二选一）。

   两条路径最终都向 HPA 提供 Kubernetes External Metrics API。部署完成后按[步骤 3](#step-2-3)核对指标可用，再配置[步骤 4](#step-3)的 `scalingPolicy`。

   <a id="step-2-1"></a>

   - **External Metrics Adaptor**

      链路：`Coordinator → External Metrics Adaptor → External Metrics API`

      External Metrics Adaptor 周期性从 Coordinator 拉取 Prometheus 格式指标并注册为 External 指标。可使用 [mindcluster-deploy 提供的适配器示例](https://gitcode.com/Ascend/mindcluster-deploy/tree/master/infer-operator-metrics-adaptor) 直接部署，也可按需自行实现。

      配置要点：

      - 正确配置 Coordinator metrics 端点地址与抓取间隔。
      - 按 Prefill/Decode 角色独立扩缩容时，Adaptor 应分别请求 `/metrics?type=role&role=prefill` 与 `/metrics?type=role&role=decode`（Infer Operator 生成的 HPA 会携带角色标签）。

   <a id="step-2-2"></a>

   - **Prometheus + Prometheus Adapter**

      链路：`Coordinator → Prometheus → Prometheus Adapter → External Metrics API`

      1. Prometheus 从 Coordinator 的 `/metrics` 端点采集指标。
      2. Prometheus Adapter 根据 `externalRules` 中的 `metricsQuery` 查询 Prometheus，并将结果注册到 `external.metrics.k8s.io`。
      3. HPA 通过 `type: External` 与 `external.metric.name` 消费该指标。

      `metricsQuery` 是组合多个指标或做时间窗口聚合的配置位置；Motor deployer 只透传 HPA 的 `scalingPolicy`，不参与 PromQL 计算。Adapter 暴露的指标名须与[步骤 4](#step-3) `external.metric.name` 一致；若 HPA 配置了 `kubernetes_namespace` 或 `infer_huawei_com_inferservice_name` selector，Prometheus 时序及 Adapter 规则必须保留对应标签。

3. <a id="step-2-3"></a>使用以下方法验证指标是否可用。

   1. 用 HPA 实际要使用的指标名查询 External Metrics API。指标名如何选取见[应填哪个外部指标名](#应填哪个外部指标名)。
   2. 将查询结果与 Coordinator `/metrics` 上的值核对；返回非空且数值合理后再进入[步骤 4](#step-3)。
   3. External Metrics Adaptor 路径：查询须带 `labelSelector`（InferService 名与角色标签），缺省 selector 时 Adaptor 无法判定 InferService：

      ```bash
      kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/{namespace}/{metric-name}?labelSelector=infer.huawei.com%2Finferservice-name%3D{is-name}%2Cinfer.huawei.com%2Frole-name%3D{prefill|decode}"
      ```

   4. Prometheus Adapter 路径：按 Adapter 注册的指标名与 HPA selector 查询，例如：

      ```bash
      kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/{namespace}/{metric-name}"
      ```

4. <a id="step-3"></a>配置弹性扩缩容策略。

   在 examples/deployer/yaml_template/infer_service_template.yaml 中，为角色配置块添加 `scalingPolicy` 字段（PD 分离用 `prefill`/`decode`，PD 混部用 `union`）。

   以下示例基于 External Metrics Adaptor 路径：Prefill 按排队请求数扩缩容、Decode 按生成 token 速率扩缩容。Prometheus Adapter 路径需在 `external.metric` 下增加 `selector.matchLabels`（见[Prometheus + Prometheus Adapter](#step-2-2)），示例如下：

   ```yaml
   metrics:
   - type: External
     external:
       metric:
         name: motor_prefill_utilization
         selector:
           matchLabels:
             infer_huawei_com_inferservice_name: vllm-0
             kubernetes_namespace: mindie-motor
       target:
         type: Value
         value: "0.8"
   ```

   deployer 生成 YAML 时会将 `kubernetes_namespace` 更新为 `motor_deploy_config.job_id`，将 `infer_huawei_com_inferservice_name` 更新为 `<InferServiceSet 名>-0`。

   ```yaml
   roles:
     # ========== Prefill 角色 ==========
     - name: prefill
       replicas: 4
       workload:
         apiVersion: apps/v1
         kind: StatefulSet
       scalingPolicy:            # 新增：弹性扩缩容策略
         type: HPA
         spec:
           minReplicas: 1
           maxReplicas: 4
           metrics:
           - type: External
             external:
               metric:
                 # 外部指标名：填指标接入组件能取到值的名称（本例 Adaptor 直取原名）
                 name: "vllm:num_requests_waiting"
               target:
                 type: AverageValue
                 averageValue: "5"
       metadata:
         labels:
           infer.huawei.com/gang-schedule: 'true'
       spec:
         # ... 其余配置保持不变 ...

     # ========== Decode 角色 ==========
     - name: decode
       replicas: 4
       workload:
         apiVersion: apps/v1
         kind: StatefulSet
       scalingPolicy:            # 新增：弹性扩缩容策略
         type: HPA
         spec:
           minReplicas: 1
           maxReplicas: 4
           metrics:
           - type: External
             external:
               metric:
                 # 外部指标名：填指标接入组件能取到值的名称（本例 Adaptor 直取原名）
                 name: "motor:generation_tokens_per_second"
               target:
                 type: AverageValue
                 averageValue: "10"
       metadata:
         labels:
           infer.huawei.com/gang-schedule: 'true'
       spec:
         # ... 其余配置保持不变 ...
   ```

   scalingPolicy 参数说明：

   | 参数 | 说明 | 取值 |
   |------|------|------|
   | scalingPolicy.type | 弹性扩缩容策略类型 | 当前仅支持 HPA |
   | scalingPolicy.spec.minReplicas | 缩容下限，实例数不会低于此值 | 正整数 |
   | scalingPolicy.spec.maxReplicas | 扩容上限，实例数不会超过此值 | 正整数，且 ≥ `minReplicas` |
   | scalingPolicy.spec.metrics[].type | 指标类型 | External（由 External Metrics Adaptor 或 Prometheus Adapter 提供） |
   | scalingPolicy.spec.metrics[].external.metric.name | 外部指标名称 | 需与所选指标接入组件暴露的指标名一致；验证方法见[步骤 3](#step-2-3) |
   | scalingPolicy.spec.metrics[].external.metric.selector | 指标选择器 | 仅 Prometheus Adapter 场景需要；External Metrics Adaptor 场景无需新增 |
   | scalingPolicy.spec.metrics[].external.metric.selector.matchLabels.kubernetes_namespace | 指标所属命名空间 | 仅 Prometheus Adapter 场景配置；模板包含此标签时，deployer 生成 YAML 会将其更新为 `motor_deploy_config.job_id` |
   | scalingPolicy.spec.metrics[].external.metric.selector.matchLabels.infer_huawei_com_inferservice_name | 指标所属 InferService | 仅 Prometheus Adapter 场景配置；模板包含此标签时，deployer 按 `<InferServiceSet 名>-0` 自动更新 |
   | scalingPolicy.spec.metrics[].external.target.type | 目标值类型 | Value / AverageValue。总请求数、总 TPS 配合 AverageValue；已取平均的 KV 使用率、容量利用率配合 Value，避免再次按副本数分摊 |
   | scalingPolicy.spec.metrics[].external.target.value / averageValue | 目标阈值 | Value 使用 value，AverageValue 使用 averageValue；依据指标接入组件最终输出的量纲设定 |

   HPA 由 Infer Operator 托管：直接 `kubectl patch` 修改 HPA 会被 Operator 回滚，须通过模板 `scalingPolicy` 或 `user_config.json` 的 `scaling_policy` 修改。

5. 生成并应用扩缩容配置。

   <a id="step-4"></a>

   修改本地模板不会更新已部署资源。使用与原部署一致的配置先生成 YAML，并检查输出中的 namespace、角色、副本上下限、指标名和 selector：

   ```bash
   cd examples/deployer
   python deploy.py --config_dir ../infer_engines/vllm --dry-run
   ```

   生成文件位于 `output_yamls/infer_service.yaml`。检查无误后，在计划的变更窗口通过部署入口应用配置：

   ```bash
   python deploy.py --config_dir ../infer_engines/vllm
   ```

   上述第二条命令会变更集群资源。`--update_instance_num` 仅用于手动调整实例数，不重新读取模板，不能用它代替本次新增策略的生成步骤。启用 HPA 后由 HPA 管理角色副本数，避免另一自动化程序同时持续写入同一角色的 replicas。

### 验证特性

1. 查看 HPA 状态。

   ```bash
   kubectl get hpa -n {namespace}
   ```

   以下为输出格式示例，资源名称和值以本集群实际结果为准：

   ```text
   NAME                     REFERENCE                    TARGETS          MINPODS   MAXPODS   REPLICAS   AGE
   vllm-0-prefill-scaler    InstanceSet/vllm-0-prefill   116m/800m        1         4         1          34m
   vllm-0-decode-scaler     InstanceSet/vllm-0-decode    897m/800m        1         4         2          34m
   ```

   - REFERENCE 列指向 Infer Operator 生成的角色工作负载，模板里角色的 `workload` 写法与实际资源类型不一定同名，以本集群实际输出为准。
   - TARGETS 列显示当前值/目标值，按 K8s 毫单位呈现（`800m` 即 0.8）；实际扩缩容还受容差、稳定窗口、副本上下限和资源供给约束。出现 `<unknown>` 时先排查指标链路与 Infer Operator 版本。
   - REPLICAS 列显示当前实际副本数。

2. 模拟负载触发扩容。

   发送大量并发推理请求，观察 HPA 是否自动扩容：

   ```bash
   # 并发发送请求
   for i in {1..100}; do
     curl -X POST "http://{service-ip}:31015/v1/chat/completions" \
       -H "Content-Type: application/json" \
       -d '{"model": "your-model", "max_tokens": 100, "messages": [{"role": "user", "content": "Hello"}]}' &
   done
   ```

   一次短请求突发可能短于采集/HPA 同步周期；验证时应维持足够长的负载，并确认实际指标越过目标。随后查看 HPA 状态和新增引擎 Pod（持续 watch 与 Pod 查询可在两个终端分别运行）：

   ```bash
   kubectl get hpa -n {namespace} --watch
   kubectl get pod -n {namespace} | grep -E "prefill|decode"
   ```

3. 验证缩容。

   停止负载后，观察指标和副本数回落。默认缩容稳定窗口为 5 分钟，若策略覆盖了窗口则以实际配置为准。只有全部有效指标的建议允许缩容时，才可能逐步回到 `minReplicas`；有指标取值失败或 KV 等指标仍偏高时，不保证回到下限：

   ```bash
   kubectl get hpa -n {namespace} --watch
   ```

## 调优建议

MindIE Motor /metrics 端点提供了丰富的引擎级指标，下表列出推荐用于自动扩缩容的关键指标：

### Prefill 扩缩容推荐指标

| 指标名 | 类型 | 说明 | 推荐阈值建议 |
|--------|------|------|-------------|
| vllm:num_requests_waiting | Gauge | 等待调度的请求数 | 由单一目标值决定两个方向，不分别设扩容/缩容阈值。示例按 Pod 平均取 target=5：高于目标扩容、低于目标缩容；HPA 默认 10% 容差内的偏差不触发动作 |
| vllm:num_requests_running | Gauge | 当前运行中的请求数 | 视 NPU 规格和模型而定 |
| vllm:kv_cache_usage_perc | Gauge | KV Cache 使用率（0-1） | 示例 target=0.8：高于目标扩容、低于目标缩容 |
| motor:prompt_tokens_per_second | Gauge | Prompt token 处理速率（Motor 计算） | 按 SLA 目标设定 |
| vllm:time_to_first_token_seconds | Histogram | 首 token 延迟（TTFT） | 按 SLA 目标（如 p95 < 500ms） |

### Decode 扩缩容推荐指标

| 指标名 | 类型 | 说明 | 推荐阈值建议 |
|--------|------|------|-------------|
| vllm:num_requests_waiting | Gauge | 等待调度的请求数 | 同 Prefill：单一目标值决定两个方向，示例 target=5 按 Pod 平均 |
| vllm:num_requests_running | Gauge | 当前运行中的请求数 | 视 NPU 规格和模型而定 |
| motor:generation_tokens_per_second | Gauge | 生成 token 速率（Motor 计算） | 按 SLA 目标设定 |
| vllm:e2e_request_latency_seconds | Histogram | 端到端请求延迟 | 按 SLA 目标（如 p95 < 2s） |
| vllm:time_per_output_token_seconds | Histogram | 跨 token 延迟（TPOT） | 按 SLA 目标（如 p95 < 50ms） |

> [!NOTE] 说明
>
>- `motor:prompt_tokens_per_second` 和 `motor:generation_tokens_per_second` 是 MindIE Motor Coordinator 计算的服务级指标，基于 vLLM 原始 counter 计算 delta rate 得到，更准确反映实时吞吐。
>- Histogram 类型指标（如 `vllm:e2e_request_latency_seconds`）需要在指标接入组件侧计算分位数（p50/p95/p99）后作为独立指标暴露。
>- 建议为 Prefill 和 Decode 分别配置不同的扩缩容指标，以匹配各自的计算特征（Prefill 为计算密集型，Decode 为访存密集型）。
>- 表中指标需先确认在当前模型、`max_model_len` 和负载下确有区分度：请求无排队时 `vllm:num_requests_waiting` 会长期为 0，短序列下 `vllm:kv_cache_usage_perc` 也接近 0，这类指标无法驱动扩缩容，应改用[容量规划利用率](#容量规划利用率)。

### 多指标组合策略

可在 HPA 中配置多个指标。HPA 分别计算各指标的期望副本数并取最大值：例如分别建议 2 和 6 个副本时，采用 6，再受上下限等约束。若某个指标获取失败，其他指标仅建议缩容，HPA 会跳过缩容；有效指标建议扩容时仍可扩容。参见 [Kubernetes HPA 算法说明](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/)。

下例的 KV 使用率由 Coordinator 聚合为均值，要求指标接入组件保持该语义，因此使用 `Value`：

```yaml
scalingPolicy:
  type: HPA
  spec:
    minReplicas: 1
    maxReplicas: 4
    metrics:
    - type: External
      external:
        metric:
          name: "vllm:num_requests_waiting"
        target:
          type: AverageValue
          averageValue: "5"
    - type: External
      external:
        metric:
          name: "vllm:kv_cache_usage_perc"
        target:
          type: Value
          value: "0.8"
```

### 容量规划利用率

当前 Motor 还提供 `motor_prefill_utilization` 和 `motor_decode_utilization`，分别表示各角色的需求/供给比，计算式为 `demand_tps / (活跃实例数 × 单实例容量 capacity_tps)`，冒号原始名与无冒号别名同时输出。它们已按当前实例数归一化，HPA 应使用 `Value`，初始目标可设为 `0.8`。相关容量先验、标定状态与计算方式见 [监控接口](../api/metrics_interfaces.md)。未标定时该利用率不会输出（容量为 0 或没有活跃实例时同样不输出），所以不要把“指标缺失”当作零负载；反过来，**指标非零也不代表当前有负载**：需求侧是 EMA，空闲时不会衰减到 0（实测空闲时该值可长期保持同一非零值），且标定早期容量估计偏小时利用率会虚高，可能在没有负载时也触发一次扩容、随后容量被抬高再按稳定窗口缩回。可先用 `motor:capacity_calibrated{role="prefill"}` 与 `motor:capacity_calibrated{role="decode"}` 确认对应角色是否已标定（1 为已标定）；该指标只在默认 `full` 视图输出，按角色请求的 `type=role` 视图里没有它，且按 role 标签成对出现，角色从未出现过时本身也缺失。

除修改 YAML 模板外，也可在 `user_config.json` 顶层加入以下配置，由 deployer 生成策略；同一角色的该配置会覆盖模板中的 scalingPolicy，请选择一种方式维护：

```json
{
  "scaling_policy": {
    "prefill": {"min_replicas": 1, "max_replicas": 4, "target": 0.8},
    "decode": {"min_replicas": 1, "max_replicas": 4, "target": 0.8}
  }
}
```

默认指标分别为上述 P/D 利用率，默认类型为 `Value`。生成器同时设置 `kubernetes_namespace` 与 `infer_huawei_com_inferservice_name` selector，Adaptor/Prometheus 时序及查询规则必须提供对应标签，且取值匹配生成结果；否则即使指标注册成功，HPA 也可能查询不到值。仅修改模板时，生成器只回填模板已配置的这两个标签，不自动补齐缺失标签。

## 常见问题

### 已修改模板，但没有 HPA

**问题描述**

已修改 `infer_service_template.yaml` 或 `user_config.json` 并执行 deploy，但集群中未出现 HPA 资源。

**原因分析**

1. 部署模式为 `multi_deployment` 或 `single_container`，不支持 HPA 自动扩缩容。
2. 仅修改本地模板，未执行[步骤 4](#step-4)生成与应用 YAML。
3. Infer Operator 版本过低或未正确创建 `scalingPolicy`。
4. 直接 `kubectl patch` HPA 后被 Operator 回滚。

**解决步骤**

1. 确认服务运行于 `infer_service_set` 模式；不支持的模式改见[手动扩缩容](../features/manual_scaling.md)。
2. 执行[步骤 4](#step-4)生成并应用配置，检查 `output_yamls/infer_service.yaml` 中各角色是否包含 `scalingPolicy`。
3. 检查 InferServiceSet 实际资源、Infer Operator 事件与日志。

### 应填哪个外部指标名

**问题描述**

不确定 `scalingPolicy.spec.metrics[].external.metric.name` 应填 Coordinator `/metrics` 上的原始名，还是 Adapter 暴露的外部指标名。

**原因分析**

1. External Metrics Adaptor 与 Prometheus Adapter 可能对原始 Prometheus 名做映射或重命名。
2. Motor 契约指标有无冒号别名（如 `motor_prefill_utilization`），非契约指标通常只有含冒号的原始序列。
3. mindcluster-deploy 的 Adaptor 示例按名称字面查找、不做冒号→下划线映射；填错名称可能静默返回 0。

**解决步骤**

1. 部署后按[步骤 2.3](#step-2-3)查询 External Metrics API，以实际返回值为准填写 `external.metric.name`。
2. 契约指标集合见[监控接口](../api/metrics_interfaces.md)；使用 mindcluster-deploy Adaptor 示例时，非契约指标须填带冒号的原始名（如 `vllm:num_requests_waiting`）。
3. 按角色扩缩容时，External Metrics Adaptor 走 `type=role` 视图；Prometheus 路径须在抓取或 PromQL 中按角色聚合，并在 HPA 侧用 selector 限定本服务。

### TARGETS 显示 unknown，或提示 FailedGetExternalMetric

**问题描述**

HPA 的 TARGETS 列显示 `<unknown>`，或事件中出现 `FailedGetExternalMetric`。

**原因分析**

1. External Metrics API 不可用，或 APIService 未注册。
2. External Metrics Adaptor 路径缺省 `labelSelector`，查询直接失败。
3. Prometheus Adapter 路径的抓取、`externalRules` 与 HPA `selector.matchLabels` 不一致。
4. 外部指标名与接入组件映射规则不匹配（mindcluster-deploy Adaptor 对错误名静默返回 0）。
5. 容量类指标尚未完成标定。

**解决步骤**

1. 确认指标 API 可用，并按 HPA 使用的指标名和 selector 查询具体值：

   ```bash
   kubectl get apiservice v1beta1.external.metrics.k8s.io
   kubectl describe hpa "<hpa-name>" -n "<namespace>"
   kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/<namespace>/<metric-name>?labelSelector=infer.huawei.com%2Finferservice-name%3D<is-name>%2Cinfer.huawei.com%2Frole-name%3D<role>"
   ```

2. 确认返回 items 非空、时间戳持续更新、标签匹配 HPA selector。
3. 取值恒为 0 时，核对指标名映射、服务标签、角色筛选及采集端点；容量指标检查标定状态。

### 有负载但没有扩容

**问题描述**

业务负载已上升，但 HPA 未增加副本数。

**原因分析**

1. 当前指标未超过目标值及 HPA 默认 10% 容差。
2. 负载持续时间短于指标采集与 HPA 同步周期。
3. HPA 已达 `maxReplicas`。
4. HPA 已提高目标副本数，但新增 Pod 因 NPU 资源不足等原因 Pending。

**解决步骤**

1. 维持足够长负载，确认实际指标越过目标；对照[步骤 2.3](#step-2-3)核对 External Metrics 取值。
2. 查看 HPA TARGETS 与 REPLICAS 列，区分“未决策”与“已决策但 Pod Pending”。
3. Pending 时检查 NPU 资源、节点选择和调度事件。

### 停止请求后没有缩容到下限

**问题描述**

停止发送请求后，实例数长期未回落到 `minReplicas`。

**原因分析**

1. 缩容稳定窗口（默认 5 分钟）尚未结束。
2. 多指标策略中仍有指标建议维持较高副本数。
3. 某个指标获取失败，HPA 跳过缩容。
4. KV Cache 使用率等指标因缓存保留长期偏高，不等同于“无活跃请求”。

**解决步骤**

1. 观察足够长时间，确认稳定窗口已过。
2. 检查 HPA 各指标 TARGETS 与事件，排查指标获取失败。
3. 按希望控制的容量目标选择指标，避免把“没有请求”等同于“所有指标都低”。

### 多个 Motor 服务读到相同指标

**问题描述**

多个 Motor 服务的 HPA 读到相同或混淆的指标值，扩缩容行为互相干扰。

**原因分析**

1. 指标接入组件查询范围过大，混入其他服务或相反角色的指标。
2. HPA 未配置或未正确匹配 `kubernetes_namespace` 与 `infer_huawei_com_inferservice_name` selector。
3. 使用全局聚合 `/metrics` 视图而未按服务/角色隔离。

**解决步骤**

1. 检查指标接入组件的查询范围及 HPA selector。
2. 为每个服务保留命名空间和 InferService 标签，核对生成 YAML 中的实际标签值。
3. 按角色扩缩容时使用 `type=role` 视图或等效的 PromQL 聚合，避免全局聚合混入其他服务。
