# FaultManager 故障管理器设计文档

## 概述

FaultManager 是 MindIE-PyMotor Controller 中负责故障容错管理的核心组件。它通过观察者模式监听实例生命周期事件，统一管理硬件故障（ConfigMap 上报）和软件故障（引擎异常），协调 ResourceMonitor 进行故障检测，并与 InstanceManager 配合进行实例隔离和恢复。

## 架构总览

### 模块拆分

FaultManager 通过 Mixin 模式将功能拆分到三个模块中，降低单文件复杂度：

```text
motor/controller/fault_tolerance/
├── fault_manager.py          (541 行)   FaultManager 主类
├── fault_types.py            (259 行)   枚举 + Pydantic 数据模型
├── mixin/
│   ├── __init__.py
│   ├── resource_manager.py   (448 行)   _ResourceManagerMixin
│   └── persistence.py        (144 行)   _PersistenceMixin
└── strategy/                            策略实现
```

```python
class FaultManager(_PersistenceMixin, _ResourceManagerMixin, ThreadSafeSingleton, Observer):
```

| Mixin | 职责 | 主要方法 |
|-------|------|---------|
| `_ResourceManagerMixin` | 节点同步、所有权交换、资源监控 | `_sync_instance_nodes`, `_add_new_instance_with_nodes`, `_swap_node_ownership`, `_create_resource_monitor_for_node`, `_handle_fault_info_update`, `_handle_node_status_update` |
| `_PersistenceMixin` | ETCD 持久化与恢复 | `persist_data`, `restore_data`, `_get_next_version` |
| FaultManager 自身 | 生命周期、配置、故障评估、策略处理 | `start`, `stop`, `_refresh_instance_fault_level`, `_process_instance_strategy`, `report_software_fault` |

所有 Mixin 不声明自己的 `__init__`，成员属性统一由 `FaultManager.__init__` 初始化，Mixin 方法通过 `self` 直接访问。

### 功能架构

```text
Controller 侧:
  FaultManager (核心)
  ├── 硬件故障: ResourceMonitor → ConfigMap → _handle_fault_info_update()
  ├── 软件故障: NodeManager/FaultReporter → HTTP API → report_software_fault()
  ├── 故障评估: _refresh_instance_fault_level() → 综合硬件+软件故障等级
  ├── 策略中心: _ft_strategy_center() → 按 fault_level 生成/管理恢复策略
  ├── 节点交换: _swap_node_ownership() → 跨 job 实例间节点所有权交换
  └── 数据持久化: ETCD Client

NodeManager 侧:
  FaultReporter (EngineManager 聚合)
  ├── ZMQ SUB → 订阅 vllm ClientSentinel PUB (每引擎一个 socket)
  ├── msgspec.msgpack 解码引擎状态
  ├── 状态去重 → 仅上报 dead/unhealthy 变更
  └── HTTP POST → Controller /controller/report_software_fault
```

## 故障上报链路 (端到端)

```text
vllm EngineCore 异常
  → (ZMQ DEALER) EngineCoreSentinel 发送 FaultInfo
    → (ZMQ ROUTER) ClientSentinel 接收并更新状态
      → (ZMQ PUB) 广播 engine_status (msgpack) 到 fault_state_pub_socket
        → (ZMQ SUB) FaultReporter._loop() 订阅 (每引擎一个 socket)
          → _process_zmq_engine_status() 去重后
            → _send_fault_to_controller() 注入 pod_ip
              → ControllerApiClient.report_software_fault()
                → POST /controller/report_software_fault
                  → FaultManager.report_software_fault(pod_ip)
                    → NodeMetadata.software_fault_infos += fault
                    → _refresh_instance_fault_level()
                      → 综合硬件+软件 → 更新 InstanceMetadata.fault_level
                        → 策略中心 → 生成/升级恢复策略
```

## 数据结构

### FaultInfo (统一故障模型)

```python
class FaultInfo(BaseModel):
    # 公共字段
    fault_category: FaultCategory  # HARDWARE / SOFTWARE
    fault_level: FaultLevel        # L0(HEALTHY) ~ L6
    fault_code: int                # 硬件故障码 或 SpecialFaultCode

    # 硬件专用
    fault_type: HardwareFaultType | None   # CARD_UNHEALTHY / CARD_NETWORK_UNHEALTHY / NODE_UNHEALTHY
    npu_name: str
    origin_fault_level: OriginFaultLevel | None

    # 软件专用 (from_exception 工厂方法自动填充)
    exception_type: str | None
    exception_message: str | None
    engine_id: int | None          # → instance_id
    engine_status: int | None      # EngineStatusType: 0=HEALTHY, 1=DEAD, 2=UNHEALTHY
    timestamp: str | None
    additional_info: dict | None
```

软件故障等级映射: `DEAD(1) → L2 (ENGINE_DEAD)`, `UNHEALTHY(2) → L2 (ENGINE_UNHEALTHY)`。软件故障会更新实例的 fault_level，但目前不触发自动恢复策略。

### NodeMetadata

```python
class NodeMetadata(BaseModel):
    pod_ip: str
    node_name: str
    instance_id: int
    job_name: str                          # 所属实例的 job_name，O(1) 查询
    node_status: NodeStatus                # READY / NOT_READY
    hardware_fault_infos: dict[int, FaultInfo]   # 硬件故障 (ConfigMap 刷新)
    software_fault_infos: dict[int, FaultInfo]   # 软件故障 (引擎上报)
```

`job_name` 字段在节点创建时设置，swap 时同步更新。通过直接读 `node.job_name` 替代频繁的 `InstanceManager.get_instance()` 查询。

硬件故障按 fault_code 做 key 覆盖刷新；软件故障按 fault_code 写入，不受硬件刷新影响。`_refresh_instance_fault_level` 综合两类故障评估。

### InstanceMetadata

```python
class InstanceMetadata(BaseModel):
    instance_id: int
    fault_level: FaultLevel           # 当前最高故障等级 (评估结果)
    fault_code: int                   # 触发该等级的故障码
    strategy_fault_level: FaultLevel  # 当前运行中策略对应的故障等级
    strategy: Any                     # 策略实例 (不可序列化)
    lock: threading.Lock              # 互斥锁 (不可序列化)
```

`strategy_fault_level` 用于：

- **策略降级保护**: 新故障等级 < 当前策略等级时忽略
- **同级去重**: 同等级不切换策略，避免 L2 内多策略互相抖动
- **清理范围**: 策略完成后清除所有软件故障

## 节点所有权交换

### job_name 语义

`job_name` 是**每个实例的唯一标识**，不是角色分类（如 "prefill" / "decode"）。它只在同一个实例重启时保持不变：

```text
P1: job_name="prefill-1"    D1: job_name="decode-1"
P2: job_name="prefill-2"    D2: job_name="decode-2"
```

`instance_id` 在每次重启时更新。`job_name` 不变 + `instance_id` 变化，是区分"同一个实例重启"和"节点在不同实例间迁移"的关键。

### 背景

物理节点（`node_name`）是稳定标识，`NodeMetadata` 上记录了该节点的故障历史。当 `scale_p2d` 等策略把节点在不同实例间迁移时，故障记录必须跟随物理节点走——否则被换走的故障节点还会继续触发原实例的策略。

### 关键设计

- **节点不随实例删除**: `INSTANCE_REMOVED` 时仅移除 `InstanceMetadata`，`NodeMetadata` 保留在 `self.nodes` 中，等待新实例认领
- **NodeMetadata.job_name 直接查询**: 不再依赖 InstanceManager，O(1) 字段访问即可判断节点归属
- **等量交换**: 接收了多少外来源节点，就归还多少我方节点给对方实例，两边节点数保持对应
- **唯一 job_name 天然隔离**: 孤儿搜索限定 `meta.job_name == new_job_name`，由于每个实例的 job_name 唯一，D1 的孤儿搜索不会抓到 D2 的节点

### 交换流程

在 `_add_new_instance_with_nodes` 中完成分类后，调用 `_swap_node_ownership`:

```text
1. [调用方] 遍历 self.nodes，将外来源节点和孤儿节点分别收集
2. [调用方] foreign_nodes: node.job_name != new_job_name（被换入的节点）
3. [调用方] orphaned_nodes: node.job_name == new_job_name 且不在新实例中（被换出的节点）
4. [_swap_node_ownership] 等量配对: foreign.instance_id/job_name ↔ orphaned.instance_id/job_name
5. [_swap_node_ownership] 剩余外来源: 单边接管（扩容场景）
6. [_swap_node_ownership] 剩余孤儿: 保留原值，等对方实例认领
```

### 场景示例

集群: 5 个 Prefill 实例（各 1 节点）+ 2 个 Decode 实例（各 4 节点），共 13 个物理节点。

```text
P1(id=1, job="prefill-1"): p_1
P2(id=2, job="prefill-2"): p_2
P3(id=3, job="prefill-3"): p_3
P4(id=4, job="prefill-4"): p_4
P5(id=5, job="prefill-5"): p_5

D1(id=6, job="decode-1"): d1_1, d1_2, d1_3(L6故障), d1_4(L6故障)
D2(id=7, job="decode-2"): d2_1, d2_2, d2_3(L6故障), d2_4
```

**Step 1 — 故障触发**: D1 故障 2 个节点, D2 故障 1 个节点。均触发 L6 → `scale_p2d`。

- D1 需要释放 2 个 Prefill 实例 → 选中 P1、P2
- D2 需要释放 1 个 Prefill 实例 → 选中 P3

**Step 2 — 策略交换后的物理分配**:

```text
D1'(id=8,  job="decode-1"):  d1_1, d1_2, p_1, p_2    (2个原健康节点 + 2个P节点)
D2'(id=9,  job="decode-2"):  d2_1, d2_2, d2_4, p_3    (3个原健康节点 + 1个P节点)
P1'(id=10, job="prefill-1"): d1_3(L6)                  (故障节点换入)
P2'(id=11, job="prefill-2"): d1_4(L6)                  (故障节点换入)
P3'(id=12, job="prefill-3"): d2_3(L6)                  (故障节点换入)
P4'(id=13, job="prefill-4"): p_4                       (未参与交换)
P5'(id=14, job="prefill-5"): p_5                       (未参与交换)
```

旧实例 1-7 删除 → `INSTANCE_REMOVED`。`self.nodes` 保留所有 13 个节点（instance_id 仍指向旧实例），`self.instances` 清空。

**Step 3 — D1'(id=8, job="decode-1") 的 INITIAL 处理**:

遍历节点 `{d1_1, d1_2, p_1, p_2}`:

```text
d1_1: node.job_name = "decode-1" → 同 job ✓
d1_2: node.job_name = "decode-1" → 同 job ✓
p_1:  node.job_name = "prefill-1" → 外来源 ✗
p_2:  node.job_name = "prefill-2" → 外来源 ✗
```

`foreign_nodes = {p_1(inst=1), p_2(inst=2)}`

孤儿搜索 — 遍历 `self.nodes` 中 NOT in {d1_1, d1_2, p_1, p_2}、且 `meta.job_name == "decode-1"` 的节点：

```text
d1_3: job_name = "decode-1" → 孤儿 ✓
d1_4: job_name = "decode-1" → 孤儿 ✓
d2_1: job_name = "decode-2" → 跳过 (job 不匹配)
d2_2: job_name = "decode-2" → 跳过
d2_3: job_name = "decode-2" → 跳过
d2_4: job_name = "decode-2" → 跳过
p_4:  job_name = "prefill-4" → 跳过
p_5:  job_name = "prefill-5" → 跳过
```

`orphaned = [d1_3, d1_4]` — **精确命中，不误抓 D2 的节点**。

交换: `d1_3(inst=6) ↔ p_1(inst=1)`, `d1_4(inst=6) ↔ p_2(inst=2)`

```text
结果:
  d1_1: inst=8  d1_2: inst=8  p_1: inst=8  p_2: inst=8   ← 归 D1'
  d1_3: inst=1  d1_4: inst=2                                ← 等 P1', P2' 认领
```

**Step 4 — D2'(id=9, job="decode-2") 的 INITIAL 处理**:

遍历 `{d2_1, d2_2, d2_4, p_3}`:

```text
d2_1, d2_2, d2_4: node.job_name = "decode-2" → 同 job ✓
p_3: node.job_name = "prefill-3" → 外来源 ✗
```

`foreign_nodes = {p_3(inst=3)}`

孤儿搜索 — `meta.job_name == "decode-2"`, NOT in {d2_1, d2_2, d2_4, p_3}:

```text
d1_1, d1_2, p_1, p_2: 已更新 job_name="decode-1" → 跳过
d1_3: job_name = "prefill-1" → 跳过
d1_4: job_name = "prefill-2" → 跳过
d2_3: job_name = "decode-2" → 孤儿 ✓
p_4, p_5: prefill → 跳过
```

`orphaned = [d2_3]` — **只有一个，精确命中**。

交换: `d2_3(inst=7) ↔ p_3(inst=3)`

**Step 5 — P1'(id=10, job="prefill-1") 的 INITIAL 处理**:

遍历 `{d1_3}`:

```text
d1_3: 当前 job_name = "prefill-1" (已被 D1' 的 swap 更新) → 同 job ✓
```

没有外来源，直接更新 `instance_id = 10`。P2', P3' 同理。

**最终状态**:

```text
self.nodes:
  d1_1, d1_2, p_1, p_2: inst=8  (D1')
  d2_1, d2_2, d2_4, p_3: inst=9  (D2')
  d1_3(L6): inst=10               (P1')
  d1_4(L6): inst=11               (P2')
  d2_3(L6): inst=12               (P3')
  p_4: inst=13                    (P4')
  p_5: inst=14                    (P5')
```

- D1 的 L6 故障跟随 `d1_3`, `d1_4` → P1', P2'，不再触发 D1 的策略
- D2 的 L6 故障跟随 `d2_3` → P3'，不再触发 D2 的策略
- 整个过程**与 INSTANCE_INITIAL 到达顺序无关**：唯一 `job_name` 天然保证每个实例的孤儿搜索只命中自己的节点

### 边界情况: 一方延迟超过 20 分钟

如果 `d1_3` 的硬件故障需要超过 20 分钟才能恢复，旧 P1(id=1) 从 InstanceManager 中删除。届时 P1' 创建时：

```text
d1_3: job_name="" (旧节点无 job_name) → 跳过外来源检测 (node_job 为空)
→ _ensure_node_metadata 直接将 instance_id 更新为 10, job_name="prefill-1"
```

节点被直接认领而不经过交换。**故障历史跟随物理节点，谁拿到物理节点就归谁**。

### 非等量场景

| 情况 | 处理 |
|------|------|
| 外来源 > 孤儿 | 多出的 foreign 单边接管（扩容） |
| 孤儿 > 外来源 | 多出的孤儿保留原 instance_id，等对方认领 |

## 策略中心核心逻辑

`_process_instance_strategy()` 按 fault_level 比较决定策略行为：

| 条件 | 行为 | 说明 |
|---|---|---|
| `new_level > current_level` | **升级** | 停止旧策略，启动新策略 |
| `new_level == current_level` | **同级保持** | 不切换，避免同类内策略抖动 |
| `new_level < current_level` | **降级忽略** | 保护高优先级策略继续运行 |
| 策略完成 | **清理** | 清除实例全部软件故障 → 重新评估故障等级 |

L2 策略按 fault_code 分发:

- 软件故障（`ENGINE_DEAD`, `ENGINE_UNHEALTHY`）→ 暂不实现自动恢复，仅更新实例故障等级
- 白名单硬件码 `0x00F1FEF5`, `0x08520003` → TokenReinferenceStrategy
- 其它 → None

L4/L5/L6 → 根据实例角色 (decode) → ScaleP2DStrategy

### 策略与故障解耦

策略本身不感知故障信息，只负责执行恢复动作。`FaultManager` 通过 `strategy_fault_level` 记录策略对应的故障等级，策略完成后：

1. 清除实例所有软件故障（恢复动作已解决根因）
2. 重置 `strategy = None`, `strategy_fault_level = HEALTHY`
3. 调用 `_refresh_instance_fault_level()` — 硬件故障由 ConfigMap 异步刷新，剩余故障会被重新评估

清理操作在 `ins_metadata.lock` 外执行，避免与 `_refresh_instance_fault_level` 的锁顺序导致死锁。

## 实例隔离/恢复规则

- **fault_level > L2**: `InstanceManager().separate_instance()` 强制隔离
- **fault_level ≤ L2 且之前被隔离**: `recover_instance()` 恢复
- **fault_level = HEALTHY**: 确保实例处于恢复状态

## 配置参数

### Controller 侧 (FaultToleranceConfig)

| 参数 | 类型 | 说明 |
|---|---|---|
| `enable_fault_tolerance` | bool | 是否启用故障容错。默认: `true` |
| `strategy_center_check_interval` | int | 策略中心检查间隔(秒)。默认: `1` |
| `enable_scale_p2d` | bool | 是否启用 Scale P2D 策略。默认: `true` |
| `enable_token_reinference` | bool | 是否启用 Token Reinference 策略。默认: `true` |

### NodeManager 侧 (FaultToleranceConfig)

| 参数 | 类型 | 说明 |
|---|---|---|
| `enable_fault_tolerance` | bool | 是否启用故障上报线程。默认: `false` |
| `zmq_pub_port` | int | ZMQ SUB 订阅的基端口 (每个引擎 = base_port + engine_id)。默认: `0` |
