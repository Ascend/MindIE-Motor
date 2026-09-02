# Motor Node Manager（节点管理器）

## 功能介绍

Node Manager 是部署在推理节点上的管理进程，负责连接 Controller 与本节点的原生 vLLM/SGLang 引擎。进程入口为 `motor/node_manager/main.py`，核心逻辑在 `motor/node_manager/node_manager.py`（`NodeManager` 类），继承自 `motor/common/app/application.py`（`Application` 基类）。主要职责如下：

1. 加载节点配置，完成端口分配并启动管理面 HTTP 服务。
2. 向 Controller 注册节点，接收 Controller 下发的实例启动命令。
3. 按 endpoint 直接拉起、监管和停止原生引擎进程组。
4. 轮询原生业务端口的健康接口并向 Controller 上报心跳。
5. 处理优雅暂停、配置热更新、容器快照恢复和软件故障上报。

### 组件结构

| 对象 | 源码 | 职责 |
|------|------|------|
| `Application` | `motor/common/app/application.py` | 基类：模块管理、配置热更新传播、daemon loop、信号处理，封装 Controller / NodeManager 共享的 boilerplate |
| `NodeManager` | `motor/node_manager/node_manager.py` | `Application` 子类：组装模块并运行 daemon loop，每 tick 检查自杀标志 |
| `NodeManagerConfig` | `motor/config/node_manager.py` | 加载、校验和重载节点配置，推导 endpoint 数量与端口 |
| `NodeManagerAPI` | `motor/node_manager/api_server/node_manager_api.py` | 在后台线程中运行 FastAPI/uvicorn，提供启动、停止和探针接口 |
| `Daemon` | `motor/node_manager/core/daemon.py` | 服务编排器：根据配置发现并实例化原生 Engine 与 KV-store 服务，维护进程监控器与自杀仲裁线程，持有 FaultReporter |
| `NativeEngineService` | `motor/node_manager/core/services/native_engine/service.py` | 原生引擎子进程生命周期管理：构造 `LaunchContext`、拉起/追踪/停止 vLLM/SGLang 进程组、重拉编排（`restart`）、`/health` 就绪等待（`wait_ready`） |
| 启动加速适配 | `motor/node_manager/core/services/native_engine/startup_acceleration.py` | 将 Motor 配置映射为 vLLM StartPlan/图复用环境变量与引擎覆盖项，并在拉起前完成 StartPlan 候选文件的 DFX 预检查 |
| `LocalService` | `motor/node_manager/core/services/memcache/lifecycle.py` | memcache 后端生命周期管理：配置准备、子进程拉起（通过 `memcache/worker.py`）、健康检查与重启 |
| `RegisterManager` | `motor/node_manager/core/register_manager.py` | 注册/重注册、校验启动命令、处理 ranktable、快照元数据、持久化引擎重拉参数 |
| `HeartbeatManager` | `motor/node_manager/core/heartbeat_manager.py` | 轮询 endpoint 状态、上报心跳、维护暂停/恢复状态；仅报告状态事实，自杀裁决在 Daemon |
| `FaultReporter` | `motor/node_manager/core/fault_reporter.py` | 轮询引擎 FT 状态接口并上报软件故障给 Controller；由 Daemon 持有，重拉期间暂停/恢复 |
| `ControllerApiClient` | `motor/node_manager/api_client/controller_api_client.py` | 调用 Controller 的注册、重注册、心跳和故障上报接口 |

`Daemon`、`RegisterManager` 和 `HeartbeatManager` 均为线程安全单例。HTTP 路由和后台线程通过这些单例共享实例、endpoint 和进程状态。`Application` 和 `NodeManager` 不是单例，由 `main.py` 显式创建。

## 生命周期

### 启动

启动流程由 `Application.run()` 模板方法驱动，`main.py` 仅负责配置加载和入口调用：

1. 模块级 `set_process_title("NodeManager")` 设置进程名。
2. `main()` 加载 `NodeManagerConfig`，配置日志，执行端口分配。
3. 创建 `NodeManager(config)` 并调用 `run()`，内部执行：
   a. `init_modules()` — 根据 `Daemon.has_engine` 动态注册模块：`Daemon`、`NodeManagerAPI`，以及有 Engine 时才注册的 `RegisterManager` 和 `HeartbeatManager`。
   b. `_start_config_watcher()` — 非快照模式下启动配置文件 watcher；快照模式跳过。
   c. `setup_signal_handlers()` — 注册 SIGINT / SIGTERM。
   d. `_daemon_loop()` — select-based 主循环，每 `daemon_loop_interval` 秒检查自杀标志和 stdin 输入。
4. 各模块的初始化行为不变：`NodeManagerAPI.__init__` 在后台线程中启动 FastAPI，`RegisterManager.__init__` 启动注册线程，`Daemon.__init__` 启动进程监控线程。

首次注册最多尝试 5 次，重试间隔为 2、4、8、16 秒。连续失败后，`RegisterManager` 向当前进程发送 `SIGTERM`。

### 启动实例

Controller 调用 `POST /node-manager/start` 后，处理流程为：

1. 将请求体解析为 `StartCmdMsg`。
2. 校验 `job_name`、endpoint 数量以及每个 endpoint 的 IP 是否与本节点配置一致。
3. 保存 `instance_id`、endpoints、`node_rank` 和 D2D peer 信息；如配置了 `RANKTABLE_PATH`，将实例 ranktable 写入该文件。
4. 准备快照运行目录和元数据。
5. `Daemon.pull_engine()` 为每个 endpoint 拉起一个原生引擎进程组（`vllm serve` / SGLang），并启动 `Daemon` 持有的 `FaultReporter`（仅在故障容忍功能开启时生效）。
6. 更新 `HeartbeatManager` 中的 endpoint，并启动状态轮询和心跳线程。

从宿主机侧快照恢复时，第 5 步不会再次拉起引擎，而是更新恢复元数据、endpoint 和恢复状态。

### 停止与重调度

- 收到 `SIGINT`、`SIGTERM` 或标准输入命令 `stop` 时，`Application._handle_signal()` 设置 `stop_event`，daemon loop 退出后执行 `shutdown()`：按注册逆序调用每个模块的 `stop()`，然后停止配置 watcher。
- `Daemon.stop()` 遍历所有 service 调用 `stop()`：`NativeEngineService.stop()` 对记录的原生引擎进程组发送 `SIGKILL`；`LocalService.stop()` 对 memcache worker 子进程发送 `SIGKILL`。
- 自杀裁决由 `Daemon` 独立 3s 仲裁线程执行：任一 endpoint 连续 5 轮观察保持 `ABNORMAL`（约 15s）时设置自杀标志（引擎重拉/死亡上报的冻结窗口内暂停计数）。daemon loop 每 tick 检查该标志，触发后 `stop_event.set()` 并返回 `-1`，用于触发重调度。
- `exit_code` 默认返回 `-1`，与旧行为一致（-1 表示 rescheduling）。

## Node Manager HTTP API

Node Manager API 默认监听 `api_config.pod_ip:api_config.node_manager_port`。启用 `mgmt_tls_config.enable_tls` 后使用 HTTPS。

| 方法 | 路径 | 响应 | 说明 |
|------|------|----------|------|
| `POST` | `/node-manager/start` | `200 {}` | 校验启动命令并拉起原生引擎；快照恢复时执行恢复准备 |
| `POST` | `/node-manager/stop` | `200 {"message": "All engine processes stopped successfully."}` | 停止全部原生引擎进程后延时 SIGTERM 自身（退出码 `-1` → k8s 重启 Pod），即 Controller 下发的「自杀」指令，用于实例拆除与跨机部分失联协同 |
| `POST` | `/node-manager/engine-restart` | `200 {"message": ...}` | Controller 驱动的容器内引擎重拉：body `{"action": "restart"\|"abort", "instance_id"?}`。`restart` 整体委托 `Daemon.restart_engine`（Daemon 解析启动参数、冻结自杀仲裁、暂停/恢复 FaultReporter、杀掉并重拉全部引擎，KV store 不动）；`abort` = 解冻自杀仲裁（重拉失败回退容器重启）。并发 409、快照恢复中 409、无启动记录 400、重拉失败 500 且解冻 |
| `POST` | `/node-manager/pause` | `200 {"status":"ok", ...}` | 将全部 endpoint 标记为 `PAUSED`，并返回非 headless 原生引擎的 `engine_metrics_targets` |
| `POST` | `/node-manager/resume` | `200 {"status":"ok", ...}` | 仅将 `PAUSED` endpoint 恢复为 `NORMAL` |
| `GET` | `/node-manager/status` | `200 {"status": true/false}` | 返回全部 endpoint 是否为 `NORMAL`；`relaxed=true`（引擎重拉轮询）时无 `ABNORMAL` 即 `true`；无 endpoint 时为 `false` |
| `GET` | `/readiness` | `200` 或 `503` | Kubernetes Readiness Probe 接口。实例节点 Pod 默认不配置该探针；仅在容器快照默认应用场景下配置，用于判断执行容器 checkpoint 前的稳态点。未到达稳态点时返回 `503`，到达后返回 `200` |

`/node-manager/pause` 用于 PreStop 优雅下线：暂停状态会使 readiness 失败，并通过心跳通知 Controller；原生健康轮询不会覆盖手动设置的 `PAUSED`。响应中的 `engine_metrics_targets` 使用原生业务端口，并排除 headless 成员。如果 PreStop 被取消，可调用 `/node-manager/resume` 恢复调度。

`/readiness` 仅用于快照默认应用场景，即 MindCluster 实例重调度，不作为 Node Manager 的通用健康检查接口。MindCluster 通过该接口查询实例节点是否到达稳态点。在容器快照的用户自定义应用场景中，可调用 `/node-manager/status` 查询稳态点；接口返回 `200 {"status": true}` 表示已到达稳态点。

### `StartCmdMsg` 请求字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `job_name` | string | 是 | 实例任务名，必须与本节点配置一致 |
| `role` | string | 是 | 实例角色，如 `prefill`、`decode` 或 `union` |
| `instance_id` | int | 是 | Controller 分配的实例 ID |
| `endpoints` | array | 是 | 本节点管理的 endpoint；元素包含 `id`、`ip`、`business_port`，SGLang PD endpoint 还可包含 `bootstrap_port`。原生引擎健康探测使用 `business_port` |
| `master_dp_ip` | string | 是 | 数据并行主节点 IP |
| `ranktable` | object/null | 否 | 实例级 ranktable，默认 `null` |
| `d2d_peer_ips` | array/null | 否 | D2D 权重传输对端，Controller 使用 `<endpoint_id>:<peer_ip>` 编码，默认 `null` |
| `node_rank` | int | 否 | Controller 按注册顺序分配的节点序号，默认 `0` |

当前接口错误码如下：

- 启动命令内部解析异常：`400 Invalid start command payload`。
- `job_name`、endpoint 数量或 endpoint IP 校验失败：`422 Start command validation failed`。
- 原生引擎拉起失败：`500 Failed to start native engine`。
- 请求 JSON/Pydantic 字段解析异常会被外层异常处理转换为通用 `500`。
- `/readiness` 在 endpoint 尚未健康或快照恢复后尚未启动时返回 `503`。

## 与外部组件的通信

### Controller

| 方向 | Controller 接口 | 行为 |
|------|-----------------|------|
| Node Manager → Controller | `POST /controller/register` | 上报角色、模型、端口、并行配置、ranktable、`nnodes` 和快照主节点标记 |
| Node Manager → Controller | `POST /controller/reregister` | Controller 重启并对心跳返回 `503` 时，携带实例与 endpoint 信息重新注册 |
| Node Manager → Controller | `POST /controller/heartbeat` | 按 `heartbeat_interval_seconds` 上报各 endpoint 状态 |
| Node Manager → Controller | `POST /controller/report_software_fault` | 转发引擎已有的软件故障信号 |

心跳使用长连接客户端，单次超时为 5 秒；TCP 请求失败时重建连接，并按 1 秒、2 秒退避重试两次。

### 原生引擎运行态

`HeartbeatManager` 每秒读取 `ProcessSupervisor` 的运行态。非 headless endpoint 使用原生 `business_port/health`，并沿用 `infer_tls_config`；headless 成员不强造 HTTP frontend，仅检查进程存活并上报 `WAIT2START`。Controller 仅在所有可路由 endpoint 为 `NORMAL`、所有 headless 成员至少已上报 `WAIT2START` 时将实例置为可用。

状态轮询具有以下保护逻辑：

- 进程拉起后进入 `STARTING`；在 `health_check_config.startup_timeout`（默认 1800 秒）内，业务端口尚未监听只表示模型仍在加载。
- headless 进程存活时进入 `RUNNING` 并上报 `WAIT2START`，不以进程存活冒充独立服务就绪；进程退出仍上报 `ABNORMAL`。
- 原生 `/health` 成功后进入 `READY` 并向 Controller 上报 `NORMAL`；单次请求超时仅按 `health_collector_timeout_retry_attempts` 限定次数重试，进程退出或启动窗口外重试耗尽后仍不可用则进入 `UNHEALTHY`。
- 更新 endpoint 时使用 generation 标记，避免旧探测结果覆盖 Controller 新下发的数据。
- 手动设置的 `PAUSED` 状态不会被轮询结果覆盖。

#### 虚推（虚拟推理）

启用虚推时（见 [虚推健康探测](../../user_guide/features/sim_inference.md)），`NativeEngineService` 每个实例维护**单个**虚推 monitor，**仅对 vLLM** 绑定有效的 DP0 target（`TargetIdentity` 不可变，含 instance/endpoint/host/port/engine_type；`VirtualInferenceSpec` 含 role/model/TLS/阈值/超时）。SGLang 不创建 Motor 虚推 monitor/worker：由自身在 `GET /health` 中执行生成式健康检查（拉起时强制 `SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION=true`），NodeManager/`ProcessSupervisor` 仅做现有 `/health` 心跳。pull 成功后按 spec 幂等 reconcile（相同 spec 复用、变化则替换；非 vLLM 的 desired 为 `None` 并走正常 reconcile 清理），`runtime_state(endpoint, instance_id)` 在 target 身份匹配且首次观察到原生 `/health` `READY` 后幂等启动 `VirtualInferenceWorker`（`motor/node_manager/core/services/native_engine/virtual_inference/`，由 Node Manager 直接执行，不依赖 Engine Server）：

- 虚推模块负责 vLLM 虚推请求（`POST /v1/completions`）、AI Cube 利用率采样与连续失败计数；`ProcessSupervisor` 仍只负责进程与 `/health`。
- `runtime_state()` 合并二者：虚推标记 abnormal 时，`READY` 降级为 `UNHEALTHY`，心跳照常上报 `ABNORMAL`，连续 5 次后触发自杀重调度。
- 虚推**只改变状态，不杀进程、不重启引擎**；`stop()` 与无有效 target 的 pull 会可靠清理 monitor 及其虚推线程、HTTP client。启动回滚仅在**本轮真正停止 monitor 目标进程**时清理该 monitor；若目标引擎仍存活（如 start 报 "different launch spec" 或仅非 DP0 endpoint 被回滚）则保留原 monitor 继续服务（`stop()` 与 `pull()` 通过 `_pull_lock` 串行）。

## 原生引擎启动

`NativeEngineService` 构造 `LaunchContext`，Native Engine Backend 加载角色对应配置并直接生成原生命令：

```text
vllm serve <model> <native vLLM args...>

python3 -m sglang.launch_server <native SGLang args...>
```

统一上下文和环境规则：

- 多 endpoint 模式下，按 `local_world_size` 为每个进程计算 `ASCEND_RT_VISIBLE_DEVICES`；设备编号超出末尾时循环分配。
- 单容器端口、D2D peer、角色、DP rank 和 node rank 先写入 `LaunchContext`，再由对应 Backend 映射为原生参数。
- 环境中存在 `POD_IP` 且未设置 `VLLM_HOST_IP` 时，自动设置 `VLLM_HOST_IP=POD_IP`。
- `MOONCAKE_ASCEND_IPV6_EXPERIMENT=1` 时，默认设置 `MC_USE_IPV6=1`。

endpoint 的业务端口必须处于 `[1024, 65535]`，IP 必须是合法的 IPv4 或 IPv6 地址，否则拉起失败。

### vLLM StartPlan 与图复用启动加速

角色对应的 `motor_nodemanger_config.vllm_startup_acceleration_config` 由 `NodeManagerConfig` 解析，并在
`NativeEngineService.pull()` 拉起 endpoint 前转换为子进程环境变量和引擎配置覆盖项：

| Motor 配置 | vLLM 子进程环境变量/引擎覆盖项 | 行为 |
|------------|---------------------------------|------|
| `enable_startup_plan=true` | `VLLM_ENABLE_STARTUP_PLAN=1` | 允许 vLLM 命中 Profile 后跳过 memory profiling |
| `enable_startup_plan=false` | `VLLM_ENABLE_STARTUP_PLAN=0` | 显式关闭 StartPlan 的生成与加载 |
| `enable_graph_reuse=true` | `VLLM_DISABLE_COMPILE_CACHE=0`、`enforce_eager=false`、`enable_npugraph_ex=true`；P/U 使用 `cudagraph_mode=FULL`，D 使用 `FULL_DECODE_ONLY` | 启用 vLLM-Ascend 后端编译图缓存的生成与复用 |
| `enable_graph_reuse=false` | `enable_npugraph_ex=false`、`enable_static_kernel=false` | 显式关闭后端完整图复用，不修改普通图捕获、`enforce_eager`、`cudagraph_mode` 或 AOT 行为 |
| 所有 vLLM 启动 | `VLLM_CACHE_ROOT=<resolved-cache-root>` | StartPlan、AOT 和后端编译缓存共享同一个 vLLM 缓存根目录，如果为空则使用已有 `VLLM_CACHE_ROOT`、vLLM 默认目录（`${XDG_CACHE_HOME}/vllm` 或 `${HOME}/.cache/vllm`） |

StartPlan 开启时，NodeManager 在每次 `pull()` 中只检查一次
`<cache_root>/startup_plan/startup_plan_*.json`。目录应可读写，候选文件不能是符号链接，并且应是非空、
大小不超过 1 MiB（1048576 字节）、根节点为对象的合法 JSON。

StartPlan 的最终生成、指纹匹配、可用内存校验、加载和回退由配套 vLLM/vLLM-Ascend 实现；Motor 仅负责
配置注入与启动前 DFX 检查。

### 跨节点 PCP

Node Manager 始终将 Controller 分配的 `node_rank` 和 `master_dp_ip` 交给 Native Engine Backend。

对于 vLLM，引擎配置包含 `nnodes > 1` 且配置了 `master_port`（兼容 `master-port`）时启用跨节点 PCP：

- `master_addr` 使用 `master_dp_ip`。
- `node_rank == 0` 为主节点。
- `node_rank != 0` 时启用 headless follower，仅启动工作进程。

Node Manager 从 `engine_config.nnodes` 推导每节点 `local_world_size`。当 `pcp_size` 能被 `nnodes` 整除时，每节点使用 `pcp_size / nnodes` 个 PCP rank 计算可见设备数量。`nnodes` 和 `master_port` 本身来自引擎配置，不是由 `Daemon` 追加的命令行参数。

## 配置说明

配置文件路径优先使用 `USER_CONFIG_PATH`，否则使用 `CONFIG_PATH`。在按角色组织的用户配置中，Node Manager 配置位于对应引擎块内的 **`motor_nodemanger_config`**。该键名中的 `nodemanger` 为当前兼容格式，请勿改写为 `node_manager`。

常用配置如下，完整字段参见[配置参考](../../user_guide/configuration/config_reference.md#motor_nodemanger_config)。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `api_config.pod_ip` | `Env.pod_ip` 或 `127.0.0.1` | 注册地址和 API 监听地址 |
| `api_config.node_manager_port` | `1026` | Node Manager 管理端口 |
| `endpoint_config.base_port` | `10000` | 当前控制面端口基址；业务/管理端口仍按偶数/奇数生成，管理端口将在剩余消费者迁移后删除 |
| `basic_config.heartbeat_interval_seconds` | `3` | 向 Controller 上报心跳的周期 |
| `basic_config.daemon_loop_interval` | `5.0` | daemon loop 检查间隔（秒），控制自杀标志轮询和 stdin 检查频率，支持热更新 |
| `basic_config.enable_multi_endpoints` | `true` | 是否按 DP 和设备数创建多个 endpoint |
| `basic_config.nnodes` | `1` | 从 `engine_config.nnodes` 派生的跨节点数量 |
| `kv_cache_store_config.mode` | `combined` | 部署模式：`combined` 表示 Engine 与 KV-store 在同一 Pod，`separated` 表示 KV-store 独立 Pod（不拉 Engine、不注册、不心跳） |
| `mgmt_tls_config.enable_tls` | `false` | Node Manager 与 Controller 管理面通信是否启用 TLS |
| `health_check_config.startup_timeout` | `1800` | 原生引擎模型加载启动窗口，窗口内 `/health` 未监听不判死 |
| `health_check_config.health_collector_timeout_retry_attempts` | `3` | 单次原生 `/health` 请求超时后的最大尝试次数；仅超时重试，包含首次请求 |
| `fault_tolerance_config.enable_fault_tolerance` | `false` | 显式开启软件故障轮询；引擎 user config 检测到 FT 时自动开启，无需配置 |
| `fault_tolerance_config.poll_interval_sec` | `5.0` | 轮询引擎 FT 状态的时间间隔（秒） |
| `fault_tolerance_config.poll_timeout_sec` | `5.0` | 单次轮询的 HTTP 超时（秒） |
| `fault_tolerance_config.max_poll_failures` | `3` | 连续轮询失败阈值，达到后按 `dead` 上报 |
| `snapshot_config.enable_snapshot` | `false` | 是否启用容器快照；当前仅 vLLM 原生引擎支持，SGLang 配置为 `true` 会校验失败 |
| `snapshot_config.snapshot_metadata_path` | 空 | 容器快照元数据路径；为空时使用默认路径 `/snapshot/snapshot_metadata.json` |
| `vllm_startup_acceleration_config.enable_startup_plan` | `false` | 是否为vLLM子进程开启StartPlan |
| `vllm_startup_acceleration_config.enable_graph_reuse` | `false` | 是否开启 vLLM-Ascend 后端完整图复用 |
| `vllm_startup_acceleration_config.cache_root` | 空 | 可选的绝对缓存根目录；缺省时继承环境或使用 vLLM 默认目录，生产环境建议显式配置持久化路径 |
| `port_allocator_config.enable` | `true` | 是否在启动时自动检查并调整端口 |

`endpoint_num`、`service_ports`、`device_num`、`parallel_config`、`model_name`、`engine_type` 和 `dispatch_capabilities` 主要由部署配置与引擎配置派生。`dispatch_capabilities` 不接受用户直接覆盖。

当 `pod_ip` 为空时，API 服务根据 `POD_IP` 判断监听协议族：IPv6 使用 `::`，其他情况使用 `0.0.0.0`。只有直接构造且不传入配置的 `NodeManagerAPI` 才使用内部兜底端口 `8080`，正常启动流程使用配置端口。

### 配置热更新

非快照模式下，配置 watcher 检测到文件变化后通过 `Application.on_config_updated()` 集中处理：

1. `_refresh_check_interval()` — 从配置刷新 daemon loop 间隔。
2. 遍历所有模块调用 `update_config()`：
   - `HeartbeatManager` 动态更新 `heartbeat_interval_seconds`。
   - `Daemon` 更新配置，并根据 `enable_fault_tolerance`、endpoint 的变化启停或重建其持有的 `FaultReporter`。
   - `RegisterManager` 更新配置。
3. 打印更新后的配置摘要 `log_configuration_summary()`。
4. API 监听地址、监听端口、TLS 和 `Daemon` 已缓存的设备参数不会热重启，修改后需要重启 Node Manager。

## 服务发现与后端扩展

Node Manager 使用基于装饰器的服务注册机制管理 Engine 和 KV-store 服务。相关代码位于 `motor/node_manager/core/services/`。

### 目录结构

```text
services/
  __init__.py
  protocols.py        ← DaemonService, PreparableService（接口契约）
  registry.py         ← _ServiceRegistry（服务发现与注册中心）
  native_engine/
    __init__.py
    service.py        ← NativeEngineService（原生引擎服务编排）
    startup_acceleration.py ← vLLM启动加速环境变量、引擎覆盖项和StartPlan DFX预检查
    models.py         ← LaunchContext、LaunchSpec、ProbeSpec 和 RuntimeState
    supervisor.py     ← 公共进程组、健康探测和状态管理
    factory.py        ← 按 engine_type 选择 Backend
    config_factory.py ← 延迟加载引擎配置转换器
    backends/
      base.py         ← NativeEngineBackend 和 IConfig 接口
      vllm/
        backend.py    ← vLLM 启动策略
        config.py     ← vLLM 参数转换与校验
      sglang/
        backend.py    ← SGLang 启动策略
        config.py     ← SGLang 参数转换与校验
  memcache/
    __init__.py
    worker.py         ← memcache worker 子进程入口（DistributedObjectStore）
    lifecycle.py      ← daemon 侧生命周期管理（@register_service）
```

### 服务注册

每个服务通过 `@register_service` 装饰器注册，指定名字、后端标签和可选的准备优先级：

```python
from motor.node_manager.core.services.registry import register_service

@register_service("engine", backend="engine")
class NativeEngineService:
    ...

@register_service("kv_store", backend="memcache", prepare_priority=10)
class LocalService:
    ...
```

`DaemonService` 和 `PreparableService` 是 Protocol 接口（`motor/node_manager/core/services/protocols.py`），定义了 `stop()`、`health_check()` 和 `prepare()` 合约。

### 后端激活

`Daemon.__init__` 根据 `kv_cache_store_config` 决定激活哪些服务：

- `mode="combined"`（默认）：`services = "engine,<backend>"`，Engine 和 KV-store 在同一 Pod。
- `mode="separated"`：`services = "<backend>"`，仅 KV-store，不拉 Engine、不向 Controller 注册、不上报心跳。
- 无 `kv_cache_store_config`：`services = "engine"`，仅推理。

`registry.discover(services)` 解析逗号分隔的服务列表，导入对应模块的 `@register_service` 触发注册。

### 新增原生引擎 Backend

vLLM 和 SGLang 是 `NativeEngineService` 的引擎 Backend，不是独立的 Daemon service。新增原生引擎时：

1. 在 `native_engine/backends/<engine>/` 中实现 `backend.py` 和 `config.py`。
2. Backend 只负责引擎差异化的配置转换、启动命令、角色校验和探针规格。
3. 在 `native_engine/factory.py` 注册 Backend，在 `native_engine/config_factory.py` 注册配置转换器。
4. 进程启动、进程组清理、健康探测和状态机继续复用公共 `ProcessSupervisor`。

不要在引擎 Backend 中直接调用 `subprocess.Popen`，也不要为每个引擎复制停止和探测状态机。

### 新增后端

新增 KV 后端只需两步，无需修改现有代码：

```python
# 1. 创建服务模块并注册
from motor.node_manager.core.services.registry import register_service, SERVICE_KV_STORE

@register_service(SERVICE_KV_STORE, backend="new_backend", prepare_priority=10)
class NewBackendService:
    ...

# 2. 注册模块发现路径
registry.add_discovery_path("new_backend", "path.to.new_backend_module")
```

## 软件故障上报

`FaultReporter` 在以下任一条件满足时自动启用：`fault_tolerance_config.enable_fault_tolerance` 显式开启，或 user config 的引擎配置（如 `motor_engine_prefill_config.engine_config`）中检测到 `enable-fault-tolerance` / `enable_fault_tolerance` 为 `true`（无需 NodeManager 显式配置）。启用后在后台线程中按 `poll_interval_sec` 间隔轮询每个 endpoint 的 FT 状态接口：

```text
GET http://{endpoint.ip}:{endpoint.business_port}/fault_tolerance/status
```

响应格式（vLLM FaultTolerance 框架提供，见 vllm-project/vllm#44428）：

```json
{
  "schema_version": 1,
  "total_engines": 1,
  "engines": [{"id": 0, "status": "healthy|dead|unhealthy", "fault_info": "..."}]
}
```

状态映射为：

- `healthy`：记录状态，不上报故障。
- `dead`：上报 `EngineDeadError`。
- `unhealthy`：上报 `EngineUnhealthyError`；响应携带 `fault_info` 时以其作为 `exception_type`（如 `RuntimeError`）。

同一 Engine 的相同非健康状态只在成功发送给 Controller 后标记为已上报；发送失败时后续轮询仍可重试。引擎连续 `max_poll_failures` 次轮询失败（连接拒绝/超时）时按 `dead` 上报 `EngineDeadError`（异常消息注明不可达轮询次数），引擎恢复可轮询后重新计数。

## 容器快照

当前 Node Manager 直接拉起原生 vLLM/SGLang；显存快照的保存与恢复由引擎自闭环完成，
仅支持提供快照能力的 vLLM 镜像。配置 `snapshot_config.enable_snapshot=true` 且引擎类型为
SGLang 时，配置校验会明确失败。

启用容器快照后，Node Manager 只做框架侧编排：准备 `model_save_path` / `model_load_path` /
`data_parallel_master_ip` 等元数据，快照恢复后刷新 `job_name`、Pod IP 与 Controller DNS，
并通过引擎就绪状态和 Host 侧 `checkpoint` 标记感知保存/恢复是否完成。Node Manager 不调用
引擎的 `/suspend`、`/device_unlock`、`/resume`。

## 使用样例

本地启动入口：

```bash
export USER_CONFIG_PATH=/path/to/user_config.json
export ROLE=prefill
python -m motor.node_manager.main
```

实际部署通常通过镜像入口脚本启动，并由 Controller 自动完成注册和实例下发。可使用以下请求检查状态：

```bash
curl http://127.0.0.1:1026/node-manager/status
curl -i http://127.0.0.1:1026/readiness
```

启用管理面 TLS 时，将协议改为 `https` 并按证书配置访问。

## 报错与排查

- 日志持续出现 `Registration attempt N failed`：检查 Controller DNS、端口、TLS 配置和网络连通性；Node Manager 会持续重试注册，不会因此退出。
- 日志出现 `Start command validation failed`：检查 Controller 下发的 `job_name`、endpoint 数量和 endpoint IP 是否与 Node Manager 配置一致。
- 日志出现 `Invalid endpoint parameters`：检查 endpoint IP 与业务端口，业务端口必须处于 `[1024, 65535]`。
- 日志出现 `Engine process exited immediately`：原生引擎在 `Popen` 后立即退出，需继续检查原生引擎日志、配置路径和启动参数。
- `/readiness` 返回 `503`：无 endpoint、存在非 `NORMAL` endpoint、处于 `PAUSED`，或快照恢复后尚未收到启动命令。
- 连续出现 `Consecutive abnormal heartbeat count: 5/5`：Node Manager 将清理原生引擎进程组并以 `-1` 退出触发 Pod 级重调度。

相关单元测试位于 `tests/node_manager/`；优雅暂停流程测试位于 `tests/e2e/test_prestop_e2e.py`。
