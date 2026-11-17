# 可靠性能力（特性说明）

MindIE Motor 提供多层级的可靠性保障机制，覆盖硬件故障感知、实例故障隔离、自动重拉起注册、缩P保D及 token 级重推（网络故障恢复）等场景。核心组件包括 `FaultManager`（故障管理）、`InstanceAssembler`（实例组装）和 `InstanceManager`（实例生命周期管理）。

## 能力总览

| 能力 | 说明 | 核心模块 |
|------|------|----------|
| 实例故障隔离 | 硬件故障（NPU/网络）+ 软件故障（节点重启）感知与隔离 | `FaultManager` + `ResourceMonitor` |
| 自动重拉起注册 | Pod 重启后 NodeManager 自动重新注册，Controller 组装实例并拉起引擎 | `InstanceAssembler` + `InstanceManager` |
| 缩P保D（Scale P2D） | Decode 实例故障时，释放 Prefill 节点以恢复 Decode | `ScaleP2DStrategy` |
| 引擎快恢 | 按 HBM / 灵衢 / 无故障码快恢区分，共用快恢编排与调度暂停 | `EngineFastRecoveryStrategy` |
| Atlas 800I A2推理服务器 linkdown 整实例自杀 | Atlas 800I A2推理服务器 上 `0x81078603` 保持 L6，对 Prefill / Decode / 多 Pod union 下发 `/node-manager/stop` | `NmSuicideStrategy` |

## 实例故障隔离

### 故障感知

`FaultManager` 作为单例观察者，通过 `ResourceMonitor` 对每个 K8s Node 启动双重 Watch：

- **ConfigMap 监控**：监听 `kube-system` 命名空间下 `mindx-dl-deviceinfo-{node_name}` ConfigMap。当驱动/固件上报硬件故障时，ConfigMap 数据更新，`ResourceMonitor` 捕获变更后解析其中的 `DeviceInfoCfg`（NPU 卡故障 `CardUnhealthy`、卡间网络故障 `CardNetworkUnhealthy`）和 `SwitchInfoCfg`（交换机故障），生成对应的 `FaultInfo` 并通过回调通知 `FaultManager`。
- **Node 状态监控**：通过 K8s Watch API 监听 Node 的 Ready/NotReady 状态变化。当 Node 变为 NotReady 时，`FaultManager` 注入 `NODE_REBOOT` 故障（fault_code: `0x0000001`，等级 L6）；Node 恢复 Ready 后自动清除该故障。

### 故障等级

故障按严重程度分为 7 个等级，由原始故障类型映射而来（[fault_types.py](https://gitcode.com/Ascend/MindIE-Motor/blob/master/motor/controller/fault_tolerance/fault_types.py)）：

| 等级 | 枚举 | 原始故障类型 | 含义 |
|------|------|-------------|------|
| 0 | `HEALTHY` | — | 无故障 |
| 1 | `L1` | `NotHandleFault` | 无需处理 |
| 2 | `L2` | `RestartRequest` | 可自愈（触发token级重推） |
| 3 | `L3` | `RestartBusiness` | 无法自动处理 |
| 4 | `L4` | `FreeRestartNPU` | 需隔离并触发恢复策略（缩P保D） |
| 5 | `L5` | `RestartNPU` | 需 NPU 重启 |
| 6 | `L6` | `SeparateNPU` / `PreSeparateNPU` | 需 NPU 分离 / 节点重启 |

### 隔离与恢复机制

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

## 自动重拉起注册

当 Pod 因故障被 K8s 重启后，NodeManager 需要重新向 Controller 注册并重新组装实例、拉起推理引擎。该过程由 `InstanceAssembler` 和 `InstanceManager` 协同 K8s 完成，无需人工干预。

### 整体流程

```text
Pod 因故障被 K8s 重启
        │
        ▼
NodeManager 启动，RegisterManager._register() 发送 RegisterMsg 到 Controller
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
InstanceAssembler._start_command_sender 发送 StartCmdMsg
  - 携带 job_name, role, instance_id, endpoints, master_dp_ip, ranktable
        │
        ▼
NodeManager 接收 StartCmdMsg
  - parse_start_cmd(): 校验参数，存储 instance_id 和 endpoints
  - Daemon.pull_engine(): 启动原生推理引擎进程
  - HeartbeatManager.start(): 开始心跳上报
        │
        ▼
InstanceManager 收到心跳 → 状态机: INITIAL → ACTIVE
实例恢复正常服务
```

### 关键组件交互

**NodeManager 侧（[register_manager.py](https://gitcode.com/Ascend/MindIE-Motor/blob/master/motor/node_manager/core/register_manager.py)）**：

- `_register()`：NodeManager 启动后自动向 Controller 发送 `RegisterMsg`（含 job_name、role、pod_ip、parallel_config、device_num、ranktable 等），失败后指数退避持续重试直至成功。
- `parse_start_cmd()`：接收 Controller 的 `StartCmdMsg`，校验参数后存储 `instance_id` 和 `endpoints`，并将 ranktable 写入本地文件供引擎使用。

**Controller 侧（[instance_assembler.py](https://gitcode.com/Ascend/MindIE-Motor/blob/master/motor/controller/core/instance_assembler.py)）**：

- `register()`：根据 `_eval_register_status()` 判断当前状态——若 `InstanceManager` 中已存在 ACTIVE 实例则跳过；若已在组装中则更新时间戳；否则创建新 `Instance` 并加入组装队列。
- `_assemble_instance()`：检查各 NodeManager 是否存活、端点数量是否满足并行配置要求（`is_endpoints_enough()`），满足则标记为 `ASSEMBLED`。
- `_send_start_command()`：向实例中每个 NodeManager 下发 `StartCmdMsg`，通知其启动推理引擎。

**Controller 重启场景（re-register）**：

当 Controller 自身重启后，NodeManager 的心跳会收到 503 响应，触发 `HeartbeatManager._reregister()`。与首次注册不同，重注册发送的 `ReregisterMsg` 携带已有的 `instance_id` 和 `endpoints`，Controller 据此还原实例身份，跳过 `StartCmdMsg` 下发（引擎已在运行），直接交还给 `InstanceManager` 管理。

### 与故障隔离的关系

自动重拉起注册是故障恢复链路的关键闭环：`FaultManager` 负责故障检测与隔离决策，而 Pod 重启后实例的重新组装和引擎拉起则由 `InstanceAssembler` + `InstanceManager` 完成。两者通过 `forced_separated_instances` 集合衔接——只有 `FaultManager` 调用 `recover_instance()` 解除隔离后，实例状态机才允许从 `INACTIVE` 恢复为 `ACTIVE`。

## 缩P保D（Scale P2D）

### 背景

在 PD 分离部署模式（X 个 Prefill 实例 + Y 个 Decode 实例）下，当某个 Decode 实例发生 L4 及以上硬件故障（如 NPU 卡故障需隔离）且集群中没有冗余节点可供恢复时，Decode 能力面临丧失。

缩P保D 通过**缩减一个 Prefill 实例来释放节点资源**，将释放的节点用于拉起新的 Decode 实例，在有限集群资源下最大化保障推理服务的可用性。

### 核心流程

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

### 关键设计点

- **触发条件**：仅 Decode 角色实例，且故障等级为 L4 / L5 / L6（L5、L6 委托 L4 策略逻辑）。
- **模式退化**：Decode 故障期间，Coordinator 调度层检测 `readiness == ONLY_PREFILL`，自动将路由模式回退为 `SINGLE_NODE`（参见 [PD 分离—实例就绪与回退](../pd_disaggregation.md)）。此过程对业务无感知，推理请求正常处理，仅延迟增加。
- **模式恢复**：新 Decode 实例心跳上报就绪后，Coordinator 感知到 P/D 均可用，自动恢复 PD 分离路由。
- **策略方向**：缩P保D 是单向的——只有 Decode 故障时才需要“借用” Prefill 的节点资源。Prefill 角色的 L4+ 故障不会触发此策略。
- **策略代码**：[scale_p2d.py](https://gitcode.com/Ascend/MindIE-Motor/blob/master/motor/controller/fault_tolerance/strategy/scale_p2d.py)

### 策略路由

[strategy.py](https://gitcode.com/Ascend/MindIE-Motor/blob/master/motor/controller/fault_tolerance/strategy/strategy.py) 中的策略映射：

```python
# L4 → ScaleP2DStrategy（仅 decode 角色，非 decode 返回 None）
# L5 → 委托 L4
# L6 → 委托 L4
```

## 引擎软件故障快恢

三种快恢共用 `EngineFastRecoveryStrategy` 的调度暂停、活动 DP 实时校验、NodeManager 并发广播、同轮 request_id 和状态轮询。`recovery_planner` 按硬件证据选择具体策略，不通过最高故障码单值猜测原因。

| 分支 | 进入条件（所有活动 DP 均 UNHEALTHY 后下发） | retry 参数 | 成功与失败 |
|------|------|------|------|
| HBM UCE | 软件 UNHEALTHY + 本实例设备的 `0x80E01801` | 单次 `abort=true`，不支持 resume | retry 后无论成功失败都等待所在节点全部设备 1801 清除；retry 成功且清码成功才结束；retry 失败但清码成功转 relaunch；清码超时/证据缺失转重配置 |
| 灵衢 token 重推 | 软件 UNHEALTHY + `0x00F1FEF5` 或 `0x08520003` | 先不携带 abort，明确失败后 `abort=true` | 完全复用无故障码快恢流程；失败转 relaunch，不等待灵衢清码 |
| 无故障码快恢 | 软件 UNHEALTHY，且未命中 HBM/灵衢专用码，也没有严重硬件阻断 | 第一轮不携带 abort；全 DP 明确失败后第二轮 `abort=true` | 全部 DP HEALTHY 即成功；再次失败、DEAD、失联、超时或部分下发失败转 relaunch；硬件证据失效则重配置 |

`enable_fast_recovery` 总开关默认关闭，未配置该键的环境升级后仍走常规恢复路径；新增 `enable_hbm_uce_recovery=true`、`enable_token_reinference=true`、`enable_no_fault_code_recovery=true` 三个独立分支开关，需同时开启 FT、总开关和相应分支。关闭某一分支不会转入另一种快恢；软件故障到齐时转重配置。

硬件先到时保存在 ConfigMap 故障视图中，已识别的 L1–L3 硬件码不单独发起快恢；首个软件故障打开共用的 120 秒收集窗口，不使用 5 秒严重硬件关联窗口去否定正常的 HBM/灵衢延迟上报。已清除的码不作为后来的分类依据；严重故障处理保留 ready 基线和已处理过滤；快恢原因分类仍检查当前存在的基线/已处理硬件码，避免误判为无故障码快恢，只排除已缩除设备。L4–L6 优先保留原有严重故障处理；其余按 HBM > 灵衢 > 无故障码快恢分类。1801 与灵衢码同时出现优先 HBM；其他低等级告警（包括未命中专用码的 `0x80E01809`）不阻断快恢。执行下发前和结束时复核严重硬件故障，迟到的严重故障阻止原分支继续下发及宣告成功。执行期间新增的可快恢故障码不改变本轮分支，也不因原因优先级变化将本轮判为失败；灵衢与无故障码快恢以全部引擎 HEALTHY 为成功条件，不等待新增 1801 清码。新增硬件故障保留到下一轮重新规划；仅有 1801、引擎仍 HEALTHY 时不立即发起 HBM 快恢，待所有 DP 再次 UNHEALTHY 后进入 HBM 分支。HBM 分支仍需等待 1801 清码。

无故障码快恢在当前协议里是“无专用快恢码、无严重硬件阻断的 UNHEALTHY”分类，不能仅凭这一状态证明异常一定是内存耗尽；该策略不限定具体软件异常原因。第二轮仍只接受所有 DP 对同一 request_id 明确报告 `ft_state=failed`，超时不代表上一轮 collective 已结束。

每轮引擎恢复使用 `recovery_round_timeout_sec=60` 秒；HBM 独立清码阶段使用 `hbm_uce_clearance_timeout_sec=60` 秒，成功或失败均进入该阶段。HBM 清码成功后的 relaunch 复用已有引擎重拉策略；关闭 `enable_engine_relaunch` 时转重配置。默认整体保护预算仍为 870 秒，按启用分支的最坏耗时计算：收集 + max(无故障码快恢两轮、HBM 一轮+清码、灵衢两轮) + max(组装、relaunch) + 30 秒余量。

无故障码快恢和 token 重推共用两轮 retry 与 relaunch 回退，保留独立入口和开关。
关闭 `enable_engine_relaunch` 则失败直接重配置。relaunch 失败、加载期间或稳定观察期内再次软件报错
均转重配置，不重新进入快恢。全部引擎 NORMAL 后连续健康 `engine_relaunch_stability_sec`（默认 60 秒）
才解除保护；探测失败会重新计时。

HBM 内存分类和 KV 清理见 [HBM UCE 原地快恢设计](hbm_uce_recover.md)；三种快恢的触发条件、分支选择、执行与回退见 [引擎快恢执行策略](fast_recovery_strategy.md)。

## 组件交互全景

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
│  │ 故障检测      │    │ 实例组装          │    │ 生命周期管理      │  │
│  │ 隔离/恢复     │    │ 下发StartCmd      │    │ 心跳/状态机      │  │
│  │ 策略调度      │    │                  │    │ forced_separated │  │
│  └──────┬───────┘    └────────┬─────────┘    └────────┬─────────┘  │
│         │                     │                       │            │
│         │  separate/recover   │                       │            │
│         └─────────────────────┴───────────────────────┘            │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ 策略中心                                                      │  │
│  │  全部 DP UNHEALTHY → EngineFastRecoveryStrategy               │  │
│  │  L6 Atlas 800I A2推理服务器 Prefill/Decode/多Pod union 隔离码 → NmSuicideStrategy │  │
│  │  L4/L5/L6 + decode（非 linkdown）→  ScaleP2DStrategy          │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
         │                                       │
         │  RegisterMsg / ReregisterMsg          │ StartCmdMsg
         ▼                                       ▼
┌────────────────────────────────────────────────────────────────────┐
│ NodeManager                                                        │
│  ┌────────────────┐    ┌──────────────────┐    ┌─────────────────┐ │
│  │ RegisterManager│    │ HeartbeatManager │    │     Daemon      │ │
│  │ 注册/重注册     │    │ 心跳上报          │    │ 拉起引擎进程     │ │
│  │ 解析StartCmd    │   │ 检测Controller重启│    │                 │ │
│  └────────────────┘    └──────────────────┘    └─────────────────┘ │
└────────────────────────────────────────────────────────────────────┘
```

## 相关文档

- [FaultManager 设计文档](fault_manager.md)
- [PD 分离特性说明](../pd_disaggregation.md)
