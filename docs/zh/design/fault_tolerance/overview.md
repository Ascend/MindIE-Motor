# 可靠性能力（特性说明）

MindIE-PyMotor 提供多层级的可靠性保障机制，覆盖硬件故障感知、实例故障隔离、自动重拉起注册、缩P保D及token级重推（网络故障恢复）等场景。核心组件包括 `FaultManager`（故障管理）、`InstanceAssembler`（实例组装）和 `InstanceManager`（实例生命周期管理）。

## 能力总览

| 能力 | 说明 | 核心模块 |
|------|------|----------|
| 实例故障隔离 | 硬件故障（NPU/网络）+ 软件故障（节点重启）感知与隔离 | `FaultManager` + `ResourceMonitor` |
| 自动重拉起注册 | Pod 重启后 NodeManager 自动重新注册，Controller 组装实例并拉起引擎 | `InstanceAssembler` + `InstanceManager` |
| 缩P保D（Scale P2D） | Decode 实例故障时，释放 Prefill 节点以恢复 Decode | `ScaleP2DStrategy` |
| token级重推 | L2 级别网络故障检测与token级重推恢复 | `TokenReinferenceStrategy` |

---

## 1. 实例故障隔离

### 1.1 故障感知

`FaultManager` 作为单例观察者，通过 `ResourceMonitor` 对每个 K8s Node 启动双重 Watch：

- **ConfigMap 监控**：监听 `kube-system` 命名空间下 `mindx-dl-deviceinfo-{node_name}` ConfigMap。当驱动/固件上报硬件故障时，ConfigMap 数据更新，`ResourceMonitor` 捕获变更后解析其中的 `DeviceInfoCfg`（NPU 卡故障 `CardUnhealthy`、卡间网络故障 `CardNetworkUnhealthy`）和 `SwitchInfoCfg`（交换机故障），生成对应的 `FaultInfo` 并通过回调通知 `FaultManager`。
- **Node 状态监控**：通过 K8s Watch API 监听 Node 的 Ready/NotReady 状态变化。当 Node 变为 NotReady 时，`FaultManager` 注入 `NODE_REBOOT` 故障（fault_code: `0x0000001`，等级 L6）；Node 恢复 Ready 后自动清除该故障。

### 1.2 故障等级

故障按严重程度分为 7 个等级，由原始故障类型映射而来（[fault_types.py](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/motor/controller/fault_tolerance/fault_types.py)）：

| 等级 | 枚举 | 原始故障类型 | 含义 |
|------|------|-------------|------|
| 0 | `HEALTHY` | — | 无故障 |
| 1 | `L1` | `NotHandleFault` | 无需处理 |
| 2 | `L2` | `RestartRequest` | 可自愈（触发token级重推） |
| 3 | `L3` | `RestartBusiness` | 无法自动处理 |
| 4 | `L4` | `FreeRestartNPU` | 需隔离并触发恢复策略（缩P保D） |
| 5 | `L5` | `RestartNPU` | 需 NPU 重启 |
| 6 | `L6` | `SeparateNPU` / `PreSeparateNPU` | 需 NPU 分离 / 节点重启 |

### 1.3 隔离与恢复机制

`FaultManager` 收集实例下所有 Node 的故障信息，取最高故障等级作为实例的当前故障等级：

- **故障等级 > L2**：调用 `InstanceManager.separate_instance()`，将实例标记为 `INACTIVE` 并加入 `forced_separated_instances` 集合，实现强制隔离。
- **故障等级 ≤ L2 且已被隔离**：调用 `InstanceManager.recover_instance()`，从强制隔离集合中移除实例，为后续自动重拉起注册打开通路。
- **故障消除（HEALTHY）**：重置实例故障状态并解除隔离。

```text
ResourceMonitor 感知故障
        │
        ▼
FaultManager._refresh_instance_fault_level()
        │
        ├── fault > L2  →  InstanceManager.separate_instance()
        │                   （强制隔离，阻止心跳恢复）
        │
        ├── fault ≤ L2  →  InstanceManager.recover_instance()
        │   (已隔离)         （解除强制隔离）
        │
        └── HEALTHY     →  重置为健康状态 + recover
```

---

## 2. 自动重拉起注册

当 Pod 因故障被 K8s 重启后，NodeManager 需要重新向 Controller 注册并重新组装实例、拉起推理引擎。该过程由 `InstanceAssembler` 和 `InstanceManager` 协同 K8s 完成，无需人工干预。

### 2.1 整体流程

```text
Pod 因故障被 K8s 重启
        │
        ▼
NodeManager 启动，EngineManager._register() 发送 RegisterMsg 到 Controller
        │
        ▼
Controller InstanceAssembler.register()
  - 检查实例是否已存在
  - NOT_REGISTERED: 创建新 Instance，分配 ID
  - 添加 NodeManager 信息和端点（Endpoints）
        │
        ▼
InstanceAssembler._instances_assembler_loop 轮询
  - _filter_abnormal_endpoints(): 剔除异常 NodeManager
  - is_endpoints_enough(): 等待所有 Pod 注册完毕
        │
        ▼
实例组装完成 → register_status = ASSEMBLED
  - InstanceManager.add_instance() 接管实例
  - 通知观察者 INSTANCE_INITIAL
        │
        ▼
InstanceAssembler._start_commmand_sender 发送 StartCmdMsg
  - 携带 job_name, role, instance_id, endpoints, master_dp_ip, ranktable
        │
        ▼
NodeManager 接收 StartCmdMsg
  - parse_start_cmd(): 校验参数，存储 instance_id 和 endpoints
  - Daemon.pull_engine(): 启动 engine_server 推理进程
  - HeartbeatManager.start(): 开始心跳上报
        │
        ▼
InstanceManager 收到心跳 → 状态机: INITIAL → ACTIVE
实例恢复正常服务
```

### 2.2 关键组件交互

**NodeManager 侧（[engine_manager.py](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/motor/node_manager/core/engine_manager.py)）**：

- `_register()`：NodeManager 启动后自动向 Controller 发送 `RegisterMsg`（含 job_name、role、pod_ip、parallel_config、device_num、ranktable 等），最多重试 5 次。
- `parse_start_cmd()`：接收 Controller 的 `StartCmdMsg`，校验参数后存储 `instance_id` 和 `endpoints`，并将 ranktable 写入本地文件供引擎使用。

**Controller 侧（[instance_assembler.py](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/motor/controller/core/instance_assembler.py)）**：

- `register()`：根据 `_eval_register_status()` 判断当前状态——若 `InstanceManager` 中已存在 ACTIVE 实例则跳过；若已在组装中则更新时间戳；否则创建新 `Instance` 并加入组装队列。
- `_assemble_instance()`：检查各 NodeManager 是否存活、端点数量是否满足并行配置要求（`is_endpoints_enough()`），满足则标记为 `ASSEMBLED`。
- `_send_start_command()`：向实例中每个 NodeManager 下发 `StartCmdMsg`，通知其启动推理引擎。

**Controller 重启场景（re-register）**：

当 Controller 自身重启后，NodeManager 的心跳会收到 503 响应，触发 `HeartbeatManager._reregister()`。与首次注册不同，重注册发送的 `ReregisterMsg` 携带已有的 `instance_id` 和 `endpoints`，Controller 据此还原实例身份，跳过 `StartCmdMsg` 下发（引擎已在运行），直接交还给 `InstanceManager` 管理。

### 2.3 与故障隔离的关系

自动重拉起注册是故障恢复链路的关键闭环：`FaultManager` 负责故障检测与隔离决策，而 Pod 重启后实例的重新组装和引擎拉起则由 `InstanceAssembler` + `InstanceManager` 完成。两者通过 `forced_separated_instances` 集合衔接——只有 `FaultManager` 调用 `recover_instance()` 解除隔离后，实例状态机才允许从 `INACTIVE` 恢复为 `ACTIVE`。

---

## 3. 缩P保D（Scale P2D）

### 3.1 背景

在 PD 分离部署模式（X 个 Prefill 实例 + Y 个 Decode 实例）下，当某个 Decode 实例发生 L4 及以上硬件故障（如 NPU 卡故障需隔离）且集群中没有冗余节点可供恢复时，Decode 能力面临丧失。

缩P保D 通过**缩减一个 Prefill 实例来释放节点资源**，将释放的节点用于拉起新的 Decode 实例，在有限集群资源下最大化保障推理服务的可用性。

### 3.2 核心流程

```text
Decode 实例出现硬件故障（≥ L4）
        │
        ▼
FaultManager 检测到故障，评估实例故障等级为 L4/L5/L6
        │
        ▼
FaultManager 调用 InstanceManager.separate_instance()
将故障 D 实例隔离为 INACTIVE
        │
        ▼
策略中心根据角色（decode）+ 故障等级匹配到 ScaleP2DStrategy
        │
        ▼
scale_p2d()：选择一个 P 实例，释放其占用的节点资源
        │
        ├──▶ 使用释放的节点拉起新的 Decode 实例
        │     （走自动重拉起注册流程：NodeManager 注册 → 组装 → 下发 StartCmd）
        │
        ├──▶ 剩余 P 实例退化为非 PD 分离模式
        │      Coordinator 感知到仅剩 Prefill、无 Decode 可用
        │      自动将 deploy_mode 回退为 SINGLE_NODE
        │      由 Prefill 独立完成 Prefill + Decode 完整推理
        │
        └──▶ 新 Decode 实例拉起就绪后
               Coordinator 恢复 PD 分离模式
               请求重新按 P/D 角色分发
```

### 3.3 关键设计点

- **触发条件**：仅 Decode 角色实例，且故障等级为 L4 / L5 / L6（L5、L6 委托 L4 策略逻辑）。
- **模式退化**：Decode 故障期间，Coordinator 调度层检测 `readiness == ONLY_PREFILL`，自动将路由模式回退为 `SINGLE_NODE`（参见 [PD 分离—实例就绪与回退](../pd_disaggregation.md)）。此过程对业务无感知，推理请求正常处理，仅延迟增加。
- **模式恢复**：新 Decode 实例心跳上报就绪后，Coordinator 感知到 P/D 均可用，自动恢复 PD 分离路由。
- **策略方向**：缩P保D 是单向的——只有 Decode 故障时才需要"借用" Prefill 的节点资源。Preifill 角色的 L4+ 故障不会触发此策略。
- **策略代码**：[scale_p2d.py](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/motor/controller/fault_tolerance/strategy/scale_p2d.py)

### 3.4 策略路由

[strategy.py](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/motor/controller/fault_tolerance/strategy/strategy.py) 中的策略映射：

```python
# L4 → ScaleP2DStrategy（仅 decode 角色，非 decode 返回 None）
# L5 → 委托 L4
# L6 → 委托 L4
```

---

## 4. token级重推

### 4.1 背景

在 Ascend 集群的推理过程中，NPU 之间的高速互连网络（"灵衢"，Lingqu）可能出现 L2 级别的瞬时故障（如链路抖动、瞬断），导致部分 token 的推理结果异常或丢失。为避免整卡重启或节点隔离带来的巨大开销，引入 **token级重推** 机制：当检测到网络故障时，仅对受影响的 token 进行重新推理，在保障推理正确性的同时最小化故障恢复成本。

### 4.2 核心流程

```text
网络出现故障（链路抖动/瞬断）
        │
        ▼
驱动/固件上报故障 → ConfigMap 更新
        │
        ▼
Controller ResourceMonitor 感知 ConfigMap 变更
解析 DeviceInfoCfg / SwitchInfoCfg 中的网络故障信息
        │
        ▼
FaultManager._handle_fault_info_update()
刷新故障信息，评估实例故障等级为 L2
        │
        ▼
故障等级 ≤ L2 → 调用 InstanceManager.separate_instance()
将受影响实例隔离为 INACTIVE
        │
        ▼
策略中心根据 fault_code 匹配策略：
故障码 ∈ {0x00f1fef5, 0x08520003} → TokenReinferenceStrategy
        │
        ▼
策略与推理引擎协同，对受影响 token 进行重新推理
（网络故障自愈 + token重推）
        │
        ▼
Token 重推完成，故障消除
        │
        ▼
FaultManager 感知故障清除 → 实例恢复 HEALTHY
调用 recover_instance() 解除隔离
        │
        ▼
推理实例通过心跳自动重注册为 ACTIVE
恢复正常推理服务
```

### 4.3 关键设计点

- **白名单机制**：仅特定故障码 `0x00f1fef5` 和 `0x08520003` 触发该策略，非白名单内的 L2 故障不执行恢复策略。
- **不可中断**：策略 `stop()` 不执行任何操作——token级重推需要等待网络自行恢复或故障升级，不应被人为中断。
- **故障检测来源**：同时支持 `DeviceInfoCfg`（卡间网络故障 `CardNetworkUnhealthy`）和 `SwitchInfoCfg`（交换机故障）两条检测路径。
- **策略代码**：[token_reinference.py](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/motor/controller/fault_tolerance/strategy/token_reinference.py)

---

## 5. 组件交互全景

```text
                          ┌─────────────────────────┐
                          │     K8s (Pod 重启)       │
                          └──────────┬──────────────┘
                                     │
                                     ▼
┌────────────────────────────────────────────────────────────────────┐
│ Controller                                                         │
│                                                                    │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐  │
│  │ FaultManager │    │ InstanceAssembler│    │ InstanceManager  │  │
│  │              │    │                  │    │                  │  │
│  │ 故障检测      │    │ 实例组装          │    │ 生命周期管理       │  │
│  │ 隔离/恢复     │    │ 下发StartCmd      │    │ 心跳/状态机       │  │
│  │ 策略调度      │    │                  │    │ forced_separated │  │
│  └──────┬───────┘    └────────┬─────────┘    └────────┬─────────┘  │
│         │                     │                       │            │
│         │  separate/recover   │                       │            │
│         └─────────────────────┴───────────────────────┘            │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ 策略中心                                                      │  │
│  │  L2 + 白名单故障码  →  TokenReinferenceStrategy                 │  │
│  │  L4/L5/L6 + decode  →  ScaleP2DStrategy                      │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
         │                                       │
         │  RegisterMsg / ReregisterMsg          │ StartCmdMsg
         ▼                                       ▼
┌────────────────────────────────────────────────────────────────────┐
│ NodeManager                                                        │
│  ┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐  │
│  │ EngineManager│    │ HeartbeatManager │    │     Daemon       │  │
│  │ 注册/重注册    │    │ 心跳上报          │    │ 拉起引擎进程       │  │
│  │ 解析StartCmd  │    │ 检测Controller重启│    │                  │  │
│  └──────────────┘    └──────────────────┘    └──────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
```

---

## 6. 相关文档

- [FaultManager 设计文档](fault_manager.md)
- [PD 分离特性说明](../pd_disaggregation.md)
