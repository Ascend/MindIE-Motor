# K8s 指标采集与 HPA 对接示例

Coordinator Observability 进程在 **1027** 端口暴露 Prometheus 格式的 `/metrics`（K8s 下经 obs Service
`mindie-motor-coordinator-obs-<infer_service_set_name>-0-coordinator`，默认 NodePort 31017）。
本目录提供 K8s 环境下的采集集成产物；docker-compose 本地参考栈见上级目录 `stack/`。

## 形态一：Prometheus Operator（ServiceMonitor）

应用 `servicemonitor.yaml` 前，按文件头部注释核对 namespace、Service label selector 与
Prometheus CR 的 `serviceMonitorSelector`。指标名带冒号（`vllm:`/`motor:`）需要 Prometheus >= 3.0
并在 Prometheus CR 上设置 `metricNameValidationScheme: UTF8`；HPA 契约指标的无冒号别名不受此限制。

## 形态二：裸 Prometheus（scrape annotations）

不使用 Prometheus Operator 时，在 obs Service 上添加 scrape annotations（与上述 ServiceMonitor 等价），
并确保 Prometheus 的 `kubernetes_sd_configs` 开启了 `role: endpoints`（或 `role: service`）抓取：

```yaml
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "1027"
    prometheus.io/path: "/metrics"
```

等价的静态 scrape 配置可参考 `../stack/prometheus/prometheus.yml` 中的
`motor-coordinator*` 任务（`metrics_path: /metrics`，支持 `?type=role&role=prefill|decode` 按角色视图抓取）。
裸 Prometheus 同样需要 `metric_name_validation_scheme: utf8` 才能采集带冒号的指标名。

## External Metrics Adaptor 对接（HPA 契约指标）

HPA 经 External Metrics Adaptor 消费 Coordinator `/metrics`，Adaptor 部署与 HPA 配置详见
`docs/zh/user_guide/features/auto_scaling.md`。Motor 在 `/metrics` 中为以下契约指标同时暴露
**无冒号别名**（`:` → `_`），作为 External Metrics 的稳定契约：

| 别名指标 | 语义 | 用途 |
|---------|------|------|
| `motor_prefill_utilization` | Prefill 需求/供给利用率 | HPA target（ρ，默认 0.8） |
| `motor_decode_utilization` | Decode 需求/供给利用率 | HPA target（ρ，默认 0.8） |
| `motor_prefill_replicas_required` | 所需 Prefill 实例数 | 外部控制器读取目标实例数 |
| `motor_decode_replicas_required` | 所需 Decode 实例数（双约束取 max） | 外部控制器读取目标实例数 |
| `motor_request_rate` | Coordinator 入口 RPS | 按请求速率扩缩容 |
| `motor_pd_ratio_current` | 当前 active P/D 实例比 | PD 配比观测 |
| `motor_pd_ratio_suggested` | 建议 P/D 配比（所需实例数之比，平滑后） | 外部控制器收敛 PD 配比 |
| `vllm_request_prefill_time_seconds` | Prefill 时延分位数及派生 | Prefill 侧 SLA 观测 |

对接指引：

- Adaptor 侧将别名指标映射为 External Metrics（名称保持 `[a-z0-9_]` 习惯，直接使用别名即可），
  部署后可用 `kubectl get --raw /apis/external.metrics.k8s.io/v1beta1 | grep motor_` 验证。
- Coordinator 指标按采集周期（默认 3s）刷新，波动较大；建议在 Prometheus 侧用 recording rules
  做平滑（如 `avg_over_time(motor_prefill_utilization[1m])`）后再供 Adaptor/HPA 消费，
  避免 HPA 抖动。
- `motor_pd_ratio_suggested` 仅为建议信号，执行（修改 InferServiceSet 各 role replicas）由外部控制器
  完成，建议执行周期 ≥ 5min 并带冷却约束；multi_deployment 模式无 Infer Operator，不支持 HPA 路径。
