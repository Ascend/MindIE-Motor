# pyMotor 可观测性一键栈

本目录提供 Prometheus、Grafana、Tempo、OTel Collector 等组件的一键发现与拉起能力。

## 文档导航

| 文档 | 说明 |
|------|------|
| [SERVICE_GUIDE.md](SERVICE_GUIDE.md) | 前提条件、镜像准备、`launch.sh` 拉起与停止、常见问题 |
| [GRAFANA_GUIDE.md](GRAFANA_GUIDE.md) | Grafana 页面、数据源、看板设计与新增指标步骤 |

## 快速开始

```bash
cd examples/features/observability/stack
MOTOR_NAMESPACE=<namespace> ./launch.sh --minimal
```

Grafana 默认：<http://localhost:3000>（`motor` / `motor`）。

## 代理 / 内网拉镜像

内网需代理访问外网时，请区分 **Docker 拉镜像**（当前 shell 的 `HTTP_PROXY`）与 **Native 下载二进制**（`.env` 中的 `PROXY_SH`）；发现阶段建议关闭代理。完整说明见 [SERVICE_GUIDE.md §2.4](SERVICE_GUIDE.md#24-代理配置)。
