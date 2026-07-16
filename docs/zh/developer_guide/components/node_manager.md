# Motor Node Manager（节点管理器）

## 功能介绍

Node Manager 是部署在推理节点上的管理进程，负责连接 Controller 与本节点的 Engine Server。进程入口为 `motor/node_manager/main.py`，主要职责如下：

1. 加载节点配置，完成端口分配并启动管理面 HTTP 服务。
2. 向 Controller 注册节点，接收 Controller 下发的实例启动命令。
3. 按 endpoint 拉起、记录和停止 `engine_server` 子进程。
4. 轮询 Engine Server 状态并向 Controller 上报心跳。
5. 处理优雅暂停、配置热更新、容器快照恢复和软件故障上报。

### 组件结构

| 对象 | 源码 | 职责 |
|------|------|------|
| `NodeManagerConfig` | `motor/config/node_manager.py` | 加载、校验和重载节点配置，推导 endpoint 数量与端口 |
| `NodeManagerAPI` | `motor/node_manager/api_server/node_manager_api.py` | 在后台线程中运行 FastAPI/uvicorn，提供启动、停止和探针接口 |
| `Daemon` | `motor/node_manager/core/daemon.py` | 组装 `engine_server` 命令，拉起子进程并维护 PID |
| `EngineManager` | `motor/node_manager/core/engine_manager.py` | 注册/重注册、校验启动命令、处理 ranktable、快照元数据和故障上报 |
| `HeartbeatManager` | `motor/node_manager/core/heartbeat_manager.py` | 轮询 endpoint 状态、上报心跳、维护暂停/恢复状态并触发异常自杀 |
| `FaultReporter` | `motor/node_manager/core/fault_reporter.py` | 订阅 Engine Server 的 ZMQ 软件故障消息并转发给 Controller |
| `ControllerApiClient` | `motor/node_manager/api_client/controller_api_client.py` | 调用 Controller 的注册、重注册、心跳和故障上报接口 |
| `EngineServerApiClient` | `motor/node_manager/api_client/engine_server_api_client.py` | 调用 Engine Server 管理面的 `GET /status` |

`Daemon`、`EngineManager` 和 `HeartbeatManager` 均为线程安全单例。HTTP 路由和后台线程通过这些单例共享实例、endpoint 和进程状态。

## 生命周期

### 启动

`main()` 的启动顺序如下：

1. 注册 `SIGINT` 和 `SIGTERM` 信号处理函数，并将进程名设置为 `NodeManager`。
2. 从 `Env.user_config_path or Env.config_path` 加载 `NodeManagerConfig`。
3. 配置日志并执行 Node Manager 端口分配。
4. 依次创建 `NodeManagerAPI`、`Daemon`、`EngineManager` 和 `HeartbeatManager`。
5. `NodeManagerAPI` 在 `nm_api_server` 后台线程中启动；FastAPI lifespan 就绪后设置 API ready 事件。
6. `EngineManager` 的 `engine_register` 线程最多等待 API ready 30 秒，然后向 Controller 注册。
7. 非快照模式下启动配置文件 watcher；快照模式不使用 inotify，因此禁用 watcher。
8. 主线程持续检查退出信号和 `HeartbeatManager.should_suicide()`。

首次注册最多尝试 5 次，重试间隔为 2、4、8、16 秒。连续失败后，`EngineManager` 向当前进程发送 `SIGTERM`。

### 启动实例

Controller 调用 `POST /node-manager/start` 后，处理流程为：

1. 将请求体解析为 `StartCmdMsg`。
2. 校验 `job_name`、endpoint 数量以及每个 endpoint 的 IP 是否与本节点配置一致。
3. 保存 `instance_id`、endpoints、`node_rank` 和 D2D peer 信息；如配置了 `RANKTABLE_PATH`，将实例 ranktable 写入该文件。
4. 准备快照运行目录和元数据。
5. `Daemon.pull_engine()` 为每个 endpoint 拉起一个 `engine_server` 子进程。
6. 更新 `HeartbeatManager` 中的 endpoint，并启动状态轮询和心跳线程。
7. 启动 `EngineManager` 中的 `FaultReporter`（仅在故障容忍功能开启时生效）。

从宿主机侧快照恢复时，第 5 步不会再次拉起 Engine Server，而是更新恢复元数据、endpoint 和恢复状态。

### 停止与重调度

- 收到 `SIGINT`、`SIGTERM` 或标准输入命令 `stop` 时，Node Manager 停止配置 watcher，并按初始化的逆序停止模块。
- `Daemon.stop()` 对记录的 Engine Server PID 发送 `SIGKILL`，随后清空 PID 列表。
- 任一 endpoint 连续 5 个心跳周期保持 `ABNORMAL` 时，`HeartbeatManager` 设置自杀标志。主线程执行清理后返回 `-1`，用于触发重调度。
- 当前 `main()` 正常退出路径同样返回 `-1`；源码注释约定 `-1` 表示 rescheduling、`0` 表示 restart。

## Node Manager HTTP API

Node Manager API 默认监听 `api_config.pod_ip:api_config.node_manager_port`。启用 `mgmt_tls_config.enable_tls` 后使用 HTTPS。

| 方法 | 路径 | 响应 | 说明 |
|------|------|----------|------|
| `POST` | `/node-manager/start` | `200 {}` | 校验启动命令并拉起 Engine Server；快照恢复时执行恢复准备 |
| `POST` | `/node-manager/stop` | `200 {"message": "All engine processes stopped successfully."}` | 停止当前 Node Manager 记录的全部 Engine Server 进程 |
| `POST` | `/node-manager/pause` | `200 {"status":"ok", ...}` | 将全部 endpoint 标记为 `PAUSED`，并返回 Engine Server 管理地址 |
| `POST` | `/node-manager/resume` | `200 {"status":"ok", ...}` | 仅将 `PAUSED` endpoint 恢复为 `NORMAL` |
| `GET` | `/node-manager/status` | `200 {"status": true/false}` | 返回全部 endpoint 是否为 `NORMAL`；无 endpoint 时为 `false` |
| `GET` | `/readiness` | `200` 或 `503` | Kubernetes Readiness Probe 接口。实例节点 Pod 默认不配置该探针；仅在容器快照默认应用场景下配置，用于判断执行容器 checkpoint 前的稳态点。未到达稳态点时返回 `503`，到达后返回 `200` |

`/node-manager/pause` 用于 PreStop 优雅下线：暂停状态会使 readiness 失败，并通过心跳通知 Controller；状态轮询不会用 Engine Server 返回值覆盖手动设置的 `PAUSED`。如果 PreStop 被取消，可调用 `/node-manager/resume` 恢复调度。

`/readiness` 仅用于快照默认应用场景，即 MindCluster 实例重调度，不作为 Node Manager 的通用健康检查接口。MindCluster 通过该接口查询实例节点是否到达稳态点。在容器快照的用户自定义应用场景中，可调用 `/node-manager/status` 查询稳态点；接口返回 `200 {"status": true}` 表示已到达稳态点。

### `StartCmdMsg` 请求字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `job_name` | string | 是 | 实例任务名，必须与本节点配置一致 |
| `role` | string | 是 | 实例角色，如 `prefill`、`decode` 或 `union` |
| `instance_id` | int | 是 | Controller 分配的实例 ID |
| `endpoints` | array | 是 | 本节点管理的 endpoint；元素包含 `id`、`ip`、`business_port`、`mgmt_port` 等 |
| `master_dp_ip` | string | 是 | 数据并行主节点 IP |
| `ranktable` | object/null | 否 | 实例级 ranktable，默认 `null` |
| `d2d_peer_ips` | array/null | 否 | D2D 权重传输对端，Controller 使用 `<endpoint_id>:<peer_ip>` 编码，默认 `null` |
| `node_rank` | int | 否 | Controller 按注册顺序分配的节点序号，默认 `0` |

当前接口错误码如下：

- 启动命令内部解析异常：`400 Invalid start command payload`。
- `job_name`、endpoint 数量或 endpoint IP 校验失败：`422 Start command validation failed`。
- Engine Server 拉起失败：`500 Failed to start engine server`。
- 请求 JSON/Pydantic 字段解析异常会被外层异常处理转换为通用 `500`。
- `/readiness` 在 endpoint 尚未健康或快照恢复后尚未启动时返回 `503`。

## 与外部组件的通信

### Controller

| 方向 | Controller 接口 | 行为 |
|------|-----------------|------|
| Node Manager → Controller | `POST /controller/register` | 上报角色、模型、端口、并行配置、ranktable、`nnodes` 和快照主节点标记 |
| Node Manager → Controller | `POST /controller/reregister` | Controller 重启并对心跳返回 `503` 时，携带实例与 endpoint 信息重新注册 |
| Node Manager → Controller | `POST /controller/heartbeat` | 按 `heartbeat_interval_seconds` 上报各 endpoint 状态 |
| Node Manager → Controller | `POST /controller/report_software_fault` | 转发 Engine Server 软件故障 |

心跳使用长连接客户端，单次超时为 5 秒；TCP 请求失败时重建连接，并按 1 秒、2 秒退避重试两次。

### Engine Server

`HeartbeatManager` 启动时先等待各 endpoint 管理端口可连接，最长等待 60 秒；之后每秒调用一次 Engine Server 的 `GET /status`，单次请求超时为 5 秒。

状态轮询具有以下保护逻辑：

- Engine Server 启动后的 120 秒宽限期内，探测到 `ABNORMAL` 时保留原状态。
- 更新 endpoint 时使用 generation 标记，避免旧探测结果覆盖 Controller 新下发的数据。
- 手动设置的 `PAUSED` 状态不会被轮询结果覆盖。
- Engine Server 返回未知状态或无效响应时，按 `ABNORMAL` 处理。

## Engine Server 拉起参数

`Daemon.pull_engine()` 为每个 endpoint 执行：

```text
engine_server \
  --dp-rank <endpoint.id> \
  --instance-id <instance_id> \
  --role <prefill|decode|union> \
  --host <endpoint.ip> \
  --port <endpoint.business_port> \
  --mgmt-port <endpoint.mgmt_port> \
  --master-dp-ip <master_dp_ip> \
  --node-rank <node_rank> \
  --config-path <USER_CONFIG_PATH>
```

其他参数和环境变量：

- 多 endpoint 模式下，按 `local_world_size` 为每个进程计算 `ASCEND_RT_VISIBLE_DEVICES`；设备编号超出末尾时循环分配。
- 单容器模式追加 `--kv-port`、`--dp-rpc-port`，配置存在时再追加 `--lookup-rpc-port`。
- 开启快照时追加 `--snapshot-metadata`。
- D2D peer 按 endpoint ID 过滤后，以逗号分隔并通过 `--d2d-peer-ips` 传递。
- 环境中存在 `POD_IP` 且未设置 `VLLM_HOST_IP` 时，自动设置 `VLLM_HOST_IP=POD_IP`。
- `MOONCAKE_ASCEND_IPV6_EXPERIMENT=1` 时，默认设置 `MC_USE_IPV6=1`。

endpoint 的业务端口必须处于 `[1024, 65535]`，IP 必须是合法的 IPv4 或 IPv6 地址，否则拉起失败。

### 跨节点 PCP

Node Manager 始终将 Controller 分配的 `node_rank` 作为 `--node-rank` 传给 Engine Server，并将 `master_dp_ip` 作为 `--master-dp-ip` 传递。

对于 vLLM，Engine Server 在引擎配置包含 `nnodes > 1` 且配置了 `master_port`（兼容 `master-port`）时启用跨节点 PCP：

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
| `endpoint_config.base_port` | `10000` | Engine Server 端口基址；业务/管理端口按偶数/奇数生成 |
| `basic_config.heartbeat_interval_seconds` | `3` | 向 Controller 上报心跳的周期 |
| `basic_config.enable_multi_endpoints` | `true` | 是否按 DP 和设备数创建多个 endpoint |
| `basic_config.nnodes` | `1` | 从 `engine_config.nnodes` 派生的跨节点数量 |
| `mgmt_tls_config.enable_tls` | `false` | Node Manager、Controller 和 Engine Server 管理面通信是否启用 TLS |
| `fault_tolerance_config.enable_fault_tolerance` | `false` | 是否启动软件故障订阅线程 |
| `fault_tolerance_config.zmq_pub_port` | `0` | ZMQ PUB 基础端口；每个 endpoint 使用 `base_port + endpoint.id` |
| `snapshot_config.enable_snapshot` | `false` | 是否启用容器快照流程 |
| `snapshot_config.snapshot_metadata_path` | 空 | 自定义快照元数据路径；用户需预先创建并挂载该文件。为空时进入快照默认应用场景，即 MindCluster 实例重调度 |
| `port_allocator_config.enable` | `true` | 是否在启动时自动检查并调整端口 |

`endpoint_num`、`service_ports`、`mgmt_ports`、`device_num`、`parallel_config`、`model_name`、`engine_type` 和 `dispatch_capabilities` 主要由部署配置与引擎配置派生。`dispatch_capabilities` 不接受用户直接覆盖。

当 `pod_ip` 为空时，API 服务根据 `POD_IP` 判断监听协议族：IPv6 使用 `::`，其他情况使用 `0.0.0.0`。只有直接构造且不传入配置的 `NodeManagerAPI` 才使用内部兜底端口 `8080`，正常启动流程使用配置端口。

### 配置热更新

非快照模式下，配置 watcher 检测到文件变化后调用各模块的 `update_config()`：

- `HeartbeatManager` 动态更新 `heartbeat_interval_seconds`。
- `EngineManager` 更新配置，并根据 `enable_fault_tolerance`、endpoint、Pod IP 或 `zmq_pub_port` 的变化启停或重建 `FaultReporter`。
- API 监听地址、监听端口、TLS 和 `Daemon` 已缓存的设备参数不会热重启，修改后需要重启 Node Manager。

## 软件故障上报

开启 `fault_tolerance_config.enable_fault_tolerance` 后，`FaultReporter` 为每个 endpoint 连接一个 ZMQ SUB socket，订阅主题 `vllm_fault`。端口为：

```text
fault_tolerance_config.zmq_pub_port + endpoint.id
```

消息中的状态映射为：

- `healthy`：记录状态，不上报故障。
- `dead`：上报 `EngineDeadError`。
- `unhealthy`：上报 `EngineUnhealthyError`。

同一 Engine 的相同非健康状态只在成功发送给 Controller 后标记为已上报；发送失败时后续消息仍可重试。ZMQ 发生错误后等待 5 秒并重建订阅。

## 容器快照

开启 `snapshot_config.enable_snapshot` 后：

- Node Manager 不支持配置热更新, 不启动配置文件 watcher。
- 配置为空时进入快照默认应用场景，即 MindCluster 实例重调度：容器快照镜像由 MindCluster 制作；MindCluster 通过 ConfigMap 挂载快照元数据，NodeManager 将挂载文件复制到默认可写路径 `/snapshot/snapshot_metadata.json` 后交给 Engine Server 使用。
- 自定义路径场景下，用户需预先创建并挂载快照元数据文件；框架读取或更新该文件，并将其路径传给 Engine Server，不负责该文件的创建和挂载。
- 快照制作阶段，Engine Server 完成 suspend 后，其管理面状态由 `INIT` 变为 `NORMAL`。当本节点全部 Engine Server 均完成 suspend 时，表示实例节点容器已到达稳态点：快照默认应用场景通过 `/readiness` 返回 `200` 判断；用户自定义应用场景通过 `/node-manager/status` 返回 `200 {"status": true}` 判断。
- 查询到稳态点后，对实例节点容器执行 checkpoint，并保存容器 Host 快照镜像。
- 处于容器快照镜像 checkpoint 过程中的实例无法提供服务, 当 NodeManager 状态已正常但 checkpoint 尚未完成时，此时暂停向 Controller 上报心跳。
- 快照恢复后，先从元数据恢复 `job_name` 和 `namespace`，刷新 Pod IP 与 Controller DNS，再重新注册。
- Controller 再次调用 `/node-manager/start` 时，Node Manager 只准备快照恢复阶段需要的 `model_load_path` 和 `data_parallel_master_ip` 元数据，但不重新创建 Engine Server 进程。
- 快照恢复后未收到启动命令前，readiness 始终为未就绪。

### 快照元数据字段

快照元数据文件必须是 JSON 对象，以下字段的值均为字符串。自定义应用场景下，用户需按字段所处阶段提前准备元数据。

| 字段 | 使用阶段 | 准备要求 | 说明 |
|------|----------|----------|------|
| `model_save_path` | 快照制作 | 制作容器快照前必须准备 | Device 快照保存时，容器内运行时权重的落盘路径，必须是宿主机挂载路径 |
| `model_load_path` | 快照恢复 | 从容器快照恢复前必须准备 | Device 快照恢复时，容器内运行时权重的加载路径，必须是宿主机挂载路径 |
| `job_name` | 快照恢复 | 从容器快照恢复前必须准备 | 恢复后注册时用于更新 Node Manager 的任务名 |
| `namespace` | 快照恢复 | Controller 使用集群内 `.svc.cluster.local` DNS 时必须准备 | 恢复后注册时用于将 Controller DNS 更新到快照所属 namespace；非集群 DNS 场景可不配置 |
| `data_parallel_master_ip` | 快照恢复 | 可不预先配置, 由controller下发 | 优先使用文件中的值；未配置时，Node Manager 写入 Controller 下发的 `master_dp_ip` |
| `checkpoint` | 快照制作 | Host 侧 checkpoint 完成后写入 | 用户或 MindCluster 将其更新为 `"done"`，框架据此解锁 Device 并恢复冷启动实例业务 |

因此，自定义应用场景从容器快照恢复前，至少需要准备 `model_load_path` 和 `job_name`；使用集群内 Controller DNS 时还需准备 `namespace`。元数据中的其他未知字段不会被 Node Manager 使用。

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

- 日志出现 `Registration failed after maximum retries`：检查 Controller DNS、端口、TLS 配置和网络连通性；Node Manager 随后会收到 `SIGTERM`。
- 日志出现 `Start command validation failed`：检查 Controller 下发的 `job_name`、endpoint 数量和 endpoint IP 是否与 Node Manager 配置一致。
- 日志出现 `Invalid endpoint parameters`：检查 endpoint IP 与业务端口，业务端口必须处于 `[1024, 65535]`。
- 日志出现 `Engine process exited immediately`：Engine Server 在 `Popen` 后立即退出，需继续检查 Engine Server 日志、配置路径和启动参数。
- `/readiness` 返回 `503`：无 endpoint、存在非 `NORMAL` endpoint、处于 `PAUSED`，或快照恢复后尚未收到启动命令。
- 连续出现 `Consecutive abnormal heartbeat count: 5/5`：Node Manager 将清理 Engine Server 并以 `-1` 退出触发重调度。

相关单元测试位于 `tests/node_manager/`；优雅暂停流程测试位于 `tests/e2e/test_prestop_e2e.py`。
