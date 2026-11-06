# MindIE Motor 可观测性示例

本目录提供 MindIE Motor 的可观测性能力示例，可按需独立启用：

| 能力 | 说明 | 目录 |
|------|------|------|
| **一键观测栈** | Prometheus + Grafana + Tempo + OTel Collector（minimal 默认含 Loki；`--full` 额外含 node-exporter / cAdvisor），自动发现 Motor 指标端点并生成抓取配置 | [`stack/`](stack/README.md) |
| **CCAE Reporter** | 采集 Motor 运行信息（告警、日志、实例信息、metrics）上报华为 CCAE（Cluster Computing Autonomous Engine） | [`ccae_reporter/`](ccae_reporter/) |
| **K8s 采集与 HPA 对接** | Prometheus Operator（ServiceMonitor）/ 裸 Prometheus / External Metrics Adaptor 示例 | [`k8s/README.md`](k8s/README.md) |

本文按 **准备 → 运行 → 结果查看** 组织，命令均与 `examples/deployer/` 的部署工具路径保持一致。

## 1. 准备

### 1.1 运行环境

| 项 | 要求 |
|----|------|
| 观测机 | Linux，可执行 `launch.sh` 与 `curl` |
| Kubernetes | Motor 已部署；`kubectl` 可用且能访问集群（发现阶段不要挂 HTTP 代理） |
| 容器运行时 | Docker + Docker Compose；无 Docker 时可用 `./launch.sh --native` 走原生二进制（仅 Prometheus / Grafana / Tempo / OTel Collector，不含 Loki 与 infra exporters）。native 自动下载目前仅支持 `linux-amd64`；arm64 推荐使用 Docker，或按 `SERVICE_GUIDE.md` 预置 arm64 官方二进制 |
| Python | 3.10+（CCAE Reporter 与 deployer 工具） |
| 网络 | 观测机可访问 Coordinator 指标端口与 Engine 业务 `/metrics` 端口（`launch.sh` 默认 `10000`），或使用 NodePort；静态 Prometheus 模板另使用管理 metrics 端口 `10001` |

### 1.2 部署 MindIE Motor（deployer 工具）

部署与配置更新统一在 `examples/deployer/` 目录执行：

```bash
cd examples/deployer

# 首次部署（配置目录内需包含 user_config.json 与 env.json）
python3 deploy.py --config_dir ../../model_configs/vllm

# 仅更新 ConfigMap（不重新部署）——修改可观测性相关配置后执行
python3 deploy.py --config_dir ../../model_configs/vllm --update_config

# 按配置扩缩容实例数量
python3 deploy.py --config_dir ../../model_configs/vllm --update_instance_num
```

Docker / Slurm 环境分别使用 `docker_deploy.py` / `slurm_deploy.py`。参数说明见
[`examples/deployer/README.md`](../../deployer/README.md)。

### 1.3 打开需要额外配置的观测能力

| 观测能力 | 是否需改 Motor 配置 | 说明 |
|----------|----------------------|------|
| Coordinator 基础指标 | 否 | Obs 端口（默认 `1027`）暴露 `/metrics`，可用 `?type=instance` / `role` / `dp` / `node` 取不同视图；无需额外配置，保证该端口可达（`/instance/metrics` 已废弃并返回 HTTP 410） |
| Engine / vLLM 指标 | 否（默认开启） | 原生引擎在业务端口暴露 `/metrics` |
| Tracing（Tempo 链路） | **是** | 见下 |
| 引擎性能剖析（`vllm_profiling_*`） | **是** | 见下 |

**Tracing**：在 deploy 使用的 `env.json` 中为 Coordinator / Prefill / Decode 增加
`OTEL_SERVICE_NAME`、`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf`、
`OTEL_EXPORTER_OTLP_TRACES_INSECURE=true`；在 `user_config.json` 中把
`motor_coordinator_config.tracer_config.endpoint` 与两个引擎的
`engine_config.otlp-traces-endpoint` 指向 `http://<obs-host>:4318/v1/traces`。
完整片段见 [`stack/config/tracing.example.json`](stack/config/tracing.example.json)，
部署说明见 `docs/zh/user_guide/features/tracing.md`。

**Profiling**：Engine 侧执行 `pip install ms_service_metric`，启动前设置
`PROMETHEUS_MULTIPROC_DIR=/dev/shm/vllm_metrics`，Engine ready 后执行 `ms-service-metric on`。
未开启时 `vllm_profiling_*` 指标不会产生，「引擎性能剖析」看板无数据。

### 1.4 CCAE Reporter 配置（可选）

在 `user_config.json` 中增加 CCAE 配置。Controller observability 用于提供告警和实例清单；
以下示例使用不加密的北向连接，生产环境启用 TLS 时需填写实际证书路径：

```json
{
  "motor_controller_config": {
    "observability_config": {
      "observability_enable": true
    }
  },
  "motor_deploy_config": {
    "tls_config": {
      "north_tls_config": {
        "enable_tls": false,
        "ca_file": "",
        "cert_file": "",
        "key_file": "",
        "passwd_file": "",
        "crl_file": ""
      }
    }
  },
  "north_config": {
    "name": "ccae_reporter",
    "ip": "192.0.2.1",
    "port": 31948
  }
}
```

> `192.0.2.1` 是文档占位 IP，请替换为实际 CCAE 平台 IP。Reporter 启动时会校验 `north_config.ip`；
> 使用无法通过 IP 地址校验的占位符会导致 Reporter 进程退出。
> Reporter 可以等待 `north_config` 首次出现后建立连接；连接建立后修改 IP / 端口不会热生效，
> 需要重启 Reporter 进程。Reporter 由 deployer 部署的启动脚本拉起，无需手动启动。

环境变量：

| 环境变量 | 说明 |
|----------|------|
| `CCAE_REPORTER_LOG_LEVEL` | Reporter 自身日志级别。可选 `DEBUG` / `INFO` / `WARN` / `ERROR` / `CRITICAL` / `NULL`（关闭文件日志）。未设置或为空时为 `INFO`；非法非空值输出 warning 并回退 `INFO`；`WARNING` 视为 `WARN` |
| `MOTOR_LOG_PATH` | Reporter 自身日志根目录与采集根目录，未设置时默认 `/root/ascend/log`；自身日志文件写入其中的 `ccae_reporter/` 子目录 |

日志采集只监控 Controller / Coordinator 日志，文件名需匹配 `*-controller-*.log` /
`*-coordinator-*.log`（对应默认开启 `logging_config.log_collector_enabled` 后的 `{HOSTNAME}.log`）。
关闭 collector 后的 `{HOSTNAME}_{pid}.log` 不会被采集。

修改配置后在 `examples/deployer` 目录更新：

```bash
cd examples/deployer

# 方式一：指定配置目录（推荐）
python3 deploy.py --config_dir ../../model_configs/vllm --update_config

# 方式二：单独指定配置文件
python3 deploy.py --user_config_path ../../model_configs/vllm/user_config.json --env_config_path ../../model_configs/vllm/env.json --update_config
```

CCAE Reporter 独立打包：在本目录执行 `bash build.sh` 生成 wheel（入口命令 `ccae_reporter`）。

### 1.5 镜像准备

minimal 模式需 Prometheus / Grafana / Tempo / OTel Collector / Loki；full 模式额外需要
node-exporter / cAdvisor（NPU 场景可选 npu-exporter）。镜像版本在
[`stack/.env.example`](stack/.env.example) 中集中配置。拉取方式、代理分工（拉镜像用 shell 代理，
发现阶段关闭代理）与推荐流程见
[`stack/SERVICE_GUIDE.md` §2](stack/SERVICE_GUIDE.md#2-准备镜像联网拉取)。

## 2. 运行

### 2.1 拉起观测栈

统一入口为 `launch.sh`（会先做目标发现，再启动栈）：

```bash
cd examples/features/observability/stack

# 联调推荐：minimal 栈
MOTOR_NAMESPACE=<namespace> ./launch.sh --minimal
```

常用变体：

| 命令 | 场景 |
|------|------|
| `MOTOR_NAMESPACE=<ns> ./launch.sh --full` | 完整栈（在 minimal 基础上额外含 node-exporter / cAdvisor） |
| `MOTOR_NAMESPACE=<ns> ./launch.sh --minimal --node-ip <ip>` | 指定 NodePort 访问 IP |
| `MOTOR_NAMESPACE=<ns> ./launch.sh --discover-only` | 只做发现，写出 `generated/*`，不启动栈 |
| `MOTOR_NAMESPACE=<ns> ./launch.sh --dry-run` | 发现并打印生成的 Prometheus 配置 |
| `MOTOR_NAMESPACE=<ns> ./launch.sh --native` | 无 Docker 时使用原生二进制 runtime（不含 Loki 与 infra exporters，需直连发现到的指标端点） |

参数与环境变量完整说明见 [`stack/SERVICE_GUIDE.md` §3](stack/SERVICE_GUIDE.md#3-拉起服务launchsh-统一入口)。

### 2.2 默认端口

| 组件 | 默认端口 | 用途 |
|------|----------|------|
| Grafana | `3000` | 浏览器访问看板 |
| Prometheus | `9090` | 指标查询与 targets |
| Tempo | `3200` | Trace 查询 API |
| OTel Collector gRPC | `4317` | OTLP gRPC 上报 |
| OTel Collector HTTP | `4318` | OTLP HTTP `/v1/traces` |
| Loki | `3100` | 日志数据源（Docker minimal / full 包含；native 不含） |
| Coordinator observability | `1027` | Coordinator typed metrics |
| Engine business `/metrics` | `10000` | `launch.sh` 默认抓取端口，可用 `MOTOR_ENGINE_BUSINESS_PORT` 修改 |
| Engine management `/metrics` | `10001` | 静态 Prometheus 模板使用的端口 |

### 2.3 停止与重置

```bash
cd examples/features/observability/stack

# 停止 Docker Compose 与 native runtime，并清理主机端口转发
./stop.sh

# 同时清空数据卷 / native 数据（彻底重置）
./stop.sh --purge
```

## 3. 结果查看

### 3.1 健康检查（预期输出样例 1）

```bash
curl -s http://localhost:9090/-/healthy
curl -s -u motor:motor http://localhost:3000/api/health
curl -s http://localhost:3200/ready
```

预期输出（`<...>` 为随镜像版本变化的占位）：

```text
$ curl -s http://localhost:9090/-/healthy
Prometheus Server is Healthy.

$ curl -s -u motor:motor http://localhost:3000/api/health
{"commit":"<commit>","database":"ok","version":"<grafana-version>"}

$ curl -s http://localhost:3200/ready
ready
```

### 3.2 抓取目标与指标（预期输出样例 2）

```bash
curl -s http://localhost:9090/api/v1/targets \
  | python3 -c "import json,sys; [print(t['labels'].get('job'), t['health']) for t in json.load(sys.stdin)['data']['activeTargets']]"
```

预期输出节选（Coordinator 各视图与 Engine 目标应为 `up`；未启用的 full / NPU exporter 允许为 `down`）：

```text
prometheus up
motor-coordinator up
motor-coordinator-instance up
motor-coordinator-role-prefill up
motor-coordinator-role-decode up
motor-coordinator-dp up
motor-coordinator-node up
motor-engine up
vllm-profiling up
...
```

确认有数据后可查询指标：

```bash
curl -sG http://localhost:9090/api/v1/query \
  --data-urlencode 'query=count(up{motor_component=~"coordinator|engine"})'
```

预期输出（`<ts>` 为 Unix 时间戳，`<up-target-count>` 为当前 UP 的 Coordinator / Engine 抓取目标总数；
Coordinator 含多个指标视图，Engine 数量取决于发现到的实例数，均不是固定值）：

```json
{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[<ts>,"<up-target-count>"]}]}}
```

### 3.3 Grafana 看板

- 访问 <http://localhost:3000>，默认账号 `motor` / `motor`。
- 看板变量因看板而异：`cluster`（指标总览、引擎性能剖析）、`source`（KV 缓存、引擎性能剖析）、
  `role` / `pd_role` / `instance_id` 等；按各看板顶部变量栏选择，不要套用到所有看板。
- 内置看板：`motor-all-metrics`（指标总览）、`motor-kv-cache`（KV 缓存）、
  `motor-vllm-profiling`（引擎性能剖析，需 §1.3 开启 Profiling）。
- 页面布局、数据源与新增指标方法见 [`stack/GRAFANA_GUIDE.md`](stack/GRAFANA_GUIDE.md)。

也可在 Grafana **Explore** 中直接写 PromQL 验证，例如：

```bash
# 列出包含指定关键字的指标名
curl -s http://localhost:9090/api/v1/label/__name__/values | tr ',' '\n' | grep -i motor
```

### 3.4 Trace 与日志

- Trace：Tempo 查询 API `http://localhost:3200`，或 Grafana 中选择 Tempo 数据源按服务名查询；
  需按 §1.3 完成 Tracing 接入。
- 日志：Docker minimal / full 均提供 Loki 数据源（native 不提供），地址为 `http://localhost:3100`。
  Grafana 中按 `{service_name=~"mindie-motor-coordinator|vllm-server-.*"}` 等标签检索（标签来自上游
  `OTEL_SERVICE_NAME`，Coordinator 建议为 `mindie-motor-coordinator`、引擎为 `vllm-server-p/d`，
  以及 `deployment.environment`）；可先用 `curl -s http://localhost:3100/loki/api/v1/labels` 确认实际标签。

### 3.5 发现产物（排障用）

| 文件 | 内容 |
|------|------|
| `stack/generated/discovery-summary.txt` | 发现摘要（namespace、Node IP、识别到的端点） |
| `stack/generated/discovered.env` | `OBS_HOST`、`PORT_FORWARD_*` 等 |
| `stack/generated/prometheus.yml` | 自动生成的 scrape 配置 |

### 3.6 CCAE 上报确认

- Reporter 自身日志位于 `${MOTOR_LOG_PATH:-/root/ascend/log}/ccae_reporter/`，按 `CCAE_REPORTER_LOG_LEVEL` 控制级别。
- CCAE 侧可看到纳管的 Motor 实例信息、告警、日志与 metrics；采集范围为
  `*-controller-*.log` / `*-coordinator-*.log`。

## 4. 常见排错

| 现象 | 处理建议 |
|------|----------|
| `kubectl is unavailable` / 超时 | 发现阶段不要挂 HTTP 代理；确认 `MOTOR_NAMESPACE` 正确 |
| `docker pull` / `compose pull` 超时 | 拉镜像前 enable 代理；内网预拉后设 `OBS_COMPOSE_PULL=never` |
| Grafana 看板 500 / 504 | 确认容器内 `HTTP_PROXY` 为空（`docker exec pymotor-grafana printenv HTTP_PROXY`） |
| Dashboard 无曲线 / No Data | 重新 `./launch.sh` 触发发现；切换环境先 `./stop.sh`，勿复用旧 `generated/discovered.env` |
| `motor-coordinator` / `motor-engine` 为 DOWN | 检查观测机到 Pod 网段 / NodePort 可达性，以及 `generated/discovered.env` 中的 `PORT_FORWARD_*` |
| 无 Docker 环境 | 使用 `./launch.sh --native` |
| 看板无 `vllm_profiling_*` | 按 §1.3 开启 `ms_service_metric` 并执行 `ms-service-metric on` |

完整排错表见 [`stack/SERVICE_GUIDE.md` §6](stack/SERVICE_GUIDE.md#6-常见问题总结)。

## 5. 目录结构与文档导航

```text
examples/features/observability/
├── README.md            # 本文件：准备 → 运行 → 结果查看
├── build.sh             # 打包 ccae_reporter wheel
├── setup.py
├── ccae_reporter/       # CCAE 上报组件源码
├── k8s/                 # ServiceMonitor / 裸 Prometheus / HPA 对接示例
└── stack/               # 一键观测栈
    ├── README.md        # 栈总览与文档导航
    ├── SERVICE_GUIDE.md # 前提条件、镜像准备、launch.sh、验收、停止、常见问题
    ├── GRAFANA_GUIDE.md # Grafana 登录、数据源、看板、新增指标
    ├── launch.sh / start.sh / stop.sh
    └── docker-compose.yml
```

| 文档 | 内容 |
|------|------|
| [`stack/README.md`](stack/README.md) | 观测栈总览与快速开始 |
| [`stack/SERVICE_GUIDE.md`](stack/SERVICE_GUIDE.md) | 服务拉起与停止指导（含镜像、代理、排错） |
| [`stack/GRAFANA_GUIDE.md`](stack/GRAFANA_GUIDE.md) | Grafana 使用与看板扩展 |
| [`k8s/README.md`](k8s/README.md) | K8s 指标采集与 HPA 对接 |
| [`examples/deployer/README.md`](../../deployer/README.md) | Motor 部署工具说明 |
