# 自动弹性扩缩容

## 特性介绍

自动弹性扩缩容功能支持根据推理实例的实时负载自动调整 Prefill 和 Decode 实例数量。当请求量上升时自动扩容，当负载回落时自动缩容，在保障服务 SLA 的同时提升资源利用率。

核心机制：Infer Operator 为推理实例创建 HPA（Horizontal Pod Autoscaler）资源，HPA 通过 External Metrics Adaptor 获取 MindIE Motor 汇聚的引擎级负载指标（如排队请求数、TPS、KV Cache 使用率等），按用户配置的扩缩容阈值自动调整实例副本数。

## 原理说明

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

1. MindIE Motor Coordinator 从所有引擎 Pod 采集 Prometheus 指标，按语义聚合后通过 `/metrics` 端点暴露。
2. External Metrics Adaptor 周期性从 Coordinator 拉取指标，转换为 Kubernetes External Metrics API。
3. HPA 从 External Metrics API 获取负载数据，与用户配置的目标阈值对比。
4. 当指标持续超出阈值时，HPA 通知 Infer Operator 增加副本；低于阈值时减少副本。

## 支持的产品型号

- Atlas 800I A2 推理服务器
- Atlas 800I A3 超节点服务器

## 前置条件

- 已完成 Infer Operator 的安装部署。
- 已完成 MindIE Motor 推理服务的部署（PD 分离或 PD 混部模式）。
- 已部署 External Metrics Adaptor，用于将 MindIE Motor 指标转换为 Kubernetes External Metrics。可直接使用 [mindcluster-deploy 提供的 Metrics Adaptor 示例](https://gitcode.com/Ascend/mindcluster-deploy/tree/master/infer-operator-metrics-adaptor)进行部署。

## 配置 MindIE Motor 暴露 Metrics

MindIE Motor Coordinator 默认通过 `/metrics` 端点暴露聚合后的引擎指标，无需额外配置即可使用。

Coordinator `/metrics` 端点提供多种聚合视图，通过 `type` 参数切换：

| type 值 | 说明 | 适用场景 |
|---------|------|---------|
| `full`（默认） | 全局聚合，所有实例指标聚合为单一值 | Prometheus 抓取、HPA 全局扩缩容 |
| `instance` | 实例级指标，注入 `instance_id`、`role` 标签 | 单实例排障 |
| `role` | 按角色（Prefill / Decode）聚合 | 按角色独立扩缩容 |

```bash
# 查看全局聚合指标（默认）
curl http://{coordinator-ip}:1027/metrics

# 按角色分别查看指标
curl http://{coordinator-ip}:1027/metrics?type=role&role=prefill
curl http://{coordinator-ip}:1027/metrics?type=role&role=decode
```

## 部署 External Metrics Adaptor

External Metrics Adaptor 负责将 Coordinator 的 Prometheus 格式指标转换为 Kubernetes External Metrics API，供 HPA 消费。

可使用 [mindcluster-deploy 提供的适配器示例](https://gitcode.com/Ascend/mindcluster-deploy/tree/master/infer-operator-metrics-adaptor) 直接部署，也可按需自行实现。

部署前需确认 Adaptor 配置了正确的 Coordinator metrics 端点地址和抓取间隔。部署完成后，执行以下命令验证指标可用：

```bash
kubectl get --raw /apis/external.metrics.k8s.io/v1beta1 | grep -E "motor_prefill_utilization|motor_decode_utilization"
```

## 部署 Prometheus Adapter

Prometheus Adapter 是新增且与 External Metrics Adaptor 并行支持的指标接入路径：

```text
MindIE Motor Coordinator -> Prometheus -> Prometheus Adapter -> External Metrics API -> HPA
```

1. Prometheus 从 Coordinator 的 `/metrics` 端点采集指标。
2. Prometheus Adapter 根据 `externalRules` 中的 `metricsQuery` 查询 Prometheus，并将结果注册到 `external.metrics.k8s.io`。
3. HPA 使用 `type: External` 和 `external.metric.name` 查询该指标，Infer Operator 据此调整 Prefill 或 Decode 副本数。

### 配置 Prometheus 采集 Coordinator 指标

若 Prometheus 使用 Kubernetes Service 发现，可为 `mindie-motor-coordinator-obs` Service 添加以下 annotations，使 Prometheus 抓取其 endpoints：

```yaml
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/path: "/metrics"
    prometheus.io/port: "1027"
```

`1027` 是 Coordinator metrics Service 的默认端口；如果部署时修改了该端口，应同步修改 annotation。Prometheus 的抓取配置还需启用对应的 Kubernetes Service 发现规则。

### 配置 Prometheus Adapter External 指标

Prometheus Adapter 的 ConfigMap 使用 `externalRules` 将 Prometheus 指标映射为 HPA 可用的 External 指标。下面的示例将等待请求数和运行中请求数之和暴露为 `vllm_total_requests`：

```yaml
rules:
  externalRules:
    - seriesQuery: '{__name__=~"vllm:(num_requests_waiting|num_requests_running)"}'
      resources:
        overrides:
          kubernetes_namespace:
            resource: namespace
          kubernetes_pod_name:
            resource: pod
      name:
        matches: "^.*$"
        as: "vllm_total_requests"
      metricsQuery: |
        sum(
          vllm:num_requests_waiting{<<.LabelMatchers>>}
          +
          vllm:num_requests_running{<<.LabelMatchers>>}
        ) by (kubernetes_namespace, kubernetes_pod_name)
```

`metricsQuery` 是复杂触发规则的配置位置。可以在其中按业务需要组合多个指标、计算比值，或使用 `avg_over_time` 等时间窗口函数；Motor deployer 只透传 HPA 的 `scalingPolicy`，不参与 PromQL 的计算。

Adapter 暴露的指标名称必须与 `scalingPolicy.spec.metrics[].external.metric.name` 一致。例如上述规则应配合 `name: vllm_total_requests` 使用。若 HPA 配置了 `kubernetes_namespace` 或 `infer_huawei_com_inferservice_name` selector，Prometheus 时序及 Adapter 规则必须保留对应标签；否则 HPA 无法匹配目标指标。

完成部署后，可查询 External Metrics API 验证新路径：

```bash
kubectl get --raw "/apis/external.metrics.k8s.io/v1beta1/namespaces/{namespace}/vllm_total_requests"
```

## 配置弹性扩缩容策略

在 `examples/deployer/yaml_template/infer_service_template.yaml` 中，为 Prefill 和 Decode 角色的配置块下添加 `scalingPolicy`。

使用原有 External Metrics Adaptor 路径时，沿用原有 `scalingPolicy` 配置即可，`external.metric` 下**无需新增** `selector`。

使用 Prometheus Adapter 路径时，需要在 `external.metric` 下新增 `selector.matchLabels`，以 Prometheus Adapter 暴露的标签限定指标范围。以下示例为 Prometheus Adapter 场景：Prefill / Decode 分别按容量规划利用率指标扩缩容（target 即目标利用率 ρ）。

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
              name: motor_prefill_utilization
              selector:
                matchLabels:
                  infer_huawei_com_inferservice_name: vllm-0
                  kubernetes_namespace: mindie-motor
            target:
              type: Value
              value: "0.8"
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
              name: motor_decode_utilization
              selector:
                matchLabels:
                  infer_huawei_com_inferservice_name: vllm-0
                  kubernetes_namespace: mindie-motor
            target:
              type: Value
              value: "0.8"
    metadata:
      labels:
        infer.huawei.com/gang-schedule: 'true'
    spec:
      # ... 其余配置保持不变 ...
```

Prometheus Adapter 场景下，`selector.matchLabels` 用于筛选本服务的指标，标签名称和值须与 Prometheus Adapter 返回的指标一致。
例如 Prometheus Adapter 暴露的指标名为 `vllm_total_requests`，则将上述 `metric.name` 改为该名称。
模板中的 `infer_huawei_com_inferservice_name` 应填写 InferServiceSet 展开出的实例名；单实例部署为 `<InferServiceSet 名>-0`，例如 `vllm-0`。

`kubernetes_namespace` 表示被采集的 Motor 服务所在命名空间，不是 Prometheus Adapter 自身的部署命名空间。
生成 `infer_service.yaml` 时，deployer 会遍历各角色的 HPA External 指标，将已配置的
`selector.matchLabels.kubernetes_namespace` 更新为 `user_config.json` 中的
`motor_deploy_config.job_id`。例如模板写的是 `mindie-motor`，而 `job_id` 为 `my-motor`，
则生成后的资源 `metadata.namespace` 和该指标标签均为 `my-motor`。
其他指标名、标签、阈值和副本上下限保持原样；未配置该标签时不会自动补充，也不会自动启用扩缩容。
此联动发生在两处：从模板生成 `infer_service.yaml` 时，以及仅更新实例数的手动扩缩容路径（如 `deploy.py --update_instance_num`）改写已生成的 `infer_service.yaml` 时；两者都会按当前 `job_id` 回填 selector，无需手动修改。

> [!WARNING]
> 手动扩缩容路径只改已生成的 `infer_service.yaml`，不会重新读取模板。更换 `job_id`（即更换部署命名空间）时，必须先按新 `job_id` 重新生成 `infer_service.yaml` 再执行扩缩容，否则模板里的旧 selector 会残留到新命名空间的资源中。

原有 External Metrics Adaptor 场景不需要添加上述 selector。只需填写该 Adaptor 实际暴露的指标名称和目标值，例如：

```yaml
metrics:
  - type: External
    external:
      metric:
        name: vllm:num_requests_waiting
      target:
        type: AverageValue
        averageValue: "5"
```

### scalingPolicy 参数说明

| 参数 | 说明 | 取值 |
|------|------|------|
| `scalingPolicy.type` | 弹性扩缩容策略类型 | 当前仅支持 `HPA` |
| `scalingPolicy.spec.minReplicas` | 缩容下限，实例数不会低于此值 | 正整数 |
| `scalingPolicy.spec.maxReplicas` | 扩容上限，实例数不会超过此值 | 正整数，且 ≥ minReplicas |
| `scalingPolicy.spec.metrics[].type` | 指标类型 | `External`（由 External Metrics Adaptor 或 Prometheus Adapter 提供） |
| `scalingPolicy.spec.metrics[].external.metric.name` | 外部指标名称 | 需与所选指标接入组件暴露的指标名一致 |
| `scalingPolicy.spec.metrics[].external.metric.selector` | 指标选择器 | 仅 Prometheus Adapter 场景需要配置；External Metrics Adaptor 场景无需新增 |
| `scalingPolicy.spec.metrics[].external.metric.selector.matchLabels.kubernetes_namespace` | 指标所属命名空间 | 仅 Prometheus Adapter 场景配置。模板包含此标签时，deployer 生成 YAML 会将其更新为 `motor_deploy_config.job_id` |
| `scalingPolicy.spec.metrics[].external.metric.selector.matchLabels.infer_huawei_com_inferservice_name` | 指标所属 InferService | 仅 Prometheus Adapter 场景配置。模板包含此标签时，deployer 按 `<InferServiceSet 名>-0` 自动更新；缺失时不注入 |
| `scalingPolicy.spec.metrics[].external.target.type` | 目标值类型 | `Value` 或 `AverageValue`（Pod 平均值）。容量规划利用率指标（`motor_*_utilization`）已按当前实例数归一化，**必须用 `Value`**；`AverageValue` 会被 HPA 再除一次副本数导致失真。原始量纲指标（如排队数、KV 使用率）用 `AverageValue` |
| `scalingPolicy.spec.metrics[].external.target.value` / `averageValue` | 目标阈值 | 与 `target.type` 对应；按指标量纲设定 |

> [!NOTE]
> `scalingPolicy.spec.metrics[].external.metric.name` 需填写所选指标接入组件暴露的指标名。External Metrics Adaptor 或 Prometheus Adapter 都可能对 MindIE Motor 原始 Prometheus 指标名（如 `vllm:num_requests_waiting`）做映射或重命名。部署后，可通过以下命令查看实际暴露的指标列表：
>
> ```bash
> kubectl get --raw /apis/external.metrics.k8s.io/v1beta1 | grep -E "vllm:|motor:"
> ```
>
> 若 Adaptor 暴露的指标名与本文示例不同，请以实际返回值为准。
>
> 本特性依赖 MindCluster Infer Operator 版本 ≥ 26.1.0。`scalingPolicy` 可在 user_config 的 `scaling_policy` 配置段中按角色声明，由 deployer/helm 自动渲染（默认指标 `motor_prefill_utilization` / `motor_decode_utilization`，target 0.8、类型 `Value`，并自动带上 namespace 与实例 selector），也可按上文示例手动添加到 `infer_service_template.yaml`。

## 推荐的扩缩容指标

MindIE Motor `/metrics` 端点提供了丰富的引擎级指标，下表列出推荐用于自动扩缩容的关键指标：

### Prefill 扩缩容推荐指标

| 指标名 | 类型 | 说明 | 推荐阈值建议 |
|--------|------|------|-------------|
| `motor:prefill_utilization`（别名 `motor_prefill_utilization`） | Gauge | Prefill 需求/供给利用率（>1 供给不足，Motor 计算） | target 即 ρ，默认 0.8，推荐首选 |
| `vllm:num_requests_waiting` | Gauge | 等待调度的请求数 | > 5 触发扩容，< 2 触发缩容 |
| `vllm:num_requests_running` | Gauge | 当前运行中的请求数 | 视 NPU 规格和模型而定 |
| `vllm:kv_cache_usage_perc` | Gauge | KV Cache 使用率（0-1） | > 0.8 触发扩容 |
| `motor:prompt_tokens_per_second` | Gauge | Prompt token 处理速率（Motor 计算） | 按 SLA 目标设定 |
| `vllm:time_to_first_token_seconds` | Histogram | 首 token 延迟（TTFT） | 按 SLA 目标（如 p95 < 500ms） |

### Decode 扩缩容推荐指标

| 指标名 | 类型 | 说明 | 推荐阈值建议 |
|--------|------|------|-------------|
| `motor:decode_utilization`（别名 `motor_decode_utilization`） | Gauge | Decode 需求/供给利用率（>1 供给不足，Motor 计算） | target 即 ρ，默认 0.8，推荐首选 |
| `vllm:num_requests_waiting` | Gauge | 等待调度的请求数 | > 5 触发扩容 |
| `vllm:num_requests_running` | Gauge | 当前运行中的请求数 | 视 NPU 规格和模型而定 |
| `motor:generation_tokens_per_second` | Gauge | 生成 token 速率（Motor 计算） | 按 SLA 目标设定 |
| `vllm:e2e_request_latency_seconds` | Histogram | 端到端请求延迟 | 按 SLA 目标（如 p95 < 2s） |
| `vllm:time_per_output_token_seconds` | Histogram | 跨 token 延迟（TPOT） | 按 SLA 目标（如 p95 < 50ms） |

> [!NOTE]说明
>
>- `motor:prompt_tokens_per_second` 和 `motor:generation_tokens_per_second` 是 MindIE Motor Coordinator 计算的服务级指标，基于 vLLM 原始 counter 计算 delta rate 得到，更准确反映实时吞吐。
>- Histogram 类型指标（如 `vllm:e2e_request_latency_seconds`）需要在 Adaptor 侧计算分位数（p50/p95/p99）后作为独立指标暴露。
>- 建议为 Prefill 和 Decode 分别配置不同的扩缩容指标，以匹配各自的计算特征（Prefill 为计算密集型，Decode 为访存密集型）。

### 多指标组合策略

可在 HPA 中配置多个指标，HPA 会选择**最保守的扩缩容决策**（即当前副本数最接近触发扩容或缩容的指标）：

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
          name: motor_decode_utilization
        target:
          type: Value
          value: "0.8"
    - type: External
      external:
        metric:
          name: vllm:kv_cache_usage_perc
        target:
          type: AverageValue
          averageValue: "0.8"
```

## 基于容量规划利用率的 HPA（推荐）

原始指标（排队请求数、TPS、KV Cache 使用率）的量纲随硬件与模型变化，阈值难以统一设定。
MindIE Motor 基于容量规划模型提供需求/供给利用率指标：取值为
`需求速率 / (当前实例数 × 单实例容量估计)`，>1 表示供给不足，**target 即目标利用率 ρ
（默认 0.8）**，是推荐的 HPA target：

- **Prefill 角色**：`motor_prefill_utilization`（原始名 `motor:prefill_utilization`），
  target 建议设为 `0.8`；
- **Decode 角色**：`motor_decode_utilization`（原始名 `motor:decode_utilization`），
  target 建议设为 `0.8`。

利用率由 Motor Coordinator 内置容量规划器计算：需求侧由入口统计推导（完成请求速率 λ、
平均输入/输出长度），供给侧在线标定单实例容量（详见下文）。指标语义与全部配置项详见
[监控接口 - 容量规划指标](../api/metrics_interfaces.md#容量规划指标)。K8s External
Metrics 命名不含 `:`，请使用无冒号别名（`motor_prefill_utilization` /
`motor_decode_utilization`）接入 HPA；别名与原始指标在 `/metrics` 中同时输出、取值一致。

> [!IMPORTANT]
> **target 类型**：利用率指标已按当前实例数归一化（取值 = 需求 / 供给），HPA 配置中
> `target.type` 必须使用 `Value`。若误用 `AverageValue`，HPA 会把指标值再除一次
> 当前副本数，导致扩缩容目标失真。
>
> **迁移提示**：初版归一化饱和度指标（`motor:prefill_saturation` /
> `motor:decode_saturation`）已移除，曾按初版文档配置 `motor_*_saturation` 的用户
> 请改用上述 `motor_*_utilization` 指标。

配置示例（Decode 角色按利用率扩缩容）：

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
          name: motor_decode_utilization
        target:
          type: Value
          value: "0.8"
```

### 容量标定说明

利用率与所需实例数的准确性取决于单实例容量估计 `C_p` / `C_d`，Motor 以**在线学习为主、
benchmark 先验为辅**：

- **在线学习（默认）**：`C_p` 由 `Δprompt_tokens / Δprefill_time_sum`（不含排队与空闲的
  纯处理吞吐）经 EMA 估计（`ema_alpha` 默认 `0.85`：样本本身是 3s 窗口聚合均值，无需重
  平滑，需求/容量估计在 3 个采集周期内收敛，毛刺由下游 HPA 稳定窗口兜底）；`C_d` 采用单实例 generation TPS 峰值学习——观测到更高值即上抬，
  否则每周期按 `capacity_decay`（默认 `0.005`）慢衰减，但**不跌破下限
  `floor = max(先验, 滑动窗口样本峰值)`**（窗口长度 `capacity_window_cycles`，默认 200
  周期 ≈ 10 分钟）。floor 机制消除了低载时 `C_d` 指数衰减至零导致的幻影扩容；当真实容量
  下降时，旧峰值滑出窗口后估计可重新向下学习。**注意**：floor 的窗口兜底有时限——持续低载
  超过 `capacity_window_cycles` 后，窗口内只剩低水位样本，`C_d` 会缓慢向观测水位（≈ 当前
  每实例吞吐）衰减，利用率随之虚高，可能诱发最多 +1 副本的缓慢漂移（每衰减一轮至多一次）。
  配置 benchmark 先验可彻底规避（见下条）。
- **benchmark 先验（建议生产环境始终配置）**：在目标硬件与模型上以贴近现网的输入/输出长度
  分布运行 benchmark，测出**单实例饱和 TPS**（时延开始显著劣化前的吞吐拐点），配置
  `prometheus_metrics_config.capacity_planning.prefill_tps_capacity_prior` /
  `decode_tps_capacity_prior`（默认 `0` 表示无先验）。先验可消除冷启动阶段的估计偏差，
  且作为 `floor = max(先验, 窗口峰值)` 的硬下限，钉死低载衰减路径，同时使角色自启动起即
  视为已标定。**取值宁低勿高**：略低于真实容量是安全的——在线学习观测到更高 TPS 会立即
  向上采纳，估计只会向上突破先验；而先验过高时无向下纠偏机制（估计不会跌破先验），
  `utilization` 与 `replicas_required` 将双双低估，HPA 该扩不扩，牺牲时延 SLO。
- **置信标志**：`motor:capacity_calibrated{role="prefill"|"decode"}` 为 `0` 表示容量
  尚未标定（无先验且无有效观测），此时利用率暂不输出、所需实例数指标取 `0`；标定完成
  （置 `1`）后正常输出。
- **保守方向**：容量估计为真实容量的下界，`utilization` 因此偏高、扩容倾向偏多（安全
  方向），且多扩后承接更多需求可观测到更高 TPS，估计自行收敛。

### Prometheus 侧平滑建议

Coordinator 按 `reuse_time`（默认 `3` 秒）粒度采集，利用率与 RPS 在单周期内可能有明显
抖动。建议 External Metrics Adaptor / Prometheus 侧先以 `avg_over_time(1m)` 平滑后再送入
HPA，例如：

```text
avg_over_time(motor_decode_utilization[1m])
```

指标本身保持原始粒度输出，不做预平滑，以便排障时仍能观察到瞬时尖峰。

## PD 配比收敛（建议信号）

P、D 两个角色由各自独立的 HPA 扩缩容，负载结构（输入/输出长度分布）变化时，PD 配比可能逐渐偏离最优。MindIE Motor 基于容量规划模型提供配比信号：

- `motor:pd_ratio_current`（别名 `motor_pd_ratio_current`）：当前 PD 配比，即 active Prefill 实例数 / max(active Decode 实例数, 1)；
- `motor:pd_ratio_suggested`（别名 `motor_pd_ratio_suggested`）：建议 PD 配比，由容量规划推导的所需实例数之比 `motor:prefill_replicas_required / motor:decode_replicas_required` 计算，经指数平滑（默认 α=0.3）与区间裁剪（默认 [0.05, 20.0]）后输出。容量未标定或无需求（所需 Decode 实例数为 0）时该比值无意义，此时回退为现状比 `motor:pd_ratio_current`，容量标定完成后平滑过渡到模型推导值。

所需实例数指标本身也以别名 `motor_prefill_replicas_required` /
`motor_decode_replicas_required` 暴露，外部控制器可直接读取，跳过配比换算。

> [!IMPORTANT]说明
> `motor:pd_ratio_suggested` 仅为**建议信号**，MindIE Motor 不会依据它自动调整任何实例数量或 K8s 资源，执行由外部控制器完成。

参考执行方式：使用 CronJob 周期性读取建议配比并调整 InferServiceSet 各 role 的 `replicas`。步骤如下：

1. 从 External Metrics API 或 Prometheus 读取 `motor_pd_ratio_suggested`（同样建议先 `avg_over_time` 平滑）；
2. 在实例总数 `N` 约束下按建议配比 `r` 换算目标实例数：`P = round(N × r / (1 + r))`，`D = N - P`；
3. 通过 `kubectl patch` 更新 InferServiceSet 对应 role 的 `replicas`。

执行约束：

- **执行周期 ≥ 5 分钟**，并设置冷却时间（两次调整之间至少间隔一个 HPA 稳定窗口），避免与 HPA 扩缩容叠加引发震荡；
- 单次调整幅度建议设上限（如每次最多变更 1~2 个实例），平滑收敛；
- 配比寻优的正确性由仓库内合成闭环仿真测试（`tests/coordinator/core/`）回归保障，但真实集群的最佳配比仍与硬件/模型/负载分布相关，建议结合观测数据验证。

## 验证扩缩容效果

### 查看 HPA 状态

```bash
kubectl get hpa -n {namespace}
```

回显示例如下：

```text
NAME                          REFERENCE                    TARGETS          MINPODS   MAXPODS   REPLICAS   AGE
prefill-my-test               StatefulSet/prefill-my-test  3/5              1         4         2          10m
decode-my-test                StatefulSet/decode-my-test   8/10             1         4         2          10m
```

- `TARGETS` 列显示 `当前值/目标值`，当前值超过目标值时触发扩容。
- `REPLICAS` 列显示当前实际副本数。

### 模拟负载触发扩容

发送大量并发推理请求，观察 HPA 是否自动扩容：

```bash
# 并发发送请求
for i in {1..100}; do
  curl -X POST "http://{service-ip}:31015/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"model": "your-model", "max_tokens": 100, "messages": [{"role": "user", "content": "Hello"}]}' &
done
```

随后查看 HPA 状态，确认 `REPLICAS` 是否增加，以及新增引擎 Pod 是否正常运行：

```bash
kubectl get hpa -n {namespace} --watch
kubectl get pod -n {namespace} | grep -E "prefill|decode"
```

### 验证缩容

停止负载后，观察数分钟（由 HPA `--horizontal-pod-autoscaler-downscale-stabilization` 默认 5 分钟），确认实例数回落至 `minReplicas`：

```bash
kubectl get hpa -n {namespace} --watch
```

## 注意事项

- HPA 弹性扩缩容当前仅支持 Prefill 和 Decode 实例；Router 不参与扩缩容。
- 缩容存在稳定窗口（默认 5 分钟），避免负载短暂波动导致频繁扩缩。
- 扩容的新实例没有 KV Cache 缓存，Prefix Cache 特性会逐步重建缓存，因此新实例的推理性能可能出现小幅度劣化并在一段时间后恢复。
- External Metrics Adaptor 需持续运行并正确配置 Coordinator 地址。若 Adaptor 异常，HPA 将无法获取指标，可能导致扩缩容失效。
- 建议 `minReplicas` 至少设为 1，避免缩容到 0 导致服务完全不可用。
- Counter 类型指标（如 token 总数）不会因 `/metrics` 请求而重置，建议优先使用 Gauge 类型指标或 MindIE Motor 计算的 TPS 指标作为扩缩容依据。
- 若使用不带 `type` 参数的 `/metrics` 端点（默认 `full`），HPA 获取到的是全局聚合值。如需按 Prefill/Decode 角色独立扩缩容，Adaptor 需分别请求 `/metrics?type=role&role=prefill` 和 `/metrics?type=role&role=decode`。
- HPA 扩缩容路径依赖 Infer Operator 创建的 HPA 资源；`multi_deployment` 部署模式没有 Infer Operator，不支持本文所述的 HPA 路径。

## 参考文档

- [配置基于负载的弹性扩缩容](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/04_usage/09_infer_operator_best_practice/05_configuring_elastic_scaling.md) — Infer Operator 弹性扩缩容策略配置指南
- [Metrics 可观测性指标设计文档](../../design/metrics.md) — MindIE Motor Metrics 子系统架构与指标说明
- [监控接口](../api/metrics_interfaces.md) — MindIE Motor `/metrics` 端点使用说明
