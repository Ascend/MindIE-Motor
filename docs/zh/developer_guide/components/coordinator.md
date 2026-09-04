# Coordinator（协调器）

## 功能介绍

Coordinator 进程入口为 `motor/coordinator/main.py`：异步 `main()` 中构造 `CoordinatorConfig.from_json()`，按需 `reconfigure_logging`，再创建并运行 **`CoordinatorDaemon`**（`motor/coordinator/daemon/coordinator_daemon.py`）。

`CoordinatorDaemon` 负责统一管理三类子进程（键名见 `motor/coordinator/process/constants.py`）：

| 进程字典键（常量名 / 值） | 管理类 | 说明 |
|--------|--------|------|
| `PROCESS_KEY_MGMT`（`"MgmtProcess"`） | `MgmtProcessManager` | 管理 HTTP + 控制面（实例成员表、熔断、精度全局门闩、schema-4 负载 SHM、ZMQ ROUTER/PUB） |
| `PROCESS_KEY_OBS`（`"ObsProcess"`） | `ObsProcessManager` | 可观测性 API 进程 |
| `PROCESS_KEY_INFERENCE`（`"InferenceWorkers"`） | `InferenceProcessManager` | 推理 Worker：OpenAI/Anthropic HTTP；调度热路径在本地打分后对 Rust POSIX SHM 做 CAS 记账 |

没有独立 Scheduler 子进程。IPC 路径名仍为 `scheduler_frontend` / `scheduler_instance_pub`（由 Mgmt bind，历史命名未改）。

未启用主备时，按 `START_ORDER` 启动 Mgmt → Obs → Inference。启用 `standby_config.enable_master_standby` 时，通过
`StandbyManager` 的 `on_become_master` / `on_become_standby` 仅在主机上启停 Infer 相关子进程；
Mgmt 与 Obs 在主备两侧都运行。并可配合共享内存 `RoleShmHolder` 写入角色字节（详见同文件注释）。

启停常量来自 `motor/coordinator/process/constants.py`：

- `START_ORDER = [MgmtProcess, ObsProcess, InferenceWorkers]`
- `STOP_ORDER = [InferenceWorkers, ObsProcess, MgmtProcess]`

也即：**停止顺序与启动顺序相反**，先收 Inference 流量、再停 Obs、最后停 Mgmt（控制面与 SHM 生命周期随 Mgmt 结束）。

子进程由 `SubprocessSupervisor` 监控；Daemon 主循环中处理信号与退出（见 `CoordinatorDaemon.run` 后半部分）。

推理面 OpenAI 兼容路径见 [服务接口](../../user_guide/api/service_interfaces.md)。vLLM layerwise metaserver 行为见 [PD 分离](../../design/pd_disaggregation.md)。
调度热路径（Rust SHM CAS、删除 Scheduler 进程）见 [Coordinator 调度热路径 Rust 重构](../../design/coordinator_scheduler_rust.md)。

### 路由前 Token 处理

启用 `render_config` 后，Coordinator 在路由前优先使用本 Pod 的 vLLM Render Sidecar 处理 Chat Completions 和 Completions，并将 token ID 复用于 context budget、KV Cache 亲和调度和 P/D 路由。即使同时启用 context budget，本地 tokenizer 也延迟到 Render fallback 时加载；Render 不可用或响应无效时自动回退，不影响 Coordinator 启动。

Render 产生的非流式 Chat Completions 和 Completions 请求支持完整 Token In/Token Out 链路。`context_budget_mode=on` 时，最终输出预算会同步到 Render 参数。配置与边界说明见
[配置参考](../../user_guide/configuration/config_reference.md)、
[max_tokens 自适应](../../user_guide/features/max_tokens_adaptation.md) 与
[KV Cache 亲和调度](../../user_guide/features/kvcache_affinity.md)。

## 环境准备

- 配置来源：`CoordinatorConfig.from_json()`，通常来自挂载的 `user_config.json` 中 **`motor_coordinator_config`** 等合并结果，字段定义见 `motor/config/coordinator.py`。独立部署时入口只读环境变量 `USER_CONFIG_PATH`。
- 部署与端口约定见 [配置参考：motor_coordinator_config](../../user_guide/configuration/config_reference.md) 与 [接口说明](../../user_guide/api/README.md)。
- 负载 SHM 需要 `libmindie_workload_shm.so`（`bash build.sh` 会 cargo 构建）。缺失时启动响亮失败，不会回退到 Python 账本。
- 不依赖 Controller / Node Manager 的拉起步骤见 [Coordinator 独立部署](../../user_guide/deployment/standalone.md)。

## 配置说明

请以 [配置参考](../../user_guide/configuration/config_reference.md) 中 **`motor_coordinator_config`** 章节为权威字段说明。代码中与 Daemon 强相关的包括：

- `standby_config.enable_master_standby`：是否走主备与 Infer 启停分支。
- `scheduler_config`：`scheduler_type` 等。推理 Router 由当前实例角色与 `dispatch_capabilities` 动态选择，不再读取 `deploy_mode`（见 [PD 分离](../../design/pd_disaggregation.md)）。
- `inference_workers_config.worker_metaserver_base_port`：vLLM layerwise/trigger 时每 Worker 独立 metaserver 端口；默认 `12000`，设为 `0` 关闭。监听地址优先 `POD_IP`，否则用 `coordinator_api_host`。端口冲突时推理口继续，Trigger 返回 503。
- `api_config`：推理端口、管理端口等（与 `interface_description.md` 一致处为准）。

## 使用样例

```bash
python -m motor.coordinator.main
```

入口无额外 argparse；配置路径由 `CoordinatorConfig` 内部解析逻辑决定（若存在 `config_path` 会在日志中打印）。

## 报错与日志

- `main.py` 在启动失败时记录 `Server startup failed` 及 traceback，并以退出码 `1` 结束。
- 子进程崩溃、重启等行为由 `SubprocessSupervisor` 与各类 `ProcessManager` 记录日志，需结合 Pod 内日志排查。
- Worker 日志出现 native workload SHM 无法 attach 时，检查 Mgmt 是否已创建 SHM、以及镜像是否带上 `libmindie_workload_shm.so`。

## 相关设计

调度热路径实现说明见 [Coordinator 调度热路径 Rust 重构](../../design/coordinator_scheduler_rust.md)。
