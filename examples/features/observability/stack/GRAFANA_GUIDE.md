# pyMotor 可观测性栈 · Grafana 使用指导

本指导说明栈内 Grafana 的页面设计、内置看板与数据源，并提供「如何在看板中新增其他 metrics 指标」的可复现步骤。

> 服务拉起 / 停止操作见 [SERVICE_GUIDE.md](SERVICE_GUIDE.md)。

前提：已按 [SERVICE_GUIDE.md](SERVICE_GUIDE.md) 拉起栈，且 `http://localhost:3000` 可访问。

---

## 1. 登录与访问

| 项 | 默认值 | 说明 |
|----|--------|------|
| 地址 | `http://localhost:3000` | 端口由 `.env` 的 `GRAFANA_PORT` 控制 |
| 账号 | `motor` | `.env` 的 `GF_SECURITY_ADMIN_USER` |
| 密码 | `motor` | `.env` 的 `GF_SECURITY_ADMIN_PASSWORD` |

登录后进入 **Dashboards** 即可看到下文的三个内置看板。

---

## 2. 页面设计总览

### 2.1 数据源（Datasources）

数据源由 `grafana/provisioning/datasources/datasources.yml` 预置，无需手动添加：

| 数据源 | UID | 地址 | 用途 |
|--------|-----|------|------|
| **Prometheus** | `prometheus` | `http://prometheus:9090` | 指标查询（默认数据源） |
| **Tempo** | `tempo` | `http://tempo:3200` | 分布式追踪（Trace） |
| **Loki** | `loki` | `http://loki:3100` | 日志（minimal / full 均包含） |

并已配置三者间的联动跳转：

- **Trace → Log**（Tempo `tracesToLogsV2`）：按 `service.name` / `x_request_id` 关联，时间窗口前后 5 分钟。
- **Trace → Metrics**（Tempo `tracesToMetrics`）：按 `service.name` 关联 Prometheus。
- **Log → Trace**（Loki `derivedFields`）：从日志中正则提取 `trace_id` / `x_request_id`，一键跳转 Tempo。

### 2.2 看板（Dashboards）

看板 JSON 位于 `grafana/dashboards/`，由 `grafana/provisioning/dashboards/dashboard-providers.yml` 自动加载（平铺、无文件夹层级，`foldersFromFilesStructure: false`，`updateIntervalSeconds: 30`）。当前仅保留三个：

| 看板 | UID | 文件 | 内容 |
|------|-----|------|------|
| **pyMotor Metrics · 指标总览** | `motor-all-metrics` | `motor-all-metrics.json` | 集群概览、PD Role / Instance 分组、吞吐与延迟 |
| **KV 缓存** | `motor-kv-cache` | `motor-kv-cache.json` | vLLM KV cache 使用率、prefix cache 命中率 |
| **引擎性能剖析** | `motor-vllm-profiling` | `motor-vllm-profiling.json` | `vllm_profiling_*` 性能剖析（显存、forward/execute/scheduler 时延等） |

> Grafana 容器以 **只读** 方式挂载 `grafana/dashboards`，因此在 UI 上的临时修改不会落盘；要长期保留改动需写回对应 JSON 文件（见第 4 节）。

### 2.3 看板变量（Template Variables）

以「指标总览」为例，顶部变量用于跨集群 / 角色 / 实例过滤，均为 `query` 类型并基于 Prometheus 标签动态生成：

| 变量 | Label |
|------|-------|
| $cluster | Cluster |
| $motor_metric_scope | Metric Scope |
| $role | Role |
| $pd_role | PD Role |
| $dp_rank | DP Rank |
| $pod_ip | Pod IP |
| $instance_id | Instance ID |
| $model_name | Model |

各变量在 Grafana 中的取值查询（`label_values`）：

- **$cluster**：`label_values({cluster!=""}, cluster)`
- **$motor_metric_scope**：`label_values({cluster=~"$cluster", motor_metric_scope!=""}, motor_metric_scope)`
- **$role**：`label_values({cluster=~"$cluster", role!=""}, role)`
- **$pd_role**：`label_values({cluster=~"$cluster", pd_role!=""}, pd_role)`
- **$dp_rank**：`label_values({cluster=~"$cluster", dp_rank!=""}, dp_rank)`
- **$pod_ip**：`label_values({cluster=~"$cluster", pod_ip!=""}, pod_ip)`
- **$instance_id**：`label_values({cluster=~"$cluster", instance_id!=""}, instance_id)`
- **$model_name**：`label_values(vllm:num_requests_running{cluster=~"$cluster"}, model_name)`

新增面板时**应复用这些变量**做标签过滤，并将变量的 `allValue` 设为 `.*`，避免标签缺失导致 No Data。「引擎性能剖析」看板另有 `$source`、`$job`、`$phase`、`$dp` 等变量，含义类似。

---

## 3. 验证指标是否已被采集

新增看板指标前，先确认 Prometheus 已抓到目标指标，避免在 Grafana 侧反复调试。

```bash
# 列出所有指标名（确认指标存在）
curl -s http://localhost:9090/api/v1/label/__name__/values | tr ',' '\n' | grep -i <keyword>

# 直接查询某指标当前值
curl -sG http://localhost:9090/api/v1/query --data-urlencode 'query=<metric_name>'

# 查看抓取目标是否 UP
curl -s http://localhost:9090/api/v1/targets
```

也可在 Grafana 左侧 **Explore** 选择 Prometheus 数据源，直接输入 PromQL 验证表达式。

---

## 4. 在看板中新增其他 metrics 指标

有两种方式：**UI 编辑后写回 JSON**（推荐，可纳入版本库）或 **直接编辑 JSON 文件**。

### 4.1 方式一：UI 编辑面板并写回 JSON

1. 打开目标看板（如「指标总览」），点击右上角 **Edit**。
2. 点击 **Add → Visualization** 新增面板，选择数据源 **Prometheus**。
3. 在 **Query** 中输入 PromQL，复用看板变量做过滤，例如新增「每实例的等待请求数」：

   ```promql
   sum by (instance_id) (
     vllm:num_requests_waiting{cluster=~"$cluster", instance_id=~"$instance_id", pd_role=~"$pd_role"}
   )
   ```

4. 选择可视化类型（Time series / Stat / Bar gauge / Pie chart 等），设置标题、单位、阈值。
5. **Apply** 返回看板，调整面板位置与大小。
6. 写回源文件以长期保留：点击看板设置（齿轮）→ **JSON Model**，复制完整 JSON，覆盖写入对应文件，例如：
   - 指标总览 → `grafana/dashboards/motor-all-metrics.json`
   - KV 缓存 → `grafana/dashboards/motor-kv-cache.json`
   - 引擎性能剖析 → `grafana/dashboards/motor-vllm-profiling.json`

   provisioner 每 30s 重新加载挂载目录，刷新页面即可看到生效（容器以只读挂载，必须写回文件才会持久化）。

### 4.2 方式二：直接编辑看板 JSON 文件

在 `grafana/dashboards/<dashboard>.json` 的 `panels` 数组中追加一个面板对象。可参考「指标总览」中现有 `stat` 面板的最小结构：

```json
{
  "id": 100,
  "type": "timeseries",
  "title": "Waiting Requests by instance_id",
  "gridPos": { "h": 8, "w": 12, "x": 0, "y": 56 },
  "datasource": { "type": "prometheus", "uid": "prometheus" },
  "targets": [
    {
      "expr": "sum by (instance_id) (vllm:num_requests_waiting{cluster=~\"$cluster\", instance_id=~\"$instance_id\"})",
      "refId": "A",
      "legendFormat": "{{instance_id}}"
    }
  ],
  "fieldConfig": { "defaults": { "unit": "short" } }
}
```

要点：

- `id` 在同一看板内唯一；`gridPos` 的 `x/y/w/h` 决定布局（看板宽度 24 格，`x` 取 0–23）。
- `datasource.uid` 固定为 `prometheus`（或 `tempo` / `loki`）。
- `targets[].expr` 为 PromQL，**务必带上看板变量过滤**（`{cluster=~"$cluster", ...}`），否则切换变量时该面板不会随动。
- `legendFormat` 用 `{{label}}` 渲染图例。

保存文件后，provisioner 自动重载（约 30s），刷新浏览器即可。若 JSON 改动较大，可执行 `docker compose restart grafana` 强制重载。

### 4.3 新增需要先接入新数据源的指标

如果指标来自尚未被抓取的新组件：

1. 在生成 / 模板 `prometheus.yml` 中新增对应 scrape job（自动发现产物为 `generated/prometheus.yml`）。
2. 重新执行 `./launch.sh`（或 `curl -X POST http://localhost:9090/-/reload` 触发 Prometheus 热加载，需启用 lifecycle，本栈已开启 `--web.enable-lifecycle`）。
3. 确认 `targets` 为 UP、指标可查询后，再按 4.1 / 4.2 添加面板。

### 4.4 性能剖析看板的自动生成（可选）

`grafana/scripts/build-profiling-dashboard.py` 可从 Prometheus 拉取 `vllm_profiling_*` 指标族并自动生成 `motor-vllm-profiling.json`（核心面板常开、明细面板默认折叠）：

```bash
cd examples/features/observability/stack
python3 grafana/scripts/build-profiling-dashboard.py \
  --prometheus-url http://localhost:9090 \
  --output grafana/dashboards/motor-vllm-profiling.json
```

适用于 Engine 暴露了新的 `vllm_profiling_*` 指标后，批量刷新剖析看板。

---

## 5. Trace 与 Profiling 数据接入（pyMotor 侧）

要让 Tempo / profiling 面板有数据，需在 pyMotor 侧开启上报；**需要在拉起栈之前完成的 pyMotor 配置清单见 [SERVICE_GUIDE.md §1.4](SERVICE_GUIDE.md)**。本节为操作要点速查。

> 基础指标（指标总览 / KV 缓存）无需改 pyMotor 配置即可生效；当前方案不使用 Controller metrics 接口，相关配置可忽略。

### 5.1 Tracing

pyMotor 建议配置（`<obs-host>` 为观测主机，参考 `config/tracing.example.json`）：

- Coordinator：`tracer_config.endpoint = http://<obs-host>:4318/v1/traces`
- Engine：`engine_config.otlp-traces-endpoint = http://<obs-host>:4318/v1/traces`
- `OTEL_SERVICE_NAME` 建议：`mindie-motor-coordinator`、`vllm-server-p`、`vllm-server-d`

在 Grafana **Explore** 选 Tempo 时，若默认 Query type 为 TraceQL，可切到 **Search**，或在 TraceQL 输入 `{}` 后执行搜索。

### 5.2 Profiling

需在 Engine 侧安装 [`ms_service_metric`](https://gitcode.com/Ascend/msserviceprofiler/tree/master/ms_service_metric)（`pip install ms_service_metric`；依赖 Python >= 3.10、pyyaml、prometheus-client、posix_ipc）。详细步骤见 [SERVICE_GUIDE.md §1.4.2](SERVICE_GUIDE.md)。

Engine 启动前：

```bash
export PROMETHEUS_MULTIPROC_DIR=/dev/shm/vllm_metrics && mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
# 可选：rm -rf $PROMETHEUS_MULTIPROC_DIR/*
```

Engine ready 后开启指标采集：

```bash
ms-service-metric on      # 开启
ms-service-metric off     # 关闭
ms-service-metric restart # 重启（重新加载配置）
ms-service-metric status  # 查看状态
```

随后 `vllm_profiling_*` 指标会被 Prometheus 抓取，「引擎性能剖析」看板即可显示数据。

---

## 6. 常见问题

| 现象 | 处理建议 |
|------|----------|
| 看板变量下拉为空 | 对应标签未被任何指标暴露；先确认 Prometheus 已抓到带该标签的指标（第 3 节）。 |
| 新增面板 No Data | 检查 PromQL 是否带了看板变量过滤；变量 `allValue` 是否为 `.*`；指标名是否含冒号（如 `vllm:*`，本栈已开启 `--enable-feature=utf8-names`）。 |
| UI 改动刷新后丢失 | 容器只读挂载 `grafana/dashboards`，需将 JSON Model 写回源文件（第 4 节）。 |
| 看板报 500 / 504 | Grafana 容器经外网代理访问 `prometheus` / `tempo` 超时；确认容器内 `HTTP_PROXY` 为空（详见 [SERVICE_GUIDE.md](SERVICE_GUIDE.md) 第 6 节）。 |
| Loki 数据源不可用 | Loki 仅在 full 模式拉起，minimal 模式无 Loki。 |
