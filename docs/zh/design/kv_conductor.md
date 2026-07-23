# KV Conductor 设计文档

## 架构总览

```text
                        ┌──────────────────────────────────────┐
                        │           KV Conductor               │
                        │                                      │
   Engine Worker        │  ┌──────────┐    ┌────────────────┐  │
   (vLLM/SGLang)        │  │ Registry │    │    Indexer     │  │
       │                │  │          │    │                │  │
       │  register      │  │ workers  │───►│ DashMap<       │  │
       ├───────────────►│  │ endpoints│    │ (model, tenant)│  │
       │                │  └──────────┘    │    →Entry      │  │
       │  ZMQ / HTTP    │                  │                │  │
       │  KV events     │                  └───┬───────┬────┘  │
       ├───────────────►│                      │       │       │
       │                │               ┌──────┘       └──┐    │
       │  query         │               ▼                 ▼    │
       ├───────────────►│    ┌──────────────┐  ┌──────────────┐│
       │                │    │  HBM Tree    │  │  CPU/Disk    ││
   Coordinator          │    │ (RadixTree)  │  │ (LowerTier)  ││
       │                │    │              │  │              ││
       │  200 OK        │    │ prefix chain │  │ continuation ││
       │◄───────────────│    │ weight ×3    │  │  edges       ││
                        │    └──────────────┘  │ weight ×2/×1 ││
                        │                      └──────────────┘│
                        └──────────────────────────────────────┘
```

模块职责：

| 模块 | 文件 | 职责 |
|------|------|------|
| HTTP Server | `server.rs` | Axum 路由，CORS，TraceLayer |
| Worker Registry | `registry.rs` | 注册/注销，事件/查询路由，ZMQ 订阅管理 |
| Indexer | `indexer.rs` | Per-(model, tenant) 索引生命周期，评分聚合 |
| HBM Tree | `concurrent_tree.rs` | 并发 Radix Tree，前缀链匹配 |
| CPU/Disk Index | `lower_tier.rs` | Continuation-edge 图，断点续查 |
| Hashing | `hashing.rs` | XXH3 token → LocalBlockHash |
| Backend | `backend.rs` | 多后端适配（Mooncake/Memcache/YuanRong） |
| ZMQ | `zmq_subscriber.rs` | ZMQ SUB 事件接入 |
| Events | `events/` | vLLM/Pool 事件解析与规范化 |
| Protocols | `protocols.rs` | API 类型定义，wire format |

---

## 多级存储介质设计

### 三层模型

```text
   ┌─────────────────────────────────────────────────────┐
   │                  KV Conductor                       │
   │                                                     │
   │  ┌──────────┐   ┌──────────────┐   ┌──────────────┐ │
   │  │   HBM    │   │     CPU      │   │     DISK     │ │
   │  │  (XPU)   │   │  (Host DDR)  │   │  (SSD/NVMe)  │ │
   │  │          │   │              │   │              │ │
   │  │  Radix   │   │ Continuation │   │ Continuation │ │
   │  │   Tree   │   │    Edges     │   │   Edges      │ │
   │  │          │   │              │   │              │ │
   │  │ weight=3 │   │   weight=2   │   │   weight=1   │ │
   │  └────┬─────┘   └──────┬───────┘   └─────┬────────┘ │
   │       │                │                 │          │
   │       └────────────────┼─────────────────┘          │
   │                        │                            │
   │              ┌─────────▼─────────┐                  │
   │              │   OverlapScores   │                  │
   │              │  total = XPU +    │                  │
   │              │   CPU + DISK      │                  │
   │              └───────────────────┘                  │
   └─────────────────────────────────────────────────────┘
```

### HBM 索引：ConcurrentRadixTree

HBM 使用前缀链 Radix Tree，每个节点以 `LocalBlockHash`（XXH3 token-content hash）为键：

```text
  root
   │
   ├─[H₀]── Block { workers: {W1, W2}, block_hash: seq₁₀₀ }
   │    │
   │    ├─[H₁]── Block { workers: {W1, W2}, block_hash: seq₂₀₀ }
   │    │    │
   │    │    └─[H₂]── Block { workers: {W1}, block_hash: seq₃₀₀ }
   │    │
   │    └─[H₃]── Block { workers: {W2}, block_hash: seq₄₀₀ }
   │
   └─[H₄]── Block { workers: {W3} }
```

**为什么用 RadixTree？**

- `LocalBlockHash` 是独立 XXH3 内容哈希，不包含前缀信息
- 仅靠哈希值无法判断 "block 3 是否紧接 block 2"
- RadixTree 以 `parent → child` 的树结构显式编码前缀链
- 查询时从 root 逐层遍历，第一个缺失即停，天然保证最长连续前缀

**并发模型**：

- 查询路径（`find_matches`）：仅读锁，多个查询互不阻塞
- 变更路径（`apply_store`/`apply_remove`）：hand-over-hand 写锁，先锁父再锁子
- per-Worker 反向查找表（`WorkerLookup`）：`SequenceBlockHash → tree node`，O(1) 定位

**弱一致性语义**：RadixTree 不是 MVCC 结构。当 Worker 被移除时（`drop_worker`），
若节点的所有 Worker 均离开，其 `children` map 通过 `Arc::make_mut` 替换为新空 HashMap——
已有 `Arc` 引用的旧 map 不受影响，正在并发遍历的查询可以安全完成。
若只有部分 Worker 离开（Worker set 使用相同的 CoW 策略），旧 Arc 上的查询仍能看到修改前的 Worker 集合。
整体语义：查询快照 = 查询开始时刻树上已提交的状态，移除操作不阻塞查询遍历。

### CPU/Disk 索引：LowerTierIndexer

CPU/DISK 不使用完整 RadixTree，而是轻量的 **continuation-edge 图**：

```text
  TransitionKey: (parent_seq_hash, local_hash) → child_seq_hash

  示例：
    (None,     H₀)  ──→ seq₁₀₀    ← 从 root 开始
    (seq₁₀₀,  H₁)  ──→ seq₂₀₀    ← 续接
    (seq₂₀₀,  H₂)  ──→ seq₃₀₀    ← 续接
```

**为什么不用 RadixTree？**

- CPU/Disk block 数量远大于 HBM（可能千万级），完整树内存开销过大
- CPU/Disk 的查询总是**从 HBM 断点续查**，不需要从 root 开始
- Continuation-edge 图只存 `(parent, local_hash) → child` 边，内存高效
- 断点续查：HBM tree 返回 `(depth, last_seq_hash)` → 以此为起点在 edge 图中连续走

**匹配语义**：

```text
  query: [H₀, H₁, H₂, H₃, H₄]
  HBM tree 返回: depth=2, last_seq=seq₂₀₀ (W1)

  CPU continuation from (seq₂₀₀, H₂):
    edge(seq₂₀₀, H₂) → seq₃₀₀  ✅
    edge(seq₃₀₀, H₃) → seq₄₀₀  ✅
    edge(seq₄₀₀, H₄) → ???      ❌ 缺失 → stop

  CPU score = 2 blocks × 2 = 4
  total = 2×3 + 2×2 = 10
```

---

## 后端适配抽象

```text
                     ┌────────────────┐
                     │   StoreBackend │  (enum)
                     └───────┬────────┘
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌───────────┐  ┌───────────┐
        │ Mooncake │  │ Memcache  │  │ YuanRong  │
        │          │  │           │  │           │
        │ Central  │  │  Central  │  │  Per-DP   │
        │  Pool    │  │   Pool    │  │  Ports    │
        └────┬─────┘  └─────┬─────┘  └─────┬─────┘
             │               │               │
        ┌────▼────┐    ┌────▼────┐    ┌─────▼─────┐
        │ IpOnly  │    │ IpOnly  │    │   None    │
        │ IP→DPs  │    │ IP→DPs  │    │ port = DP │
        └─────────┘    └─────────┘    └───────────┘
```

**设计要点**：

- HBM 事件来自引擎 Worker，**不经过 Pool**。Worker 身份 = `(instance_id, dp_rank)`，后端无关。
- CPU/Disk 事件来自 Pool Master/Daemon，携带 `backend_id`（节点 IP 或端口）。
  - Mooncake/Memcache：`backend_id` = 节点 IP → 节点上所有 DP 共享同一 Pool 事件
  - YuanRong：每个 DP 独立端口，`backend_id` = 端口号 → 精确匹配 DP

**MatchMode 策略**：

| Backend | Pool `backend_id` | 事件如何关联 Worker |
|---------|-------------------|-------------------|
| Mooncake | 节点 IP（如 `10.0.0.1`） | `hbm_ip_index[IP]` → 该节点所有 DP |
| Memcache | 节点 IP | 同 Mooncake |
| YuanRong | 端口号（如 `15558`） | ZMQ 订阅端口 → 唯一 DP |

**注册流程差异**：

```text
  Mooncake/Memcache 注册:
    HBM: medium_endpoints={"xpu": "tcp://IP:50090"}  → 记录到 hbm_ip_index
    Pool: endpoint="tcp://master:5557"               → 启动一个全局 ZMQ SUB

  YuanRong 注册:
    HBM:   medium_endpoints={"xpu": "tcp://IP:15557"}
    CPU:   medium_endpoints={"cpu": "tcp://IP:15558"}  → 各启动一个 ZMQ SUB
    Disk:  medium_endpoints={"disk": "tcp://IP:15558"}  → 与 CPU 共享端口时去重
```

---

## 哈希设计

### 两种哈希的职责分离

```text
                 Engine computes                  Conductor computes
              ┌──────────────────┐             ┌──────────────────┐
              │ SequenceBlockHash│             │  LocalBlockHash  │
              │ (chained, parent │             │ (independent     │
              │  hash as seed)   │             │  XXH3, seed 1337)│
              ├──────────────────┤             ├──────────────────┤
              │ algo: engine def │             │ algo: XXH3       │
              │ seed: dynamic    │             │ seed: 1337       │
              │ deps: parent hash│             │ deps: tokens only│
              │ use:  reverse    │             │ use:  tree key   │
              │       lookup     │             │                  │
              └──────────────────┘             └──────────────────┘
                       │                                │
                       └───────────┬────────────────────┘
                                   │
                         ┌─────────▼─────────┐
                         │  KvCacheStored    │
                         │  BlockData        │
                         │  {block_hash,     │
                         │   tokens_hash}    │
                         └───────────────────┘
```

**为什么独立计算 XXH3？**

- 引擎的 `BlockHash` 是链式滚动哈希（`H_i = hash(H_{i-1}, tokens_i)`），编码了整个前缀
- Conductor 的 `LocalBlockHash` 是独立 XXH3（`H_i = XXH3(1337, tokens_i)`），仅编码本块内容
- 独立哈希使树节点可以被多个不同前缀的序列**共享**——两个 Worker 的同一 token 内容映射到同一个树节点
- 如果使用引擎的链式哈希，相同 tokens 在不同前缀上下文中会有不同哈希，无法共享

### 计算细节

```text
  fn compute_block_hash_for_seq(token_ids: &[i64], block_size: u32) -> Vec<LocalBlockHash>

  输入: token_ids = [t₀, t₁, ..., tₙ]
        block_size = B

  输出: [XXH3(1337, [t₀..t_B₋₁])],
         XXH3(1337, [t_B..t_₂B₋₁])],
         ...]

  实现:
    - little-endian 平台上使用 transmute: &[i64] → &[u8]，零拷贝
    - 大端平台逐字节写入
    - >2048 个 block 时启用 rayon 并行，batch_size=1024
```

---

## 评分与匹配逻辑

### find_matches_by_hash 完整流程

```text
  Input: [LocalBlockHash; N]   (查询序列)

  ┌─ Phase 1: HBM Prefix Match ──────────────────────────────┐
  │  tree.find_matches_detailed(hashes)                      │
  │                                                          │
  │  root → H₀ → H₁ → H₂ → (H₃ missing)                      │
  │                                                          │
  │  Result: {                                               │
  │    W1: PrefixMatch { depth: 3, last_seq_hash: seq₂₀₀ }   │
  │    W2: PrefixMatch { depth: 1, last_seq_hash: seq₁₀₀ }   │
  │  }                                                       │
  │  Score: depth × hbm_weight                               │
  └──────────────────────────────────────────────────────────┘
                           │
                           ▼
  ┌─ Phase 2: CPU/Disk Continuation ─────────────────────────┐
  │  lower_tier_lookup(hashes, hbm_results, tiers, weight)   │
  │                                                          │
  │  Continuation sources:                                   │
  │    a) Root workers:  edge(None, H₀) → ...                │
  │    b) HBM breakpoints: edge(seq₂₀₀, H₃) → ...            │
  │                                                          │
  │  query_contiguous_hits(hashes, continuations):           │
  │    walk: (parent, local_hash) → child                    │
  │    stop on first missing edge or missing worker          │
  │                                                          │
  │  Score: contiguous_blocks × weight                       │
  └──────────────────────────────────────────────────────────┘
                           │
                           ▼
  ┌─ Phase 3: Aggregate ─────────────────────────────────────┐
  │  OverlapScores { WorkerKey → total_score }               │
  │                                                          │
  │  Per-DP aggregation in build_response:                   │
  │    dp.xpu_score  = hbm_depth × hbm_weight                │
  │    dp.cpu_score  = cpu_contig × cpu_weight               │
  │    dp.disk_score = disk_contig × disk_weight             │
  │    dp.total      = xpu_score + cpu_score + disk_score    │
  │    dp.matched_tokens = max(hbm_depth, cpu_contig, ...)   │
  │                               × block_size               │
  └──────────────────────────────────────────────────────────┘
```

### 为什么要区分 "连续前缀" 和 "独立块"？

| | HBM RadixTree | CPU/Disk Continuation |
|---|---|---|
| 匹配方式 | root 出发，树遍历 | 从 HBM 断点/root 出发，边遍历 |
| 缺失处理 | 第一个缺失即停 | 第一个缺失即停 |
| 保证 | 匹配的块形成合法前缀链 | 匹配的块形成合法前缀链 |
| 与 vLLM prefix cache 的关系 | 语义一致——只能从 block 0 连续命中 | 语义一致——只能续接 |

如果不做连续匹配而只用平铺 HashMap（旧实现），会出现"block 0, 1, 3, 4 命中，block 2 缺失"的虚高分数，
即上一轮对话分析的核心问题。PR #565 通过 `LowerTierIndexer` 修复了此问题。

---

## 事件接入

### 双协议支持

```text
  ┌────────────────────────────────────────────────────┐
  │                  KvEventWirePayload                │
  │                     .normalize()                   │
  └────────┬──────────────────────────────┬────────────┘
           │                              │
  ┌────────▼──────────┐     ┌─────────────▼──────────┐
  │ Engine format     │     │ RFC #1527 format       │
  │ (vLLM msgspec)    │     │ (Mooncake pool)        │
  │                   │     │                        │
  │ {type: "stored",  │     │ {event_type: "stored", │
  │  blocks: [...],   │     │  seq_hashes: [...],    │
  │  parent_hash,     │     │  medium: "cpu",        │
  │  token_ids,       │     │  backend_id: "..."}    │
  │  block_size}      │     │                        │
  └────────┬──────────┘     └────────────┬───────────┘
           │                             │
           └──────────┬──────────────────┘
                      ▼
           ┌─────────────────────┐
           │ KvCacheEventData    │
           │ (canonical internal)│
           │                     │
           │ Stored / Removed /  │
           │ Cleared             │
           └─────────────────────┘
```

### 两阶段 Offload 匹配

CPU/Disk 事件分两阶段到达（详见 README 功能文档中的时序图），核心数据结构：

```rust
pub struct OffloadPoolState {
    // Phase 1 先到: block_hash → (tokens_hash, parent_hash)
    offload: FxHashMap<u64, NonHbmCacheEntry>,

    // Phase 2 先到: block_hash → {waiting workers}
    pending_pool: FxHashMap<u64, FxHashSet<WorkerKey>>,
}
// 不变量: 一个 block_hash 最多存在于两个 map 之一
```

**Phase 1 先到**：引擎 offload 事件到达 → 计算 `tokens_hash` → 缓存到 `offload` → 等待 Pool 确认
**Phase 2 先到**：Pool store 事件到达 → 排入 `pending_pool` → 等待引擎 offload 事件
**双方到齐**：匹配 `tokens_hash` + `parent_hash` → 构建 continuation edge → 插入 CPU/Disk 索引

过期清理：TTL 60s，每 100 次 ingest 触发一次 `sweep_stale_pending()`。

### ZMQ Wire Format

事件通过 ZMQ PUB 以 3 段消息送达：`[topic][seq: u64 BE][msgpack payload]`。

解析顺序（5 种格式依次尝试）：

1. **vLLM msgspec batch**：`[ts, [[tag, block_hashes, token_ids, block_size, medium, ...]], dp_rank]`
2. **vLLM 裸事件**：`[tag, block_hashes]`
3. **Pool backend batch**：`(timestamp_ms, [PoolEvent...], dp_rank)`
4. **vLLM JSON batch**：`{events: [...]}`
5. **Pool legacy**：旧格式兼容

事件中缺失的 `model_name` / `block_size` / `dp_rank` / `medium` 使用注册时的默认值补齐。

---

## 错误处理

| 错误 | HTTP 状态码 | 场景 |
|------|-----------|------|
| `DuplicateRegistration` | 409 | 同一 (instance_id, dp_rank) 重复注册 |
| `InstanceNotFound` | 404 | 对未注册实例执行操作 |
| `NoIndexer` | 404 | 查询时 (model, tenant) 无已注册 Worker |
| `NoWorkers` | 200 `{}` | 无缓存命中——正常，不视为错误 |
| `ParentBlockNotFound` | 500 | Store 事件引用了未知 parent hash |
| `InvalidBlockSequence` | 500 | 检测到自引用 block |

---

## 相关文件索引

| 文件 | 说明 |
|------|------|
| `motor/kv_conductor/src/` | Rust 源码 |
| `motor/kv_conductor/__init__.py` | Python 包入口，`is_available()`, `start()` |
| `motor/kv_conductor/__main__.py` | `python -m motor.kv_conductor` 入口 |
| `build.sh` | 条件编译，`KV_CONDUCTOR_PREBUILT` 支持预构建二进制 |
| `setup.py` | 条件 `package_data`，按需打包二进制到 wheel |
| `docs/zh/user_guide/features/kvcache_affinity.md` | 用户部署文档 |
| `motor/kv_conductor/README.md` | 功能简介 |
