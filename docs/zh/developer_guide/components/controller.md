# Controller（控制器）

## 功能介绍

Controller 进程入口为 `motor/controller/main.py`。启动时加载 `ControllerConfig`（来自 `--config` 指定文件，或未指定时由环境变量等解析的 JSON，与 `ControllerConfig.from_json` 行为一致），并依次初始化下列模块（见 `init_all_modules`）：

| 模块名 | 源码 | 职责摘要 |
|--------|------|----------|
| `InstanceAssembler` | `motor/controller/core/instance_assembler.py` | 处理 Node Manager 上报的注册/重注册，组装实例 |
| `EventPusher` | `motor/controller/core/event_pusher.py` | 作为 `InstanceManager` 的观察者推送事件 |
| `FaultManager` | `motor/controller/fault_tolerance/fault_manager.py` | 仅当 `fault_tolerance_config.enable_fault_tolerance` 为真时加载；可挂接到 `InstanceManager` 并 `start()` |
| `InstanceManager` | `motor/controller/core/instance_manager.py` | 维护实例字典、心跳、状态与 etcd 等持久化（类文档字符串中列出的职责） |
| `Observability` | `motor/controller/observability/observability.py` | 仅当 `observability_config.observability_enable` 为真时加载 |
| `ControllerAPI` | `motor/controller/api_server/controller_api.py` | 在独立线程中启动 FastAPI/uvicorn，对外提供管理面 HTTP API |

若启用主备（`standby_config.enable_master_standby`），则先仅启动 `ControllerAPI`，其余业务模块在 `StandbyManager` 回调 `on_become_master` 时再 `start`；切为备机时 `on_become_standby` 会停止除 `ControllerAPI` 外的模块（见 `main.py` 中注释）。

配置热更新：当 `config.config_path` 存在且文件在磁盘上时，会启动 `ConfigWatcher`；重载后调用 `on_config_updated`，可对各模块执行 `update_config`，并在故障开关变化时启动或停止 `FaultManager`。

## 环境准备

- 运行所需 JSON 配置需满足 `ControllerConfig` 的字段定义，见 `motor/config/controller.py`。
- 与集群部署相关的挂载路径、环境变量等与 `examples/deployer` 生成的启动脚本及 ConfigMap 一致；部署层面步骤见 [环境准备](../../user_guide/environment_preparation.md) 与 [配置参考](../../user_guide/configuration/config_reference.md)。

## 配置说明

用户侧总配置中对应块为 **`motor_controller_config`**（文档中键名以 `config_reference.md` 为准）。代码中常用项包括：

- **API**：`api_config.controller_api_host`、`controller_api_port`（默认 `1026`）、`observability_api_port`（默认 `1027`）等，见 `motor/config/controller.py` 中 `ApiConfig`。
- **TLS**：`mgmt_tls_config`、`observability_tls_config` 等；`ControllerAPI` 在 `mgmt_tls_config.enable_tls` 为真时使用 HTTPS 启动主 API。
- **主备**：`standby_config.enable_master_standby`。
- **可观测**：`observability_config.observability_enable` 控制库存盘点/指标/告警等 HTTP 能力是否在独立 observability 应用上开放（见 `_create_observability_app` 与装饰器 `observability_enabled_required`）。

完整字段表请以 [配置参考：motor_controller_config](../../user_guide/configuration/config_reference.md) 为准。

## 对外 HTTP 路由（主 API）

`ControllerAPI._create_app` 注册的路由包括（方法以代码为准）：

| 方法 | 路径 | 处理逻辑要点 |
|------|------|----------------|
| `POST` | `/controller/heartbeat` | 解析 `HeartbeatMsg`，`InstanceManager().handle_heartbeat` |
| `POST` | `/controller/register` | 解析 `RegisterMsg`（含 `nnodes` 字段，用于跨节点 PCP 场景），`InstanceAssembler().register` |
| `POST` | `/controller/reregister` | 解析 `ReregisterMsg`，`InstanceAssembler().reregister` |
| `POST` | `/controller/terminate_instance` | 解析 `TerminateInstanceMsg`，向各 Node Manager 下发 stop 等 |
| `GET` | `/startup`、`/readiness`、`/liveness` | 探针类接口（实现见同文件内对应 handler） |
| `POST` | `/observability/add_alarm` | 上报告警记录 |

另有一套 **Observability** 独立 FastAPI 应用（端口 `observability_api_port`），在 `observability_enable` 为真时提供如 inventory、metrics、alarms 等路由；未启用时相关接口返回「Observability is not enabled.」类错误（见 `observability_enabled_required`）。

### Observability 应用路由

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/observability/inventory` | 获取服务库存信息 |
| `GET` | `/observability/metrics` | 获取监控指标（**已弃用**，请使用 Coordinator 的 `/metrics` 接口） |
| `GET` | `/observability/alarms` | 获取告警信息，支持 `source_id` 查询参数过滤 |

> [!WARNING] 已弃用
> `GET /observability/metrics` 已弃用，将在后续版本移除。请改为直接访问 Coordinator 的 `GET /metrics?type={type}&role={role}` 接口。Coordinator 的地址和端口见 [Coordinator 指标查询接口](../../user_guide/api/monitoring_interfaces.md#指标查询接口)。

## 使用样例

```bash
# 指定配置文件（与 main.py 中 argparse 一致）
python -m motor.controller.main --config /path/to/controller_config.json
```

非交互环境下主循环可能阻塞在 `select`/`wait`；退出可通过 SIGINT/SIGTERM（`signal_handler`）或交互输入 `stop`。

### RegisterMsg 字段

`POST /controller/register` 请求体（`RegisterMsg`）新增字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `nnodes` | int | 1 | 跨节点 PCP 期望节点数，来自 `engine_config` 中的 `nnodes` 配置。**仅在 `nnodes > 1` 时启用跨节点 PCP 组装逻辑，此时就绪条件从 `total_endpoints == dp_size` 切换为 `node_managers_count >= nnodes`**。 |

`nnodes=1` 时行为与既有逻辑完全一致（向后兼容）。

## 报错与日志

- API 层对非法 body 多返回 JSON 中带 `"error"` 字段的说明，例如 `"Invalid HeartbeatMsg format"`、`"Instance not found"` 等（见各 `_heartbeat`、`_register` 等实现）。
- 控制器侧日志使用 `motor.common.logger`；具体落盘路径由 `LoggingConfig` 决定，见 `motor_controller_config` 中的日志相关配置。
