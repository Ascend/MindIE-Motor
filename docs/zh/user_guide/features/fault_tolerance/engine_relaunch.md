# 故障引擎重启

## 特性介绍

故障引擎重启（`engine_relaunch`）用于在推理引擎进程退出或健康检查异常时，自动在原容器内重新拉起引擎，恢复实例的推理能力。该特性默认开启，复用已有的实例资源与启动配置，无需重新分配节点或创建 Pod。

与[实例重调度](./instance_rescheduling.md)相比，故障引擎重启优先恢复原实例；而实例重调度通过回收故障 Pod、重新分配资源恢复服务。对于节点失联、硬件不可用等无法在原容器内恢复的故障，需要结合实例重调度处理。

可通过恢复前后的标识变化区分两种特性：

| 标识变化 | 对应特性 |
| --- | --- |
| `job_name` 不变，`instance_id` 不变 | 故障引擎重启（本特性） |
| `instance_id` 变化 | [实例重调度](./instance_rescheduling.md) |

### 工作原理

1. NodeManager 持续监控引擎进程与健康状态，异常时向 Controller 上报。Controller 确认实例异常后将其标记为 INACTIVE，并通知 Coordinator 摘除该实例，新请求不再调度到它。重拉与模型加载期间实例保持 INACTIVE，引擎恢复正常后自动回到 ACTIVE 并重新进入调度池。
2. Controller 根据故障类型选择恢复策略。进入故障引擎重启流程后，先确认实例内各 NodeManager 可达，再向所有 NodeManager 下发引擎重启命令，按实例协调恢复。
3. NodeManager 使用已保存的启动参数，在原容器内停止并重新拉起引擎。等待引擎重启与模型加载期间，暂缓因引擎异常触发的容器退出。
4. 引擎重新就绪后，NodeManager 恢复正常心跳；实例满足健康条件后回到 `ACTIVE` 状态，由 Controller 重新发布给 Coordinator，恢复请求调度。
5. 若引擎重启命令下发失败、等待超时或 NodeManager 失联，则进入后续恢复策略。容器级恢复依赖外部重启管理，需要重新分配资源时，由 Kubernetes 与 MindCluster 承担实例重调度。

> [!NOTE] 说明
> 引擎重启策略执行结束不代表模型已完成加载。应以实例恢复为 `ACTIVE`、重新进入调度池且推理请求成功为恢复完成的判断依据。

### 约束与限制

- 原容器及其 NodeManager 需要可达，模型权重、启动配置和计算资源仍需可用。持续存在的配置错误、资源不足或硬件故障不能仅靠引擎重启解决。
- 多节点实例按实例协调引擎重启，恢复时间取决于故障检测、命令下发和模型加载等过程，不保证固定恢复时长。
- 引擎重启会中断故障引擎正在处理的请求。若需将受影响请求转移到其他健康实例，可结合[请求重调度](./request_rescheduler.md)使用，并预留同角色的健康实例。
- Docker 与 Kubernetes 部署均支持故障引擎重启。Kubernetes 环境默认启用外部容器重启管理；Docker 独立部署默认不启用，引擎重启失败后不能依赖 Pod 重建兜底，需要配置外部重启管理或人工恢复。

## 特性使用

### 配置说明

使用 Motor 提供的部署方式完成服务部署后，该特性按默认配置生效，无需额外开启。自定义配置时，请检查以下参数是否被覆盖：

| 所属组件 | 参数 | 默认值 | 说明 |
| --- | --- | --- | --- |
| Controller | `fault_tolerance_config.enable_fault_tolerance` | `true` | 启用故障处理 |
| Controller | `fault_tolerance_config.enable_engine_relaunch` | `true` | 允许故障策略选择故障引擎重启 |
| Controller | `fault_tolerance_config.engine_relaunch_complete_timeout_sec` | `600` | 故障引擎重启策略的等待预算，单位为秒，不代表服务恢复时长承诺 |
| NodeManager | `fault_tolerance_config.enable_engine_relaunch` | `true` | 配合故障引擎重启，在等待恢复期间暂缓容器退出 |
| NodeManager | `fault_tolerance_config.container_restart_managed` | 自动判断 | 未指定时，在 Kubernetes Pod 中启用外部容器重启管理，Docker 独立部署不启用 |

Controller 参数位于 `user_config.json` 的 `motor_controller_config` 中。NodeManager 参数位于对应引擎配置的 `motor_nodemanger_config` 中；未配置时使用默认值。其他参数请参见[配置参数说明](../../configuration/config_reference.md)。

### 验证特性

建议在具备冗余实例的测试环境中验证，避免影响业务请求。

1. 确认实例为 `ACTIVE`，推理请求正常，并记录 `job_name`、`instance_id` 及实例所在容器或 Pod。
2. 在测试环境中模拟推理引擎进程退出，保留 NodeManager 运行，观察 Controller 与 NodeManager 日志中的故障上报和故障引擎重启过程。
3. 确认引擎进程在原容器内重新启动，且 `job_name`、`instance_id` 均保持不变；Kubernetes 部署可同时检查 Pod UID 与容器重启次数，区分故障引擎重启和容器重建。
4. 等待模型加载完成，确认实例恢复为 `ACTIVE`、重新进入调度池，并通过推理请求验证服务恢复。

若未恢复，请先检查 NodeManager 连通性、引擎启动日志、模型与资源是否可用，再确认外部重启管理和[实例重调度](./instance_rescheduling.md)的部署条件。
