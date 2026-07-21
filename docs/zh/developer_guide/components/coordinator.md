# Coordinator（协调器）

## 功能介绍

Coordinator 进程入口为 `motor/coordinator/main.py`：异步 `main()` 中构造 `CoordinatorConfig.from_json()`，按需 `reconfigure_logging`，再创建并运行 **`CoordinatorDaemon`**（`motor/coordinator/daemon/coordinator_daemon.py`）。

`CoordinatorDaemon` 负责统一管理三类子进程（键名见 `motor/coordinator/process/constants.py`）：

| 进程字典键（常量名 / 值） | 管理类 | 说明（来自 `CoordinatorDaemon` 模块文档字符串与 `run` 实现） |
|--------|--------|---------------------------------------------------------------|
| `PROCESS_KEY_SCHEDULER`（`"SchedulerProcess"`） | `SchedulerProcessManager` | 调度器进程 |
| `PROCESS_KEY_MGMT`（`"MgmtProcess"`） | `MgmtProcessManager` | 管理面 API 进程 |
| `PROCESS_KEY_INFERENCE`（`"InferenceWorkers"`） | `InferenceProcessManager` | 推理 Worker 进程（含推理 HTTP、可选 metaserver 端口等） |

启动顺序上，文档写明：**先 Scheduler，再 Mgmt**，以便 Mgmt 能成功 `connect`。未启用主备时，在 Scheduler/Mgmt 之后会启动 Inference。启用 `standby_config.enable_master_standby` 时，通过 `StandbyManager` 的 `on_become_master` / `on_become_standby` 仅在主机上启停 Infer 相关子进程；并可配合共享内存 `RoleShmHolder` 写入角色字节（详见同文件注释）。

启停常量来自 `motor/coordinator/process/constants.py`：

- `START_ORDER = [SchedulerProcess, MgmtProcess, InferenceWorkers]`
- `STOP_ORDER = [InferenceWorkers, MgmtProcess, SchedulerProcess]`

也即：**停止顺序与启动顺序相反**，先收 Inference 流量、再停 Mgmt、最后停 Scheduler，避免在停止过程中产生悬空连接。

子进程由 `SubprocessSupervisor` 监控；Daemon 主循环中处理信号与退出（见 `CoordinatorDaemon.run` 后半部分）。

推理面 OpenAI 兼容路径与 metaserver 行为见 [服务接口](../../user_guide/api/service_interfaces.md)。

## 环境准备

- 配置来源：`CoordinatorConfig.from_json()`，通常来自挂载的 `user_config.json` 中 **`motor_coordinator_config`** 等合并结果，字段定义见 `motor/config/coordinator.py`。
- 部署与端口约定见 [配置参考：motor_coordinator_config](../../user_guide/configuration/config_reference.md) 与 [接口说明](../../user_guide/api/README.md)。

## 配置说明

请以 [配置参考](../../user_guide/configuration/config_reference.md) 中 **`motor_coordinator_config`** 章节为权威字段说明。代码中与 Daemon 强相关的包括：

- `standby_config.enable_master_standby`：是否走主备与 Infer 启停分支。
- `scheduler_config`：`deploy_mode`、`scheduler_type` 等，影响推理路由（见 [PD 分离](../../design/pd_disaggregation.md)）。
- `api_config`：推理端口、管理端口等（与 `interface_description.md` 一致处为准）。

## 使用样例

```bash
python -m motor.coordinator.main
```

入口无额外 argparse；配置路径由 `CoordinatorConfig` 内部解析逻辑决定（若存在 `config_path` 会在日志中打印）。

## 报错与日志

- `main.py` 在启动失败时记录 `Server startup failed` 及 traceback，并以退出码 `1` 结束。
- 子进程崩溃、重启等行为由 `SubprocessSupervisor` 与各类 `ProcessManager` 记录日志，需结合 Pod 内日志排查。
