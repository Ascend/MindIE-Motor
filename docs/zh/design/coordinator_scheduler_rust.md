# Coordinator 调度热路径 Rust 重构设计

> 状态：本分支已实现 R1–R4 代码路径（schema 4；无独立 Scheduler 进程；热路径 CAS）。§13.2 性能验收需 NPU 集群对照，不在单机交付范围。
> 基线代码：`b94fc20b`（2026-08-15，`master`）
> 相关文档：[熔断设计](circuit_breaker_design.md)、[精度检测](fault_tolerance/precision_detection.md)、[KV Conductor](kv_conductor.md)、[Coordinator 开发者指南](../developer_guide/components/coordinator.md)

本文描述将 Coordinator **调度热路径**从「Scheduler 进程单点账本 + 每请求 ZMQ」重构为「Rust 共享内存多进程原子记账 + Mgmt 控制面」的完整设计。负载均衡 / KV 亲和 / 轮询的**打分公式不变**；变的是账本介质、提交原语和进程边界。

---

## 1. 需求与目标

### 1.1 需求

| # | 需求 | 约束 |
|---|------|------|
| R1 | 负载记账的共享内存用 **Rust** 实现 | Infer Worker **多进程**对同一段 POSIX SHM 做**原子读写** |
| R2 | **删除 Scheduler 进程** | 实例变更由 **Mgmt 进程**通过 ZMQ PUB 广播给 Infer Worker |
| R3 | Scheduler 中的熔断等控制面能力迁到 **Mgmt** | 状态机语义与现网一致（见 [熔断设计](circuit_breaker_design.md)） |
| R4 | 优化前后 **不改变负载均衡算法逻辑** | 打分公式、提交量、fast-path / 慢路径仲裁语义保持等价 |

功能目标之外，本需求关闭还须满足性能目标（详见 [§13.2](#132-性能验收)）：**GLM5.1、并发 32、RR=0** 场景下，Coordinator **调度时延**（请求进入 Coordinator → 向 Prefill 实例发出 HTTP）的 **P99 降至优化前的 50% 及以下**（加速比 ≥ 2，即「减少一倍以上」）。

### 1.2 要解决的问题

当前生产热路径已经是「Worker 本地选点」，但每条推理请求仍强制 round-trip 到 Scheduler 进程提交账本：

```text
Infer Worker                              Scheduler 进程（单 asyncio + GIL）
────────────                              ────────────────────────────────
读 SHM → Python policy 打分
        ── ALLOCATE_ONLY (ZMQ) ──►        校验 / 可能全池重排
                                          InstanceManager ledger += demand
                                          seqlock 写 SHM
        ◄──── instance / endpoint ──
HTTP 转发 Engine
        ── UPDATE_WORKLOAD (ZMQ) ──►      ledger -= ; 再写 SHM
```

PD 分离是 **2× ALLOCATE + 2× UPDATE**。瓶颈不在选点公式，而在：

1. **每请求 Unix IPC + msgspec + pydantic**（TTFT 前至少 1 次）
2. **Scheduler 单事件循环**把账本 `+=`、全池打分、SHM `struct.pack` 跑在同一线程，阻塞其它 RPC
3. **Python SHM 单写者 seqlock**：没有硬件原子，N 个 Worker 的互斥靠进程汇聚，而不是靠介质本身

### 1.3 目标

- **数据面（每请求）**：Worker 本地打分后，对 SHM slot 做 CAS add/sub，**不再**走 `ALLOCATE_ONLY` / `UPDATE_WORKLOAD`
- **控制面（低频）**：实例成员表、熔断、精度采样全局门闩、SHM 生命周期、ZMQ PUB —— 全部归 Mgmt
- **算法**：`LoadBalancePolicy` / `KvCacheAffinityPolicy` / `RoundRobinPolicy` 的公式与提交量保持不变；ALLOCATE 的 fast-path / 权威重排用「CAS-expected + 本地重选」语义等价还原
- **工程**：Rust 以 **in-process `cdylib`** 插入，复用 kv-conductor 的 `build.sh` + `package_data` 打包带，**不**做成第二个独立 HTTP 进程
- **性能**：去掉每请求 ALLOCATE ZMQ 后，规定场景下 Coordinator 调度 P99 至少减半（§13.2）

### 1.4 非目标

- 不把 LB / KVA / RR 打分搬进 Rust
- 不把 KV Conductor 查询、tokenizer、HTTP 转发搬进 Rust
- 不上 pyo3 / maturin，不把 `py3-none-any` wheel 改成 manylinux 平台包（见 §8）
- 不改变 OpenAI / Anthropic HTTP API、Router 选择（`UnifiedPDRouter` / `PDHybridRouter`）、PD 降级语义
- 不在本方案中重做主备协议；只调整「谁先 bind、监督集去掉 Scheduler」

### 1.5 做完的判定

最终是否交付，以 [§13 最终验收标准](#13-最终验收标准) 为准：**四条需求目标（R1–R4）全部达成，且规定场景下调度 P99 减半（§13.2）**。不以「进程删了」或「有一个 `.so`」单独作为完成标志。开发顺序与测试门禁见 [§11](#11-现有测试评估与-tdd-适配)、[§12](#12-整体开发流程)。**未写齐契约测试、金标测试变红，不得进入下一阶段；不得为了保绿而把已删除的 ZMQ ALLOCATE 加回去。性能达标但不能证明 R4，或 R1–R4 达标但 P99 未减半，需求均未关闭。**

---

## 2. 现状（As-Is）

### 2.1 进程模型

```text
CoordinatorDaemon（spawn，非 fork）
│
├── SchedulerServer（1）     ZMQ ROUTER + PUB
│     拥有：InstanceManager master、WorkloadSharedMemoryWriter、
│           CircuitBreakerManager、精度全局表
│     启动顺序 1；停止顺序最后
│
├── MgmtServer（1）          管理 HTTP
│     拥有：InstanceManager mirror（typename=mgmt，唯一做 KV Conductor 注册）
│     DEALER 客户端，连 Scheduler；启动前 sleep 2s 等 Scheduler bind
│
├── ObsServer（1）           /metrics
│     经 SchedulerConnectionManager 拉实例
│
└── InferenceWorkers（N）    OpenAI / Anthropic HTTP，SO_REUSEPORT
      拥有：RequestManager、SchedulerClient（DEALER）、WorkloadSharedMemoryReader
```

启动：`Scheduler → Mgmt → Obs → Inference`；停止相反。HA 下 Scheduler / Mgmt / Obs 主备都跑，Inference 仅 master。

关键文件：

| 职责 | 路径 |
|------|------|
| 进程编排 | `motor/coordinator/daemon/coordinator_daemon.py`、`process/constants.py` |
| Scheduler 服务 | `motor/coordinator/scheduler/runtime/scheduler_server.py` |
| Worker 客户端 | `motor/coordinator/scheduler/runtime/scheduler_client.py` |
| ZMQ 协议 | `motor/coordinator/scheduler/runtime/zmq_protocol.py` |
| SHM | `motor/coordinator/scheduler/runtime/workload_shm/` |
| 策略 | `motor/coordinator/scheduler/policy/` |
| 熔断 | `motor/coordinator/domain/circuit_breaker.py` |
| Mgmt HTTP | `motor/coordinator/api_server/management_server.py` |

### 2.2 热路径调用链

```text
POST /v1/chat/completions
  → InferenceServer._handle_openai_request
    → router.dispatch.handle_request
      → select_router_class（本地 cache + CB unblocked 集，无 ZMQ）
      → UnifiedPDRouter | PDHybridRouter
        → BaseRouter.prepare_resource
          → AsyncSchedulerClient.select_and_allocate
              1) WorkloadSharedMemoryReader.read_and_patch_cache
              2) LoadBalance / RR / KVA 本地打分
              3) ZMQ ALLOCATE_ONLY          ← 热路径汇聚点
        → HTTP 转发 Engine
        → BaseRouter.release_all
          → ZMQ UPDATE_WORKLOAD             ← 释放路径汇聚点
```

`select_router_class`、策略打分、Conductor `/query` **已经在 Worker 本地**。Scheduler 作为 SoT 的原因不是选点，而是跨 Worker 的 **workload ledger、熔断、精度门闩、实例主副本**。

### 2.3 ZMQ 协议（现状）

ROUTER/DEALER：`ipc://<tmpdir>/scheduler_frontend`
PUB/SUB：`ipc://<tmpdir>/scheduler_instance_pub`

| RPC | 方向 | 热路径？ | 现状语义 |
|-----|------|----------|----------|
| `ALLOCATE_ONLY` | Worker → Scheduler | 是 | Worker 提案；sequence 匹配走 fast-path 接受 top-1，否则权威重排后 `update_workload_sync` + 写 SHM |
| `UPDATE_WORKLOAD` | Worker → Scheduler | 释放路径 | ledger `+=` 负 delta，clamp 到 0，写 SHM |
| `GET_AVAILABLE_INSTANCES` | Worker/Obs → Scheduler | 冷 | 全量 Instance dump + `workload_shm_name` |
| `REFRESH_INSTANCES` | Mgmt → Scheduler | 否 | 改 master，snapshot SHM，PUB |
| `CIRCUIT_BREAKER_REPORT` | Worker → Scheduler | 否 | fire-and-forget |
| `CONFIRM_SAMPLE` 等 4 个 | Worker → Scheduler | 否 | 精度跨 Worker 单点 |

PUB topic：`instances_changed`（ADD/DEL 带 delta 帧；SET 无 delta）、`circuit_breaker`。

### 2.4 当前 SHM 布局（以代码为准）

技能文档 `.agent/skills/motor-dev/references/coordinator.md` 仍写 SCHEMA=2、entry 32B、含 `active_kv_cache`。**代码是 SCHEMA_VERSION=3、entry 24B、只有 `active_tokens`。**

```text
Header 64B   magic="WKLD"  schema=3  seqlock sequence  entry_count
             max_entries=10240  instance_version  heartbeat
             prefill_sequence  decode_sequence  hybrid_sequence
Entry  24B   instance_id i32  endpoint_id i32  role u8  pad 3
             active_tokens f64  pad 4
Name         mindie_workload_<scheduler_pid>
总大小       64 + 10240 × 24 = 245824 B
```

- **唯一写者**：Scheduler。Worker 只读。
- Seqlock：writer 把 `sequence` 置奇数 → 写 entries → 置偶数。Reader 最多试 3 次。
- P/D/U 可用 per-role sequence **跳过扫表**（token 变化也会 bump role sequence）。
- Heartbeat 每 1s 写 offset 32；5s 不变则 Worker 视为 stale，走 `GET_AVAILABLE_INSTANCES`。
- 不是硬件原子：`struct.pack` 整段 memcpy；同进程互斥靠 GIL + 单 asyncio。

### 2.5 算法（必须保持）

策略 **不直接读 SHM**，只读 cache 上的 `endpoint.workload.active_tokens` 与 `instance.gathered_workload`。

**LoadBalance**（`load_balance.py`）：

```text
endpoint_score = active_tokens
instance_score = gathered_workload.active_tokens
n_ep = max(1, len(instance.endpoints))
score = endpoint_score + w * (instance_score / n_ep)
w = endpoint_instance_score_weight，默认 0.05
选全局 min；start_index 只用于并列打破
```

**RoundRobin**：per-process 计数器，`% len`；**不写 workload**（ALLOCATE 提交 0）。多 Worker 之间本就不共享计数器。

**KvCacheAffinity**（仅 P/U；其它 role 回退 LB）：

```text
matched_tokens = min(加权 block 覆盖, ISL)
prefill_cost   = max(0, ISL - overlap_credit * matched_tokens)
load_cost      = endpoint active_tokens 分数

unified（默认）:
  score = prefill_load_scale * prefill_cost + load_weight * load_cost
  Scheduler 慢路径用新鲜 ledger 重算 combined（load 用同一套 LB endpoint score）

load_gated:
  先按 load 取 topN，再在集合内取最长前缀
  Scheduler 只在候选里取最低 load，不再用 unified 软分
```

P/U 亲和路径提交量：`committed = ISL - matched_tokens`（`calculate_committed_workload`）。
非亲和 demand：E 用多媒体启发式；P/U 用 `len(token_ids)` 或线性启发式；D 用 `req_len`。

**ALLOCATE 仲裁（算法的一部分，不只是实现细节）**：

```text
if worker_role_seq == writer.role_seq AND instance_version 相等:
    fast_path: 校验 top-1（在池、角色、engine_type、CB 未 open）
else:
    LB: 全池重扫
    KVA unified: 用新鲜 load 重算 combined
    KVA load_gated: 候选里最低 load
    RR: 接受 Worker 提议
```

若重构后 Worker 读完就盲目 `atomic_add`，争用下会 herd 到同一 min-load endpoint，**慢路径语义丢失**，违反 R4。

### 2.6 熔断与精度（Scheduler 独占）

熔断（`CircuitBreakerManager`）：实例粒度；连续 3 次 failure → OPEN；`timeout = min(30 * 2^(trip_count-1), 300)` 秒；success 可提前关闭；到期对全部 endpoint `GET /health`；SET 清全部，DEL 清该实例。Worker 只有 PUB 镜像 `_cb_blocked_instances`；**最终闸门在 ALLOCATE**。

精度：按 PD group 的 sample 间隔门闩、streak、action_token、alarm 全在 Scheduler 对象上。Worker 侧采集 / checker / 告警上报可本地；跨 Worker 一致必须单点。详见 [精度检测设计](fault_tolerance/precision_detection.md)。

---

## 3. 目标架构（To-Be）

### 3.1 进程模型

```text
CoordinatorDaemon
│
├── MgmtServer（1）                 控制面 SoT
│     InstanceManager master（原 Scheduler 那份 + 原 mirror 的 KV 注册）
│     CircuitBreakerManager + /health probe
│     精度全局表
│     ZMQ ROUTER（仅控制面 RPC）+ ZMQ PUB
│     创建 POSIX SHM；Rust snapshot_write / heartbeat / set_blocked
│
├── ObsServer（1）                  改订 Mgmt PUB / GET
│
└── InferenceWorkers（N）
      Python policy + Router + RequestManager（不变）
      Rust SHM：atomic load / CAS add / CAS sub
      ZMQ SUB（实例 + 熔断）+ DEALER（精度 / CB 上报 / 冷启动 GET）
      无 ALLOCATE_ONLY / UPDATE_WORKLOAD
```

```text
                    Controller
                        │
                        │ POST /instances/refresh
                        ▼
                 ┌──────────────┐
                 │  Mgmt 进程    │
                 │              │
                 │ IM master    │────── POSIX SHM ──────┐
                 │ CB + probe   │   成员表 seqlock       │
                 │ Precision    │   token 槽 CAS         │
                 │ PUB + ROUTER │                        │
                 └──────┬───────┘                        │
                        │ PUB instances_changed          │
                        │ PUB circuit_breaker            │
                        │                                │
           ┌────────────┼────────────┐                   │
           ▼            ▼            ▼                   ▼
         Obs         Worker×N      Worker×N      Rust atomic R/W
                     policy 打分                  （每请求数据面）
                     CAS allocate
                     DEALER: CB / precision only
```

**禁止**再做一个「Rust Scheduler 进程」用 HTTP/ZMQ 接 allocate。那只是把 GIL 瓶颈换成另一个 RPC 汇聚点。KV Conductor 适合独立进程（跨节点索引）；workload 是同机、µs 级 mmap。

### 3.2 职责切分

| 层 | 谁 | 做什么 | 不做什么 |
|----|----|--------|----------|
| 数据面 | Infer Worker + Rust `.so` | 读 token、CAS 提交/释放 | 改成员表、跑熔断状态机 |
| 策略 | Python Worker | LB / RR / KVA 打分、Conductor query | 当权威账本 |
| 控制面 | Mgmt | 实例 REFRESH、SHM 成员快照、CB、精度、PUB、heartbeat | 每请求 allocate RPC |
| 观测 | Obs | 订 PUB，暴露 `/metrics` | 写账本 |

### 3.3 热路径（目标）

```text
select_and_allocate（Worker 本地，无 ZMQ）:
  1. Rust atomic load 本 role 全部 slot → patch cache
     （成员表按 instance_version 缓存；token 每次都 load）
  2. Python policy 打分（公式与现网同一套函数）
  3. slot_cas_add(slot, expected=当时 tokens, delta=demand)
       Ok      → 等价 fast-path
       Blocked → 槽已熔断，从候选去掉，回到 2
       Changed → 等价慢路径：回到 1，用新鲜 load 重打分
  4. 记下 committed，写入 RequestManager

release:
  slot_cas_sub_floor0(slot, delta=committed)   # 与现网 clamp 到 0 一致
```

Hybrid：1 次 allocate + 1 次 release。Unified PD：P、D 各一次，与现在次数相同，只是不再进 Scheduler。

---

## 4. 关键设计决策

| 决策 | 选择 | 不选 | 原因 |
|------|------|------|------|
| D1 账本 SoT | POSIX SHM + per-slot AtomicU64 | Python `InstanceManager.workload` | 多进程可见、无 RPC |
| D2 成员表 SoT | Mgmt 单写 + header seqlock | Worker 自己增删 slot | ADD/DEL 低频，避免槽位 ABA 失控 |
| D3 仲裁 | CAS-expected + 本地重选 | 盲目 fetch_add；或保留 ALLOCATE RPC | 满足 R4；又删掉热路径 ZMQ |
| D4 Rust 形态 | `cdylib` + C ABI + ctypes | 独立进程；pyo3/maturin | 见 §8 |
| D5 打分语言 | Python 现有 policy | Rust 重写公式 | R4；KVA 还依赖 Conductor HTTP |
| D6 熔断最终闸 | SHM slot `flags.blocked` + CAS 检查 | 只靠 Worker 本地 set | 替代 ALLOCATE 里的 `_is_instance_circuit_open` |
| D7 浮点 | f64 bit-CAS | 改成整数 milli-token | 不改 `Workload.active_tokens` 类型与公式 |
| D8 ZMQ 路径 | Mgmt bind 原 `scheduler_frontend` / `scheduler_instance_pub` | 立刻改名 | 减小 Worker/Obs 连接代码 diff；后续可改名 |

---

## 5. 共享内存设计（Schema 4）

### 5.1 两平面

这是整套方案的核心：

```text
控制面（低频，仅 Mgmt 写）
  槽里是谁、instance_version、blocked flags、heartbeat
  成员表变更走 header seqlock（与今天 snapshot 同构）

数据面（每请求，N 个 Worker 写）
  只对已知 slot 做 CAS add/sub
  不走全局 seqlock，不走 ZMQ
```

今天把 token 更新也绑进全局 seqlock，所以 Reader 能靠 `role_sequence` 跳过扫表。多写者之后 **每次选点必须 atomic load tokens**，否则看不见别人的 allocate。`role_sequence` / `instance_version` 只表示**成员变化**。

### 5.2 字节布局

总大小保持 `64 + 10240 × 24`，便于灰度时按容量创建。Magic 仍为 `0x574B4C44`（`"WKLD"`）。`SCHEMA_VERSION = 4`。不兼容 schema 3 读者（硬拒绝，与今天 schema mismatch 行为一致）。

**Header 64B**（little-endian）：

| Offset | Size | 字段 | 谁写 | 说明 |
|--------|------|------|------|------|
| 0 | 4 | magic | Mgmt 创建时 | `0x574B4C44` |
| 4 | 2 | schema_version | Mgmt | `4` |
| 6 | 2 | padding | — | 0 |
| 8 | 8 | membership_seqlock | Mgmt | 奇数=成员表写入中；偶数=稳定。**不因 token CAS 而变化** |
| 16 | 4 | entry_count | Mgmt | 有效槽数 |
| 20 | 4 | max_entries | Mgmt | 10240 |
| 24 | 8 | instance_version | Mgmt | REFRESH 导致成员变化时 +1 |
| 32 | 8 | heartbeat_sequence | Mgmt ~1s | `AtomicU64` Relaxed；不进 seqlock |
| 40 | 8 | prefill_membership_seq | Mgmt | 仅 P 成员变化时 +1 |
| 48 | 8 | decode_membership_seq | Mgmt | 仅 D |
| 56 | 8 | hybrid_membership_seq | Mgmt | 仅 U；Encode 无独立字段，读全局 membership_seqlock |

**Entry 24B**：

| Offset | Size | 字段 | 原子性 |
|--------|------|------|--------|
| 0 | 4 | instance_id | 仅 snapshot 时写 |
| 4 | 4 | endpoint_id | 仅 snapshot 时写 |
| 8 | 1 | role | 0=P 1=D 2=U 3=E |
| 9 | 1 | flags | bit0=`BLOCKED`（熔断 OPEN）；bit1=`VALID` |
| 10 | 2 | generation | 槽位复用时 +1，降低 ABA |
| 12 | 4 | reserved | 0 |
| 16 | 8 | active_tokens | **AtomicU64**，payload 为 `f64::to_bits`；必须 8 对齐 |

实现相对早期草稿的唯一布局修正：`active_tokens` 在 **偏移 16**（reserved 在 12）。24B 步长下偏移 12 只有 4 字节对齐，aarch64 上对 `AtomicU64` 会 bus error。字段集合、24B 大小与 CAS 语义不变。

CAS 必须同时校验 `(instance_id, endpoint_id, generation)` 仍匹配调用方记忆，防止 DEL 后槽被新 endpoint 占用。

### 5.3 内存序

| 操作 | ordering |
|------|----------|
| membership seqlock：writer 置奇 / 置偶 | `Release` 写 sequence；reader `Acquire` 读 |
| `active_tokens` CAS | `AcqRel` |
| `active_tokens` load（打分） | `Acquire` |
| flags `BLOCKED` | `AcqRel`（Mgmt 写，Worker CAS 时一起检查） |
| heartbeat | `Relaxed`（只做存活探测） |

跨进程 POSIX SHM 上的 `std::sync::atomic` 在 Linux/aarch64、x86_64 上与 C++11 同进程共享对象模型一致；本方案 **只支持 Linux**（与现网 SHM 测试 skip Windows 一致）。

### 5.4 f64 CAS 加减

硬件没有 `atomic f64 add`。实现：

```text
fn cas_add(slot, expected_bits, delta) -> Result:
    if !delta.is_finite() || delta < 0: return BadArg
    loop:
        cur = atomic_load(Acquire)
        if flags & BLOCKED: return Blocked
        if (iid,eid,gen) mismatch: return SlotInvalid
        if cur != expected_bits: return Changed(cur)   # 触发重选，不要在本槽硬加
        new_f = f64::from_bits(cur) + delta
        if cas(cur, new_f.to_bits(), AcqRel): return Ok
        # cas 失败：有人抢先，返回 Changed，让 Python 重打分
```

释放：

```text
fn cas_sub_floor0(slot, delta):
    if !delta.is_finite() || delta < 0: return BadArg
    loop:
        cur = load
        new_f = max(0.0, from_bits(cur) - delta)
        if cas(cur, to_bits(new_f)): return Ok
```

与现网 `active_tokens < 0` 则 clamp 0、并 rebuild `gathered_workload` 对齐：`gathered` **不再单独原子**；打分时对实例内各 endpoint slot load 再求和（O(DP)）。

释放路径允许无 expected 的 floor-CAS（请求结束时槽值已被别人改变是正常的）；**allocate 路径禁止**无 expected 的盲目 add，否则违反 R4。

### 5.5 成员表 snapshot（仅 Mgmt）

`REFRESH_INSTANCES` 本地落地后：

1. 从 `InstanceManager` 扫 E/P/D/U 全部 available endpoint（与今天 `_collect_entries_and_slot_map` 相同顺序，保证并列打破稳定）
2. seqlock 置奇
3. 写 entry 的 iid/eid/role/generation/flags。**已有 pair 的 tokens 禁止普通 store**：同槽留下 Worker CAS 值；换槽则 atomic-load 旧槽当前值再写入新槽。新 pair 才用 IM 种子。Python 侧对仍存活的 pair **保持 slot 稳定**（新 pair 占最低空槽，下线 pair 打 INVALID 洞），避免成员表变化时把别人的槽前移盖掉在途 CAS。
4. 只 bump 成员集合变化了的 role membership seq；`instance_version += 1`
5. seqlock 置偶
6. PUB `instances_changed`

超过 10240 槽仍截断并打 warning（保持现行为）。

Heartbeat：Mgmt 每 1s `heartbeat_sequence += 1`。Worker 5s 不变 → 认为 Mgmt 不健康，停止用 SHM 打分，走 `GET_AVAILABLE_INSTANCES` 或直接 503（与今天 stale 行为对齐：先 GET 再尝试）。

SHM 名：`mindie_workload_<mgmt_pid>`。创建遇 `EEXIST` 则 unlink 再建（其它 errno 不得 unlink 仍被占用的同名段）。Worker 从 GET 响应拿名字，不写死 PID。

### 5.6 CPython resource_tracker

CPython `multiprocessing.shared_memory` 在 attach 方进程退出时，resource_tracker 可能把仍在用的 POSIX 对象 `shm_unlink`。Rust 路径应：

- Mgmt：`shm_open(O_CREAT)` + `mmap`，**自己**持有 fd 直到进程退出
- Worker：Rust `shm_open` + `mmap`，**不要**经 Python `SharedMemory` attach；或 attach 后立刻 `resource_tracker.unregister`
- 只有 Mgmt（创建者）在正常退出时 `shm_unlink`

---

## 6. 算法不变：CAS-expected 如何等价 ALLOCATE

### 6.1 映射表

| 现网 ALLOCATE | Schema 4 |
|---------------|----------|
| `role_seq` + `instance_version` 匹配 → fast-path 接受 top-1 | CAS 时 `expected` 仍等于打分时 load → Ok |
| sequence 已变 → 权威重排 | CAS 返回 Changed → 重新 load + 同一套 Python 打分函数 |
| fast-path 但 top-1 已不在池 / CB open | `SlotInvalid` / `Blocked` → 去掉该候选再选 |
| LB 全池 `select_endpoint_candidates_from_list` | Worker 本地调用**同一个**函数 |
| KVA unified 用新鲜 load 重算 combined | Worker 用 cache 里已有 `prefill_cost` + 新 load 调同一 combined（把 `_select_affinity_global` **下沉为共享 Python 函数**，不改公式） |
| KVA load_gated 在 topK 里取最低 load | 失败后仍只在原 topK 上重选 |
| RR 接受提议、提交 0 | 不 CAS token（或 +0）；计数器仍 per-worker |
| P/U 提交 `ISL - matched` | 仍由 `calculate_committed_workload` 算出 delta 再 CAS |

### 6.2 必须下沉到 Worker 的 server 侧函数

这些函数今天在 `scheduler_server.py`，公式与 policy 重复。重构时抽到 `scheduler/policy/` 或 `scheduler/allocate_arbitration.py`，供 Worker 在 CAS 失败后调用：

- `_select_authoritative_allocate_candidate`
- `_select_global_load_balance_candidate`
- `_select_affinity_global`（`combined = pscale * prefill_cost + lweight * fresh_lb_score`）
- `_select_lowest_load_among_candidates`

**禁止**在 Rust 里再写一套 min-score。Rust 只回答「这个 expected 还能不能占坑」。

### 6.3 争用下的语义差异（明确承认）

单 asyncio 串行 ALLOCATE 与多进程 CAS 在**并列分数打破顺序**上不是指令级等价。R4 的范围是：

- 同一份打分输入 → 同一套 Python 函数 → 同一个 winner
- 账本已被别人更新 → 用新账本重新跑同一函数，而不是 herd 到旧 winner

`start_index = (n * client_index) // client_count` 的并列打破必须保留。

重试上限：建议有界（例如 16 次）后仍 Changed 则 **继续重选直到成功或无候选**（现网 ALLOCATE 也不会因争用失败，只是排队）。不要退化为盲目 add。

### 6.4 同一 Worker 连续请求

CAS 成功后必须立刻 patch 本进程 cache 的 `endpoint.workload` 与 `gathered_workload`，否则同一 Worker 的下一请求若碰巧跳过 load（不应发生：每次 select 都 load）也会用旧分。实现上：`slot_cas_add` 返回新值，Python 写入 cache。

---

## 7. 控制面迁到 Mgmt

### 7.1 实例变更（R2）

**现在：**

```text
Controller POST /instances/refresh
  → Mgmt 先 ZMQ REFRESH_INSTANCES 到 Scheduler
  → Scheduler 改 master + write_snapshot + PUB
  → Mgmt 再改 mirror
```

**目标：**

```text
Controller POST /instances/refresh
  → Mgmt InstanceManager.refresh_instances（唯一 master）
  → Rust snapshot_write（保留在途 tokens，见 §5.5）
  → PUB [instances_changed, version, optional delta]
  → KV Conductor register/unregister（本来就只在 typename=mgmt）
```

Worker `_InstancePushSubscriber` 协议不变：连续 `version+1` 且 ADD/DEL 有 delta → `cache.apply_add/apply_remove`；否则 `GET_AVAILABLE_INSTANCES`。SET/PAUSE/RESUME 仍无 delta，走全量 GET。

`REFRESH_INSTANCES` RPC **删除**（Mgmt 不再自己 RPC 自己）。

### 7.2 熔断（R3）

`CircuitBreakerManager` 整类迁 Mgmt，**状态机一个数字都不要改**（3 次、30s 指数、300s cap、early success、SET/DEL 清除）。

```text
Worker 请求失败/成功
  → DEALER CIRCUIT_BREAKER_REPORT（仍 fire-and-forget，不挡响应）
  → Mgmt process_failure / process_success
  → 若 trip / recover：
       1) PUB circuit_breaker {instance_id, state}
       2) Rust set_blocked(instance_id, blocked)
       3) 启动或取消 /health probe timer
```

Worker 本地 `_cb_blocked_instances` 继续用于 `select_router_class` 的 PD/hybrid 降级（允许略陈旧）。**最终闸**是 CAS 看到 `BLOCKED`。这替换今天 ALLOCATE 里的 `_is_instance_circuit_open`。

probe 仍是 Mgmt asyncio 里对每个 endpoint 业务端口 `GET /health`（2s/阶段），逻辑从 `scheduler_server._probe_instance` 原样搬迁。

现网文档 [熔断设计](circuit_breaker_design.md) 中「熔断中枢 = SchedulerServer」在本重构落地后应改为 Mgmt；本文合入实现时同步改那一页。

### 7.3 精度采样

下列状态表从 `Scheduler` 类迁到 Mgmt 进程内同等对象（可仍叫 `Scheduler` facade，或新建 `PrecisionCoordinator`）：

- `_sample_exit_last_time`、`_sample_exit_locks`
- `_precision_streak_counts` / `_precision_normal_streak_counts`
- `_precision_*_probing` / `_precision_*_tokens`
- `_precision_alarm_active` / `_precision_alarm_moi`

RPC 仍为 `CONFIRM_SAMPLE` / `RECORD_PRECISION_RESULT` / `FINISH_PRECISION_ACTION` / `DISMISS_PRECISION_ALARM_STATE`。`POST /precision/alarm_cleared` 改为 Mgmt **本地**调用，不再转 Scheduler。

Worker 的 `inject_logprobs`、Checker、Probe、Alarm **留在 Worker**。无单点会重复采样、重复告警。

### 7.4 ROUTER 职责精简

| RPC | 去向 |
|-----|------|
| `ALLOCATE_ONLY` | **删除** |
| `UPDATE_WORKLOAD` | **删除** |
| `GET_AVAILABLE_INSTANCES` | Mgmt（冷启动 / PUB 丢消息 / heartbeat stale） |
| `REFRESH_INSTANCES` | **删除** |
| `CIRCUIT_BREAKER_REPORT` | Mgmt |
| 4 × precision | Mgmt |

IPC 地址第一期仍用 `scheduler_frontend` / `scheduler_instance_pub`，避免 Worker 配置项扩散。文档与常量注释改为「由 Mgmt bind」。

### 7.5 Observability

`ObservabilityServer` 经 `SchedulerConnectionManager` 拉全量实例。对端改为 Mgmt 的 ROUTER/PUB 即可，不必给 Obs 再做一份 `InstanceManager`。`MetricsCollector.set_scheduler_provider` 名字可保留，provider 实现换成 Mgmt 视图。

---

## 8. Rust 如何插入本仓库

### 8.1 与 KV Conductor 对比

仓库里现有的唯一 Rust 是 `motor/kv_conductor/`：独立 `bin`，axum HTTP，Python `subprocess.Popen` + `ConductorApiClient`。`build.sh` `cargo build --release` 后 copy 到 `motor/kv_conductor/bin/`，`setup.py` 条件打进 **`py3-none-any`** wheel。没有 pyo3、maturin、`cdylib`。

| | kv-conductor | workload SHM（本方案） |
|--|--|--|
| 问题域 | 集群级 radix 索引 | 同机多进程账本 |
| 形态 | 独立进程 + HTTP | **in-process cdylib** |
| Python 调用 | HTTP | ctypes / 稳定 C ABI |
| 为何不做成进程 | — | 多一跳就抵消 SHM 的意义 |
| 打包 | copy bin → package_data | **同一套**：copy `.so` → package_data |
| wheel tag | 已是 none-any 里塞本机 ELF | 沿用，不为此平台化 |

### 8.2 为什么不用 pyo3

- 仓内零先例，根目录没有 `pyproject.toml`（打包是 `setup.py` + `pip wheel --no-deps`）
- wheel 会变成 `cp311-manylinux_*` / `macosx_*`，与 kv-conductor 的 none-any 模型冲突
- 每个 CPython 小版本、每种 arch 一条构建矩阵；Docker/CI 要绑 Python headers

只有在「整个 motor wheel 平台化」时才值得上 pyo3。为 SHM 单独开口，和现有打包会打架。

### 8.3 Crate 布局

不要塞进 `kv_conductor/`。新建：

```text
motor/coordinator/workload_shm_rs/
├── Cargo.toml                 # crate-type = ["cdylib", "rlib"]
├── src/
│   ├── lib.rs                 # C ABI 导出
│   ├── layout.rs              # 与 Python layout.py 同一套常量
│   ├── seqlock.rs             # 仅成员表
│   ├── slot.rs                # per-slot CAS
│   └── error.rs
└── tests/                     # cargo test：seqlock、CAS 争用、floor0、blocked
```

可选后续：Cargo workspace 把 `kv_conductor` 与本 crate 收在一起；第一期独立 `Cargo.toml` 即可，降低与 conductor 依赖（libzmq）的耦合。本 crate **不依赖 zmq / tokio / axum**。

建议 `Cargo.toml` 依赖保持极小：`libc`（`shm_open`/`mmap`）。原子用 `std::sync::atomic`。

### 8.4 C ABI（稳定、窄）

所有导出 `extern "C"`，整数错误码，不抛跨 FFI 异常。字符串为 UTF-8 + 显式 len，或纯整数句柄。

```text
typedef int32_t shm_status;
/* 0 Ok, 1 Changed, 2 Blocked, 3 SlotInvalid, 4 SchemaMismatch,
   5 NotAttached, 6 NoSpace, 7 Syscall */

shm_status mindie_wl_create(const char* name, uint32_t max_entries, uint64_t* handle);
shm_status mindie_wl_attach(const char* name, uint64_t* handle);
shm_status mindie_wl_close(uint64_t handle, int unlink);   /* 仅创建者 unlink=1 */

shm_status mindie_wl_snapshot_begin(uint64_t h);
shm_status mindie_wl_snapshot_write_entry(uint64_t h, uint32_t slot, const entry_t*);
shm_status mindie_wl_snapshot_commit(uint64_t h, uint32_t entry_count, int bump_instance_version);

shm_status mindie_wl_heartbeat(uint64_t h);
shm_status mindie_wl_set_blocked(uint64_t h, int32_t instance_id, uint8_t blocked);

shm_status mindie_wl_load_role(uint64_t h, uint8_t role, entry_t* out, uint32_t cap, uint32_t* n,
                               uint64_t* instance_version, uint64_t* membership_seq);

shm_status mindie_wl_cas_add(uint64_t h, int32_t iid, int32_t eid, uint16_t gen,
                             uint64_t expected_bits, double delta, uint64_t* actual_bits);
shm_status mindie_wl_cas_sub_floor0(uint64_t h, int32_t iid, int32_t eid, uint16_t gen,
                                    double delta, uint64_t* actual_bits);
```

`entry_t` 与 24B 槽一一对应（含 `generation`、`flags`、`active_tokens` 的 f64 或 bits）。Python `ctypes.Structure` 对齐到 24。

ABI 版本：在 `.so` 导出 `mindie_wl_abi_version() -> uint32_t`，与 `SCHEMA_VERSION` 解耦（ABI 变了但 layout 可不变）。

### 8.5 构建与打包

`build.sh` 在 kv-conductor 段落后增加同样的优先级链：

1. `WORKLOAD_SHM_PREBUILT` → copy `.so`
2. 有 cargo 且未设 `SKIP_WORKLOAD_SHM_BUILD=1` → `cargo build --release --manifest-path motor/coordinator/workload_shm_rs/Cargo.toml`，copy `target/release/libmindie_workload_shm.so`（macOS 为 `dylib`）到 `motor/coordinator/workload_shm_rs/lib/`
3. `lib/` 已有产物 → 跳过
4. 否则 WARNING，wheel 不含 `.so`；运行时 Python 报明确错误（调度不可用），**不要**静默回退到错误账本

`setup.py`：

```python
_shm_so = os.path.join("motor", "coordinator", "workload_shm_rs", "lib", "libmindie_workload_shm.so")
if os.path.isfile(_shm_so):
    _package_data["motor.coordinator.workload_shm_rs"] = ["lib/*"]
```

`.gitignore`：该 crate 的 `target/`、`lib/*.so`。`Cargo.lock` 是否入库：kv-conductor 当前 lock 被 ignore；本 crate **建议入库 lockfile**（纯 std+libc，可复现），若与仓规冲突则跟 conductor 一致并在文档写明。

`.pre-commit-config.yaml`：将 `cargo fmt` / `cargo clippy -D warnings` 的 `files:` 从只 `^motor/kv_conductor/` 扩到新 crate（两条 hook 或改成 workspace）。

`AGENTS.md` / `.agent/skills/motor-dev/references/coordinator.md`：与实现 **同一 PR** 更新进程图、SHM layout（schema 4）、RPC 表。这是仓库 Skill Sync 铁律。

Docker：`docker/mindie-motor-vllm/*/Dockerfile` 已有 cargo（为 kv-conductor）。新 crate 无 libzmq 依赖，只要 rustc。`SKIP_*` 行为与 conductor 对齐。

### 8.6 Python 加载

新建 `motor/coordinator/scheduler/runtime/workload_shm/native.py`：

- `ctypes.CDLL` 加载包内 `.so`（wheel）或 `target/release/`（源码开发）
- 封装为 `WorkloadShm` 类：`create` / `attach` / `load_role` / `cas_add` / …
- `writer.py` / `reader.py` 变为对 `WorkloadShm` 的薄封装，保持现有测试能按行为迁

源码开发：改 `.rs` 后需重新 `cargo build`（不像纯 Python 改完即生效）。`AGENTS.md` 写明。

---

## 9. Python 侧改动要点

### 9.1 `select_and_allocate`

`AsyncSchedulerClient.select_and_allocate` 删除 ZMQ `ALLOCATE_ONLY` 段，改为 §3.3 循环。返回值仍是 `tuple[Instance, Endpoint, Workload] | None`，Router **不改签名**。

`update_workload` / `release_all` 删除 ZMQ，改 `cas_sub_floor0`。Unified PD 后台 release + 3 次重试改为：CAS 失败（SlotInvalid）则忽略（实例已删）；其它错误打日志。现网 `CancelScope(shield=True)` 的「断开也要释放」仍然成立，只是目标从 ZMQ 换成 SHM。

### 9.2 InstanceManager 上的 ledger

热路径不再更新 Scheduler 进程里的 Python `endpoint.workload`。Mgmt 的 IM **不再作为 token 权威**，只作为：

- 成员表来源（refresh）
- readiness / KV 注册
- snapshot 时的初值（见 §5.5 保留在途 tokens）

Obs / metrics 若展示 per-endpoint load：从 SHM load，或 Worker 不提供、改由 Obs attach 只读 SHM（推荐：Obs attach reader，避免再走 GET 全量 dump 才能看到 load）。

### 9.3 连接管理

`SchedulerConnectionManager` 改名为实现细节仍可用；连接的是 Mgmt。Worker 启动：

1. DEALER connect Mgmt ROUTER
2. `GET_AVAILABLE_INSTANCES` → attach SHM
3. SUB connect PUB
4. 之后热路径不再用 DEALER

### 9.4 进程编排

`process/constants.py`：

```text
START_ORDER = [MGMT, OBS, INFERENCE]
STOP_ORDER  = [INFERENCE, OBS, MGMT]
```

删除 `SchedulerProcessManager` / `run_scheduler_server_proc`。Daemon 去掉「Scheduler bind 后再 sleep 2s」；改为 Mgmt **bind 成功、SHM create 成功** 后再起 Obs/Infer。HA 监督集 `{SCHEDULER, MGMT, OBS}` → `{MGMT, OBS}`。`on_become_standby` exclude 列表同步。

Standby Mgmt 仍吃 `/instances/refresh` 并维护 SHM，保证切主后 Worker 能立刻 attach 到**新 Mgmt 的 SHM 名**（切主时 Worker 需重新 GET；角色变化本就会停/起 Inference）。

### 9.5 配置

建议增加（默认关，阶段 2/3 打开）：

```text
scheduler_config.workload_shm_native: bool = False   # 阶段 1 可先强制 True 若 schema3 兼容写
scheduler_config.workload_shm_multi_writer: bool = False
```

落地完成后删除 flag，避免双栈长期共存。

---

## 10. 时序

### 10.1 一次 Hybrid 请求（数据面）

```text
Client → Worker HTTP
  Worker: load_role(P or U) from SHM
  Worker: LoadBalancePolicy.select_endpoint_candidates_from_list
  Worker: cas_add(expected, demand)
      Ok → RequestManager.add_req_workload
      Changed → 重 load + 重选（同函数）
  Worker → Engine HTTP
  Worker: cas_sub_floor0(committed)
  Worker → Client
```

无 Scheduler，无 ALLOCATE ZMQ。

### 10.2 实例 ADD

```text
Controller → Mgmt POST /instances/refresh ADD
  Mgmt IM.add
  Mgmt Conductor.register
  Mgmt snapshot_write（seqlock；新槽 tokens=0）
  Mgmt PUB instances_changed + delta
Worker SUB → cache.apply_add
  下次 select 的 load_role 看到新 slot（membership_seq 变，重建 slot 映射）
```

### 10.3 熔断 trip

```text
Worker Engine 失败 → REPORT failure（ZMQ，非热路径）
Mgmt failure_count==3 → OPEN
  set_blocked(id)=1
  PUB open
  start probe timer
Worker SUB → _cb_blocked_instances.add
  select_router_class 可能 PD→hybrid 降级
  其它 Worker 的 cas_add 对该实例 Slot 返回 Blocked
超时 /health 全 200 → CLOSED
  set_blocked=0，PUB closed
```

---

## 11. 现有测试评估与 TDD 适配

本章回答：当前 `tests/coordinator/` **能不能**作为本次重构的质量看护。结论先说：**只能锁住算法和状态机，撑不起「Rust 账本 + 删 Scheduler」的 TDD。** 按现有 suite 保绿，会把「ALLOCATE 还在走 ZMQ」测成通过，把「多进程 CAS 对不对」整段漏掉。

TDD 在本方案中的定义（硬性）：

```text
先写（或先冻结）契约测试 → 实现未完成时必须红（或 skip 且注明阶段）
→ 换介质 / 换进程后变绿
→ 绿的原因是行为对，不是 Python 私有符号或 ZMQ 还在
```

禁止：为了让旧测试绿回去而恢复 `ALLOCATE_ONLY` / Python `struct.pack` writer。

继续只用 `bash tests/run_tests.sh`，禁止直接 `python -m pytest`。每个 `motor/` 逻辑改动必须带对应测试（仓库 `AGENTS.md` 铁律）。

### 11.1 测试分层（要锁什么）

| 层 | 锁的契约 | 换 Rust / 删 Scheduler 后 |
|----|----------|---------------------------|
| L1 算法 | 固定 workload 向量 → 同一个 winner；demand / committed 公式 | 应继续绿，**零改公式文件** |
| L2 账本介质 | seqlock、CAS-expected、floor0、BLOCKED、generation、多进程守恒 | **今天几乎没有，P0/P1 必须先写** |
| L3 控制面 | CB 三次 trip、精度门闩、PUB delta 订阅 | 状态机可迁；发布端绑了 Scheduler，要改挂 |

### 11.2 现有用例分类（基线 `b94fc20b`）

#### 冻结金标：全程不许红（L1 / 部分 L3）

这些是固定输入 → 可观察输出，不依赖 Scheduler 进程名、不依赖 `.so`。P0 起列入「金标门禁」，任何阶段回归都失败即停。

| 文件 | 锁什么 | 用法 |
|------|--------|------|
| `tests/coordinator/scheduler/test_load_balance_policy.py` | tokens 10/5/15 → 最低；并列取先遇到的 | **公式测试零改动** |
| `test_round_robin_policy.py` | 计数器 0→1→2 wrap | 零改动 |
| `test_kv_cache_affinity.py` 中 winner 用例 | unified / load_gated、磁盘权重、短 prompt 跳 Conductor | 零改动公式断言 |
| `tests/coordinator/core/test_scheduler.py`：`test_load_balance_selects_global_lowest_endpoint`、RR 顺序用例 | 10/10/1/50 → `(inst=2, ep=20)` | 零改动 |
| `tests/coordinator/domain/test_circuit_breaker.py` | 3 次 trip、30s 指数、300s cap、early success、clear | 迁 Mgmt 只改 import |
| `test_precision_streak_scheduler.py`、`test_confirm_sample_exit.py` | 直调状态机，无 ZMQ | 迁 Mgmt 只改 import |
| `test_scheduler_client_cache.py` 的 ADD/DEL delta、version 跳号 | 订阅端帧语义 | 发布者换成 Mgmt 后帧格式不变则应绿 |

`test_scheduler_allocate_arbitration.py` 的 **12 条语义是金标**（fast-path 接受 top-1、mismatch 全池 LB、KVA unified `prefill_cost`、load_gated 只在候选集内重选），但全部走 `_SchedulerRequestDispatcher.dispatch(ALLOCATE_ONLY)`。P0 必须把仲裁抽到共享 Python 函数，**把这 12 条改挂到无 ZMQ 入口**；抽完之前不得宣称 R4 已有 TDD 门禁。

#### 实现耦合：预期红，禁止为保绿留旧路径

| 文件 / 用例 | 为何不能当门禁 |
|-------------|----------------|
| `test_workload_shm_writer.py` 大量测 `_buf` / `_slot_map` / `_write_header` / 私有 `_collect_entries_and_slot_map` | 换成 `.so` 会因 **import 私有符号** 误红，而不是 layout 错 |
| `test_workload_shm_reader.py` patch `unpack_header`；`test_read_and_patch_cache_stale_heartbeat` 允许 `{(7, True), (None, False)}` | 无 Writer→Reader 真 mmap 往返；心跳契约未锁死；**无奇数 seqlock 重试** |
| `test_workload_shm_roles.py` | 只测 Python 私有 mapper，不写 slot 字节 |
| `test_scheduler_client.py`：`test_select_and_allocate` 断言 `None or len==3` | 空断言，看护为零 |
| `test_select_and_allocate_transport_failure` | 断言 `send_request` 失败 → None；删 ALLOCATE 后必红 |
| `test_update_workload*` / `test_refresh_instances*` | 断言 transport 被调用 |
| `test_scheduler_server_main.py` 的 `TestHandleAllocateOnly*`、`TestCanUseWorkerTop1FastPath`（私有方法）、`TestHandleUpdateWorkload*` | 锁 ZMQ 载荷与 dispatcher 符号 |
| `TestAsyncSchedulerServerPublishInstanceChanged` | 发布端绑 `AsyncSchedulerServer` |

P1 起对这些用例的策略是：**改写成公共 API / 字节契约，或删除。** 不得为了 CI 绿而保留 Python writer 双栈或 ALLOCATE RPC。

#### 空白：TDD 必须先写、实现前允许 skip(P1/P2)

仓库内 **没有** `cas_add`、多进程双 writer、SHM `BLOCKED`、无 ZMQ 的 `select_and_allocate` 提交路径。ALLOCATE 最终闸 `_is_instance_circuit_open` 也无对应用例。Router 测试把 `get_unblocked_instances` mock 成全放行。

### 11.3 必须新增的契约测试（P0 先写，后阶段实现）

命名按「测行为、一测一事」。实现未就绪时用 `@pytest.mark.skip(reason="P1: native shm")` 等标明阶段，**禁止静默 skip**。

**L2 介质（驱动 Rust `.so`）**

| 建议文件 | 用例意图 | 红/绿条件 |
|----------|----------|-----------|
| `tests/coordinator/scheduler/test_workload_shm_writer.py` | Writer 写真实 POSIX SHM → Reader `read_and_patch_cache`：magic、schema、偶数 seq、entry 24B、heartbeat@offset 32 | P1：Python 或 `.so` 任一 writer 都必须绿；P1 结束时必须能对 `.so` 绿 |
| 同上 | 写中途 header sequence 为奇数 → Reader 返回不稳定 / 重试后成功 | 无 seqlock 则红 |
| 同上 | schema ≠ 当前版本 → 拒绝，不污染 cached role seq | 已有 reader 用例语义，改为走真 header 字节 |
| `tests/coordinator/scheduler/test_workload_shm_native.py` | `mindie_wl_cas_add`：expected 匹配 → Ok 且 tokens += delta | P2 前 skip |
| 同上 | expected 过期 → Changed，值不被盲目 add | 盲目 `fetch_add` 则红 |
| 同上 | `cas_sub_floor0` 不写出负数 | 负值则红 |
| 同上 | `set_blocked` 后 `cas_add` → Blocked，tokens 不变 | P3 可先在 P2 用 flags |
| 同上 | generation 变化 → SlotInvalid | schema 4 |
| 同上 | `multiprocessing.spawn` 双进程对同一 SHM CAS，最终值 = Σ delta | **R1 核心验收** |
| 同上 | 无 `.so` 时加载失败信息明确，不静默落到错误 Python 账本 | R-6 |

**L1 仲裁下沉（驱动「算法不变」）**

| 建议文件 | 用例意图 |
|----------|----------|
| `tests/coordinator/scheduler/test_allocate_arbitration.py`（由现文件改挂） | 无 ZMQ：固定 ledger 向量 → 与今日 12 条 **同一 winner / 同一 committed** |
| 同上或 `test_scheduler_client.py` | `select_and_allocate` **不** mock `send_request`：读 SHM → 打分 → CAS → 返回 `(Instance, Endpoint, Workload)` |
| 同上 | 人为把 expected 设成过期 → 第二次打分输入等于更新后的向量（同一套 Python 函数） |

**L3 控制面改挂**

| 建议文件 | 用例意图 |
|----------|----------|
| `test_circuit_breaker_report.py` 改 Mgmt | REPORT → 状态机数字不变 + `set_blocked` 反映到 SHM |
| `test_scheduler_circuit_breaker_probe.py` 改 Mgmt | `/health` 语义不变 |
| 新：`test_scheduler_server_main.py` | Mgmt refresh → PUB 帧 → 第二进程 cache `apply_add`（发布端不再绑 SchedulerServer） |
| 精度四个 RPC | 可选：Mgmt ROUTER round-trip；状态机金标已覆盖数字 |

### 11.4 现有文件在重构后的处置

| 现有文件 | 处置 |
|----------|------|
| `test_load_balance_policy.py` / `test_round_robin_policy.py` / KVA winner | **零改动**，金标门禁 |
| `test_scheduler_allocate_arbitration.py` | P0 抽函数后改挂；禁止继续 `dispatch(ALLOCATE_ONLY)` |
| `test_workload_shm_writer.py` / `reader.py` | P1 改为测公共 API + 字节；删除私有字段断言 |
| `test_workload_shm_roles.py` | 补「Encode role byte 出现在 slot 字节」；可删纯 mapper |
| `test_workload_shm_native.py` 孤儿段 | 迁到 Mgmt create 路径，行为保留 |
| `test_scheduler_client.py` 的 ALLOCATE/UPDATE/transport_failure | P2 **删除或改写**为 CAS 路径；禁止保绿 |
| `test_scheduler_server_main.py` ALLOCATE/UPDATE/fast_path 私有方法 | P3 删除 |
| `test_circuit_breaker.py` | 零改动 |
| `test_circuit_breaker_report.py` / probe | 对端 Mgmt；断言 SHM flags + PUB |
| `test_precision_streak_scheduler.py` | 状态表在 Mgmt，断言不变 |
| `test_scheduler_client_cache.py` | 保留；PUB 源测试改 Mgmt |
| `test_scheduler_connection_manager.py` | 对端 Mgmt，连接语义不变 |
| `conftest.py` | 增加可切换 Python / `.so` 的真实 shm 夹具（现仅有 policy Mock） |

### 11.5 Rust `cargo test`

crate 内至少覆盖：seqlock 奇数重试、多线程 CAS 守恒、CAS-expected 互斥、floor0、BLOCKED、generation、schema mismatch。`tests/run_tests.sh` 增加 `--rust`（默认在有 cargo 时跑本 crate；与 kv-conductor 现状对齐，也可先放 pre-commit clippy）。clippy `-D warnings` 扩到新 crate。

### 11.6 正确性 vs 性能

算法是否变了，只看 L1 金标（固定向量 → 同一 winner），**不能**用 P99 证明 R4。性能是否达标，只看 [§13.2](#132-性能验收) 的对照实验，**不能**用单测绿代替。Skill reference（`references/coordinator.md`）只记机制，不写入某次跑出来的毫秒数（仓库规范）。

---

## 12. 整体开发流程

原则：**契约测试先行、阶段门禁、金标永不破、实现 PR 带 Skill Sync。** 一次 PR 只跨越一个阶段（P0 可与 P1 同 PR 仅当 P0 测试已先合入或同一 PR 前半提交先加测试）。

```text
P0 钉门禁与补契约（测试为主，代码只抽函数）
  → P1 .so 单写者兼容（介质，不动热路径 RPC）
    → P2 Worker CAS + 去掉 ALLOCATE/UPDATE（数据面）
      → P3 控制面迁 Mgmt + 删除 Scheduler 进程
        → §13.1 + §13.3 功能验收
          → §13.2 性能对照（GLM5.1 / 并发32 / RR=0）
```

每个阶段内部循环：

```text
1. Red    契约测试已存在且失败（或 skip 已到期必须转失败）
2. Green  最小实现让本阶段契约 + 金标全绿
3. Refactor  去掉双栈 / 过期 flag（本阶段内能删就删）
4. Gate   本阶段验收表勾完才能开下一阶段分支/PR
```

源码开发：改 `.rs` 后必须 `cargo build`，不像纯 Python 改完即生效。

### 12.1 P0 — 钉金标与契约（不允许「先写 .so」）

**做什么**

1. 修正 `.agent/skills/motor-dev/references/coordinator.md` 的 SHM 描述（代码为 schema 3 / 24B / 仅 `active_tokens`），避免文档当错金标。
2. 把 `_select_authoritative_allocate_candidate` 等从 `scheduler_server.py` 抽到 `scheduler/allocate_arbitration.py`（或 `policy/` 共享模块），**行为零变化**。
3. `test_scheduler_allocate_arbitration.py` 改挂该模块；原 dispatcher 测试可暂时双跑，但 **新挂载必须无 ZMQ**。
4. 新增 §11.3 中 roundtrip / 奇数 seqlock 测试（可先对 Python writer 绿，证明契约不是绑 `.so` 符号）。
5. 列出金标命令并写入 PR 模板（见下）。

**门禁（P0 完成定义）**

- `bash tests/run_tests.sh tests/coordinator/scheduler/test_load_balance_policy.py`
- `... test_round_robin_policy.py`
- `... test_kv_cache_affinity.py`
- `... tests/coordinator/core/test_scheduler.py -k "load_balance_selects or round_robin"`
- `... test_scheduler_allocate_arbitration.py`（已改挂）
- `... tests/coordinator/domain/test_circuit_breaker.py`
- `... test_precision_streak_scheduler.py` `test_confirm_sample_exit.py`
- `... test_scheduler_client_cache.py`
- 新建 roundtrip 测试绿（Python writer 即可）

**禁止**：本阶段引入 `.so` 行为分叉；禁止改打分公式。

### 12.2 P1 — Rust 单写者（验证插入带）

**做什么**

- crate `workload_shm_rs` + ctypes `native.py`；`build.sh` / `setup.py` / pre-commit。
- `.so` 实现 **schema 3 兼容写**（原子 store + 真 seqlock fence）。Writer 仍在 Scheduler；热路径仍 ALLOCATE。
- `test_workload_shm_writer.py` 对 `.so` 绿；私有字段测试改写或删。

**门禁**

- P0 金标全绿。
- roundtrip + schema mismatch + heartbeat@32 对 `.so` 绿。
- 孤儿 SHM 恢复仍绿（create 路径）。
- 无 cargo 时：`SKIP_WORKLOAD_SHM_BUILD` 或缺失 `.so` → **启动报错明确**（本阶段若仍回退 Python writer，必须在日志中标明 deprecated，P2 起禁止回退）。

**回滚**：`SKIP_WORKLOAD_SHM_BUILD` + 预编译；P1 不删 Python writer 源码，但测试不得只覆盖 Python 私有 API。

### 12.3 P2 — 多写者 CAS，去掉热路径 ZMQ

**做什么（测试先红）**

1. 打开 `test_workload_shm_native.py` 的 CAS / 多进程用例（去掉 skip）。
2. schema 4：per-slot CAS、`generation`、`flags`；Reader **每次** atomic load tokens。
3. `select_and_allocate` 改为 CAS-expected 循环；删除 Worker 热路径 `ALLOCATE_ONLY` / `UPDATE_WORKLOAD`。
4. Scheduler 进程可暂时保留作控制面（CB/精度/PUB），ALLOCATE handler 变为死代码后删除，**不要**做成 shadow RPC。

**门禁**

- P0 金标全绿（含改挂后的 12 条仲裁）。
- 无 ZMQ 的 `select_and_allocate` 测试绿。
- 双进程 CAS 总量守恒绿。
- Changed → 重打分用例绿（第二次输入为新向量）。
- `test_select_and_allocate_transport_failure` **已删除或改写**，CI 中不再出现「必须 send_request」。
- feature flag `workload_shm_multi_writer=true` 为默认测试配置。

**回滚**：关 `multi_writer`（仅 P2 灰度窗口）。合入主干前应默认开；flag 在 P3 验收后删除。

Staging 可选 shadow 日志：`(req_id, proposed, committed, cas_attempts, score)`，只用于人工对比，**不是** §13 的通过条件。

### 12.4 P3 — 控制面迁 Mgmt，删除 Scheduler 进程

**做什么**

- IM master、SHM create/snapshot/heartbeat、CB+probe、精度表、ZMQ ROUTER/PUB 全部到 Mgmt。
- `START_ORDER = [MGMT, OBS, INFERENCE]`；去掉 2s sleep；HA 监督集去掉 SCHEDULER。
- `set_blocked` 写入 SHM；REPORT / 精度 RPC 对 Mgmt。
- 删除 `SchedulerProcessManager`。Skill Sync：`references/coordinator.md`、[熔断设计](circuit_breaker_design.md) 进程归属、精度文档中 Scheduler 表述。

**门禁**

- P0+P2 金标与 CAS 测试全绿。
- CB domain 测试零改动绿；report/probe 在 Mgmt 绿；SHM BLOCKED 阻止 `cas_add`。
- 精度 streak / confirm_sample 绿（进程在 Mgmt）。
- Mgmt PUB → Worker cache delta 绿。
- Obs 能从 Mgmt 拿到实例列表（metrics 非空路径）。
- 进程表断言：无 `PROCESS_KEY_SCHEDULER`；`zmq_protocol.py` 无 `ALLOCATE_ONLY` / `UPDATE_WORKLOAD` / `REFRESH_INSTANCES`。
- `bash tests/run_tests.sh tests/coordinator/` 全绿。

**回滚**：不支持热回滚，靠发版回退。因此 P3 必须先在分支上跑完全部门禁。

### 12.5 PR / 分支约定

| 规则 | 说明 |
|------|------|
| 一阶段一主 PR | 允许阶段内小步 PR，但不得把 P2 数据面和 P3 删进程混在同一 PR |
| 测试与代码同 PR | 新契约测试不得「下次再补」 |
| 金标 job | CI 或本地提交说明中列出 §12.1 命令；P2+ 加上 native CAS |
| Skill Sync | 改进程图 / SHM schema / RPC 表时，`references/coordinator.md` 同 PR |
| Commit | `[feature]` / `[fix]` 中文描述；不 skip hook |

### 12.6 角色与顺序（建议）

```text
P0  测试 + 抽 allocate_arbitration     （Python）
P1  Rust crate + ctypes + 打包         （Rust/Python 边界）
P2  Worker select_and_allocate CAS     （热路径）
P3  Mgmt 控制面 + Daemon 去 Scheduler  （进程模型）
```

P1 与 P2 不要对调：没有稳定 `.so` 插入带，就不要让 N 个 Worker 写 SHM。

---

## 13. 最终验收标准

验收 **按需求目标是否达成** 判定，不是按「改了哪些文件」。分两层：

| 层 | 对应什么 | 不通过则 |
|----|----------|----------|
| **功能验收** | 需求 1–4（Rust 账本、删 Scheduler、熔断迁 Mgmt、算法不变） | 需求未完成，即使 P99 变好也不关闭 |
| **性能验收** | GLM5.1 / 并发 32 / RR=0 下调度 P99 减半 | 需求未关闭；功能可合开发分支，但不得宣称优化达标 |

功能层的工程 checklist（§13.3）只是 **如何证明** 目标达成，不能写成另一套平行目标，更不能拿 checklist 绿替代性能实验。

---

### 13.1 功能验收（对应四条需求）

| ID | 需求目标（用户原话） | 通过条件（目标语言） |
|----|----------------------|----------------------|
| R1 | 负载记账的共享内存使用 Rust 实现，支持 Infer Worker 多进程原子化读写共享内存 | 账本在 Rust POSIX SHM 上；**多个 Infer Worker 进程**对同一段 SHM 做原子读（打分）和原子写（allocate/release CAS）；调度热路径 **不再** 为记账 round-trip 到独立 Scheduler 进程 |
| R2 | 删除 Scheduler 进程，实例变更由 Management 进程通过 ZMQ 广播给 Infer Worker | Coordinator **不再存在 Scheduler 子进程**；Controller 的实例变更进 Mgmt 后，由 Mgmt **ZMQ PUB** 通知各 Infer Worker（协议与现网 `instances_changed` 兼容） |
| R3 | 熔断等原先在 Scheduler 中实现的部分，转移到 Management 进程 | 熔断状态机、probe、精度跨 Worker 门闩等控制面 **只活在 Mgmt**；Infer Worker 只上报与执行过滤；语义与现网一致（三次 trip、指数超时、SET/DEL 清除等） |
| R4 | 优化前后不改变负载均衡算法逻辑 | 同一套 Python 打分与提交量公式；winner 在固定负载向量下与优化前一致；争用时用新账本 **重跑同一函数**，而不是换一套启发式 |

**功能验收通过规则：** 上表四行全部满足。证明手段见 [§13.3](#133-功能验收的工程证明)。

---

### 13.2 性能验收

在 **R1–R4 功能已落地的同一构建** 上做对照。本项是需求关闭条件；**不是** R4 的替代。

#### 场景

| 项 | 规定 |
|----|------|
| 模型 | **GLM5.1**（与对照实验同一权重 / 同一 PD 拓扑 / 同一 Worker 数） |
| 负载 | **并发 32**（闭环 in-flight=32） |
| RR=0 | **不额外限制到达率**（压测 `request_rate=0` / inf：有空槽即发，由并发打满）。该窗口内调度失败 / HTTP 503 须单独披露；**失败率 > 1% 则本场无效**，须先把场景跑稳再比 P99（等价于「拒绝不主导该场景」） |
| 调度策略 | 与优化前 **同一** `scheduler_type` / `kv_affinity`（保证比的是热路径，不是换了算法） |
| 对照 | 优化前：现网 Scheduler + ZMQ `ALLOCATE_ONLY`；优化后：本文 To-Be。硬件、副本数、ISL/OSL、流式/非流式一致 |
| 样本 | 去掉预热后 **至少 1000** 条成功发到 P 的请求；预热圈数两边相同 |

#### 指标定义（必须钉死，避免和 TTFT 混用）

**Coordinator 调度时延** \(T_{\mathrm{sched} \rightarrow P}\)：

```text
T_sched→P = t(Worker 对 Prefill 实例发出 HTTP 请求的时刻)
          − t(该推理请求进入 Coordinator 的时刻)
```

【代码事实】进入时刻为 `RequestInfo` 构造时写入的 `ReqState.ARRIVE`（`motor/coordinator/models/request.py`）。发出时刻为 `UnifiedPDRouter` / `PDHybridRouter` 在 **已经 `select_and_allocate` 成功之后**、第一次对 P（或 Hybrid 的 U/P）调用 `forward_request` / httpx POST **之前** 打点。

现网全量 INFO（禁止抽样）：

- 起点：`Scheduling metric stage=request_arrive req_id=… unix_ts=…`（`unix_ts` = `ReqState.ARRIVE`）
- 终点：`Scheduling metric stage=dispatch_to_p req_id=… unix_ts=… elapsed_ms=… role=prefill|union …`（`elapsed_ms` 即 \(T_{\mathrm{sched}\rightarrow P}\)）

本指标 **不含** Prefill 计算、KV 传输、Decode、首 token 返回（那些是 TTFT/E2E，不是本需求优化对象）。PD 分离只统计 **到 P 的这一段**；不把选 D 的时间算进去。

现网日志里的 `Scheduling latency ... stage=select_and_allocate` 只覆盖 allocate RPC，**短于** 本指标（还缺进入后到选点前、以及 allocate 后到发出 HTTP 前）。验收须用 **全量** 直方图或全量日志，**禁止**用 `_should_log_scheduling_sample` 抽样估 P99。

#### 通过线

```text
P99(T_sched→P)_优化后  ≤  0.5 × P99(T_sched→P)_优化前
```

即加速比 ≥ 2，对应需求表述「减少一倍以上」。须同时报告 P50/P99 与样本量；P50 仅作参考，**通过只看 P99**。

对比报告写入 PR/ISSUE（含集群、commit、命令、原始分位数），**不写入** skill reference。

---

### 13.3 功能验收的工程证明

以下条目用于 **证明 §13.1**，不是第二套需求。合入开发分支前应勾完；与 §13.2 一起勾完才关闭需求。

#### 总开关

| ID | 标准 | 验证方法 |
|----|------|----------|
| G1 | `bash tests/run_tests.sh tests/coordinator/` 全绿 | 命令 |
| G2 | §11.2 金标文件相对重构前 **断言未改弱** | review diff |
| G3 | 新 crate `cargo test` + `cargo clippy -D warnings` 通过 | 命令 |
| G4 | `references/coordinator.md` 与代码一致（无 Scheduler 进程、schema 4、热路径 RPC 已删） | 文档 diff |
| G5 | 无 pyo3/maturin；`build.sh` 产出的 wheel 含 `.so` | 构建日志 |

#### 证明 R1

| ID | 标准 | 验证方法 |
|----|------|----------|
| A1 | 热路径只经 `libmindie_workload_shm.so` CAS 记账，不再 `struct.pack` 写 tokens | 代码 |
| A2 | N≥2 Infer 进程对同一 SHM 并发 `cas_add`，最终值 = Σ delta | `test_workload_shm_native.py` |
| A3 | allocate 用 CAS-expected，禁止盲目 add | 单测 |
| A4 | `cas_sub_floor0` 后 `active_tokens >= 0` | 单测 |
| A5 | 成员表仅 Mgmt snapshot；Worker 不增删 slot | 代码 + 单测 |
| A6 | Worker 热路径无 `ALLOCATE_ONLY` / `UPDATE_WORKLOAD` | 协议 + client |
| A7 | 无 `.so` 时拒绝调度，不静默错账 | 负向启动 |

#### 证明 R2

| ID | 标准 | 验证方法 |
|----|------|----------|
| B1 | 无 Scheduler 子进程（`START_ORDER` / HA 监督集 / ProcessManager） | daemon 代码 |
| B2 | Mgmt bind PUB；Worker 收到的 `instances_changed` 来自 Mgmt | `test_scheduler_server_main.py` |
| B3 | `POST /instances/refresh` 本地落地 + snapshot + PUB，不再转发另一进程 | `management_server.py` |
| B4 | ADD/DEL delta、SET 全量 GET 与现网 Worker cache 一致 | `test_scheduler_client_cache.py` |
| B5 | `GET_AVAILABLE_INSTANCES`（含 shm 名）由 Mgmt 提供 | 单测 |
| B6 | 无「等 Scheduler 2s」；Mgmt bind+SHM 后再起 Infer | daemon |

#### 证明 R3

| ID | 标准 | 验证方法 |
|----|------|----------|
| C1 | `CircuitBreakerManager` 仅 Mgmt；3 次 trip；`min(30×2^(n-1), 300)` | `test_circuit_breaker.py` 零改动 |
| C2 | REPORT → Mgmt；PUB 更新 Worker `_cb_blocked_instances` | report 改挂 |
| C3 | SHM `BLOCKED` 为 CAS 最终闸 | native 单测 |
| C4 | `/health` 全 200 才关闭；不在池则 drop recovery | probe 改挂 |
| C5 | SET 清全部；DEL 清该实例 | 单测 |
| C6 | 精度门闩在 Mgmt；`/precision/alarm_cleared` 本地处理 | streak / confirm 测试 |
| C7 | inject logprobs / checker / alarm 仍在 Infer | 代码审查 |

#### 证明 R4

「不变」= 同一套 Python 公式 + CAS-expected 等价原 ALLOCATE 仲裁。并列打破允许与单 asyncio 非指令级一致（R-1）。

| ID | 标准 | 验证方法 |
|----|------|----------|
| D1 | policy / `workload_calculator` 公式测试 **未改断言且全绿** | G2 |
| D2 | LB：`ep + 0.05×(inst/n_ep)`，全局 min | policy 测试 |
| D3 | KVA unified / load_gated / P/U `ISL-matched` winner 一致 | KVA + 改挂仲裁 |
| D4 | RR 仍 per-worker 计数器、提交 0 | RR 测试 |
| D5 | 无争用：CAS 路径 winner = 仲裁函数 winner | 改挂测试 |
| D6 | Changed 后重跑 D1 同一函数 | CAS 用例 |
| D7 | `.so` 内无打分 / min-heap | crate 审查 |

#### 边界（随功能一并勾）

| ID | 标准 |
|----|------|
| E1 | OpenAI/Anthropic API、Router、PD 降级不在本需求改动范围；router 测试仍绿 |
| E2 | HA：Mgmt+Obs 主备都跑，Inference 仅 master |
| E3 | Obs 实例视图来自 Mgmt |
| E4 | KV Conductor 仍仅 Mgmt 注册；`/query` 仍在 Worker |
| E5 | 文档站本页改为已合入；熔断/精度文更新进程归属 |

---

### 13.4 明确不算验收通过

- 只删 Scheduler，但 ALLOCATE 改打到 Mgmt（热路径仍是 RPC）—— **R1/R2 未达成**
- 有 `.so` 但 Worker 仍只读、提交仍走 ZMQ —— **R1 未达成**
- 金标被改弱或 skip 来保绿 —— **R4 未达成**
- P99 减半但打分公式已改 —— **R4 未达成，性能无效**
- R1–R4 工程项全绿，但 §13.2 场景下 P99 未 ≤ 50% 基线 —— **需求未关闭**
- 用 TTFT/E2E 冒充 \(T_{\mathrm{sched} \rightarrow P}\) —— **性能验收无效**
- Python writer 与 Rust writer 在 P3 后仍双栈 —— **R1 未达成**

---

### 13.5 关闭顺序

```text
P3 功能工程证明勾完（§13.1 + §13.3）
  → 允许合入特性分支 / 拉起对照集群
    → §13.2 对照实验通过
      → 需求关闭，可合主干 / 发版
```

---

## 14. 文件变更清单（落地时）

### 14.1 新增

| 路径 | 说明 |
|------|------|
| `motor/coordinator/workload_shm_rs/` | Rust crate |
| `motor/coordinator/scheduler/runtime/workload_shm/native.py` | ctypes 封装 |
| `motor/coordinator/scheduler/allocate_arbitration.py`（建议） | 从 server 下沉的重选函数 |
| `docs/zh/design/coordinator_scheduler_rust.md` | 本文 |
| `tests/coordinator/scheduler/test_workload_shm_writer.py` | Writer→Reader 真 mmap 契约（P0/P1） |
| `tests/coordinator/scheduler/test_workload_shm_native.py` | FFI / 多进程 CAS（P2） |
| `tests/coordinator/scheduler/test_scheduler_client.py` | 无 ZMQ 的提交路径（P2） |
| `tests/coordinator/scheduler/test_scheduler_server_main.py` | Mgmt 为 PUB 源（P3） |

### 14.2 大改

| 路径 | 说明 |
|------|------|
| `workload_shm/layout.py` | schema 4 |
| `writer.py` / `reader.py` | 调 native；Reader 每次 load tokens |
| `scheduler_client.py` | 去掉 ALLOCATE/UPDATE 热路径 |
| `scheduler_server.py` | P3 删除或拆出控制面到 Mgmt |
| `management_server.py` | bind ROUTER/PUB、CB、精度、snapshot |
| `process/constants.py` / `*_manager.py` / `coordinator_daemon.py` | 去 Scheduler |
| `zmq_protocol.py` | 删除两种热路径 RPC |
| `observability_server.py` | 连 Mgmt |
| `build.sh` / `setup.py` / `.pre-commit-config.yaml` / `AGENTS.md` | 构建与门禁 |
| `.agent/skills/motor-dev/references/coordinator.md` | Skill Sync |

### 14.3 原则上不改

- `scheduler/policy/load_balance.py`、`kv_cache_affinity.py`、`round_robin.py` 的公式
- `domain/workload_calculator.py` 的 demand / committed
- `router/strategies/*` 的转发与 PD 降级（只换 facade 实现）
- `kv_conductor` crate、`ConductorApiClient`

---

## 15. 风险、边界与开放问题

| ID | 风险 | 缓解 |
|----|------|------|
| R-1 | 并列分数打破顺序与单线程不完全一致 | 打分函数下沉共享；金标锁死无争用路径；争用路径只要求「用新账本重跑同一函数」 |
| R-2 | snapshot 与 CAS 并发，槽被复用 | `generation` + `(iid,eid)` 校验；mismatch → SlotInvalid → 重选 |
| R-3 | CPython 误 unlink SHM | Rust 自己 `shm_open`；仅创建者 unlink |
| R-4 | 满 10240 截断 | 保持现行为；超限 warning |
| R-5 | HA 切主后 SHM 名随 Mgmt PID 变 | Worker 随 Inference 启停；切主走 GET |
| R-6 | 无 cargo 的构建环境 | `PREBUILT` / 预编译 `.so`；缺失则拒绝启动调度 |
| R-7 | 技能文档与代码长期漂移 | 实现 PR 必须改 `references/coordinator.md` |
| R-8 | `gathered_workload` 跨 endpoint 撕裂 | 与今天 seqlock 跨槽撕裂同级；打分一次 load 全部 slot，接受 µs 级非快照 |

**开放问题（实现前拍板）：**

1. Obs 是否 attach 只读 SHM 展示 load，还是继续只展示实例列表？
2. `UPDATE_WORKLOAD` 的 `operation_id` 去重 FIFO（上限 100_000，且源码注释称尚无 producer）—— CAS 后是否还要？建议 **P3 删除**，释放幂等靠「同一 req 只 sub 一次」（RequestManager 已有 committed）。
3. Encode role 无独立 membership seq（现网亦然）—— schema 4 是否补第四个 seq？建议保持与现网一致，避免无谓行为差。
4. IPC 路径是否在 P3 顺便改名为 `mgmt_*`？建议 **P3 不改路径**，减少协同成本。

---

## 16. 需求追踪

| 需求 | 设计落点 | 最终验收 |
|------|----------|----------|
| R1 Rust SHM，Infer 多进程原子读写 | `cdylib` + per-slot f64 CAS；成员表 Mgmt 单写 seqlock | [§13.1](#131-功能验收对应四条需求) + 工程证明 A1–A7 |
| R2 删 Scheduler；Mgmt ZMQ 广播实例 | Mgmt bind PUB；REFRESH 本地 + snapshot + PUB | [§13.1](#131-功能验收对应四条需求) + 工程证明 B1–B6 |
| R3 熔断等迁 Mgmt | CB + probe + 精度表迁 Mgmt；SHM `BLOCKED` 做最终闸 | [§13.1](#131-功能验收对应四条需求) + 工程证明 C1–C7 |
| R4 算法不变 | 公式留 Python；ALLOCATE 仲裁 = CAS-expected + 同一套重选函数 | [§13.1](#131-功能验收对应四条需求) + 工程证明 D1–D7 |
| 性能 | 去掉 ALLOCATE ZMQ 热路径 | [§13.2](#132-性能验收)：GLM5.1、并发 32、RR=0，\(T_{\mathrm{sched}\rightarrow P}\) P99 ≤ 50% 基线 |

功能证明细则见 [§13.3](#133-功能验收的工程证明)。否决项见 [§13.4](#134-明确不算验收通过)。关闭顺序见 [§13.5](#135-关闭顺序)。

**一句话：** Rust 只替换账本介质和提交原语；Python 继续决定选谁；Mgmt 继续决定谁还活着、谁被熔断。插入方式对齐 kv-conductor 的 **打包带**。开发按 P0→P3 TDD 推进；**R1–R4 功能达标且规定场景 P99 减半，需求才关闭。**
