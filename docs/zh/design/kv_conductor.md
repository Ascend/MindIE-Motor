# KV Conductor 设计文档

## 架构总览

```text
                        ┌──────────────────────────────────────┐
                        │             KV Conductor             │
                        │                                      │
   Engine Worker        │  ┌──────────┐    ┌────────────────┐  │
   (vLLM/SGLang)        │  │ Registry │    │    Indexer     │  │
       |                │  │          │    │                │  │
       |  register      │  │ workers  │--->│ DashMap<       │  │
       +--------------->│  │ endpoints│    │ (model,tenant) │  │
       |                │  └──────────┘    │   -> Entry     │  │
       |  ZMQ / HTTP    │                  │                │  │
       |  KV events     │                  └───┬───────┬────┘  │
       +--------------->│                      |       |       │
       |                │               ┌──────┘       └──┐    │
       |  query         │               v                 v    │
       +--------------->│    ┌──────────────┐  ┌──────────────┐│
       |                │    │  HBM Tree    │  │  CPU/Disk    ││
   Coordinator          │    │ (RadixTree)  │  │ (LowerTier)  ││
       |                │    │              │  │              ││
       |  200 OK        │    │ prefix chain │  │ continuation ││
       <----------------│    │ matched      │  │ edges        ││
                        │    │ block counts │  │ matched      ││
                        │    └──────────────┘  │ block counts ││
                        │                      └──────────────┘│
                        └──────────────────────────────────────┘
```

模块职责：

| 模块 | 文件 | 职责 |
|------|------|------|
| HTTP Server | `server.rs` | Axum 路由，CORS，TraceLayer |
| Worker Registry | `registry.rs` | 注册/注销，事件/查询路由，ZMQ 订阅管理 |
| Indexer | `indexer/mod.rs` | Per-(model, tenant) 索引生命周期、两阶段缓存、三层匹配聚合与维护 |
| HBM Tree | `concurrent_tree.rs` | 并发 Radix Tree，前缀链匹配 |
| CPU/Disk Index | `lower_tier.rs` | Continuation-edge 图，断点续查 + root 走查 |
| Hashing | `hashing.rs` | XXH3 token → LocalBlockHash |
| Backend | `backend.rs` | 多后端适配（Mooncake/Memcache/YuanRong） |
| ZMQ | `zmq_subscriber.rs` | ZMQ SUB 事件接入 |
| Events | `events/` | vLLM/Pool 事件解析与规范化 |
| Protocols | `protocols.rs` | API 类型定义，wire format |
| Error | `error.rs` | `KvConductorError` 错误类型 |

**身份模型**：每个缓存持有者是一个 `WorkerKey` 四元组 `(instance_id, backend_id, dp_rank, medium)`。
同一实例同一 DP 的 HBM / CPU / Disk 块是三个不同的 WorkerKey，查询时按 `(instance_id, dp_rank)`
聚合跨介质命中。`backend_id` 是块的来源后端（引擎实例或 pool daemon），用于事件路由。

**匹配语义**：查询先收集每 DP 各介质的绝对覆盖终点，再按优先级 NPU > CPU > Disk
互斥切分为 `npu_blocks` / `cpu_blocks` / `disk_blocks`（同前缀副本只归最高优先级介质）。
`matched_tokens = (npu + cpu + disk) × block_size`（未加权真实覆盖）。亲和性评分由
Coordinator 调度器（`kv_cache_affinity`）按 `*_blocks` 与配置项
`scheduler_config.kv_affinity.w_npu/w_cpu/w_disk`（默认 `1.0/1.0/0.0`）加权后完成。

---

## 多级存储介质设计

### 三层模型

```text
   ┌─────────────────────────────────────────────────────┐
   │                     KV Conductor                    │
   │                                                     │
   │  ┌──────────┐   ┌──────────────┐   ┌──────────────┐ │
   │  │   HBM    │   │     CPU      │   │     DISK     │ │
   │  │  (NPU)   │   │  (Host DDR)  │   │  (SSD/NVMe)  │ │
   │  │          │   │              │   │              │ │
   │  │  Radix   │   │ Continuation │   │ Continuation │ │
   │  │   Tree   │   │    Edges     │   │    Edges     │ │
   │  │          │   │              │   │              │ │
   │  └────┬─────┘   └──────┬───────┘   └──────┬───────┘ │
   │       │                │                  │         │
   │       └────────────────┼──────────────────┘         │
   │                        ▼                            │
   │             ┌─────────────────────┐                 │
   │             │    Query Response   │                 │
   │             │ npu/cpu/disk_blocks │                 │
   │             │   + matched_tokens  │                 │
   │             └─────────────────────┘                 │
   └─────────────────────────────────────────────────────┘
```

- 上层介质命中后，下层介质**从上层断点续查**；同时对拥有首边的 worker **无条件 root 走查**，与续接链并列，取绝对终点最远者（如实报告更长副本）
- HBM 是前缀树（从 root 走）；CPU/Disk 是 continuation-edge 图（断点续查 + root 走查）
- 同一 `(instance_id, dp_rank)` 的跨介质命中在响应中聚合到同一个 DP 条目

### HBM 索引：ConcurrentRadixTree

HBM 使用前缀链 Radix Tree，每个节点以 `LocalBlockHash`（XXH3 token-content hash）为键：

```text
  root
   |
   +-[H0]-- Block { workers: {W1, W2}, block_hash: seq100 }
   |    |
   |    +-[H1]-- Block { workers: {W1, W2}, block_hash: seq200 }
   |    |    |
   |    |    +-[H2]-- Block { workers: {W1}, block_hash: seq300 }
   |    |
   |    +-[H3]-- Block { workers: {W2}, block_hash: seq400 }
   |
   +-[H4]-- Block { workers: {W3} }
```

**为什么用 RadixTree？**

- `LocalBlockHash` 是独立 XXH3 内容哈希，不包含前缀信息
- 仅靠哈希值无法判断 "block 3 是否紧接 block 2"
- RadixTree 以 `parent → child` 的树结构显式编码前缀链
- 查询时从 root 逐层遍历，第一个缺失即停，天然保证最长连续前缀

**并发模型**：

- 查询路径（`find_matches_detailed`）：仅读锁，多个查询互不阻塞
- 变更路径（`apply_store`/`apply_remove`）：hand-over-hand 写锁，先锁父再锁子
- per-Worker 反向查找表（`WorkerLookup`）：`SequenceBlockHash → tree node`，O(1) 定位
- 遍历优化：active Worker 集合收缩到 1 个后改为单 Worker 成员检查，避免集合差集开销

**弱一致性语义**：RadixTree 不是 MVCC 结构。`workers` 使用 `Arc<FxHashSet>` 写时复制
（CoW）——查询热路径只 bump 引用计数，变更路径用 `Arc::make_mut`。当 Worker 被移除时
（`remove_worker`），若节点的所有 Worker 均离开，其 `children` map 一并清空以回收内存；
已有 `Arc` 引用的旧集合不受影响，正在并发遍历的查询可以安全完成。

**维护**：HBM `Removed` / `Cleared` 只做精确索引删除，不在事件热路径扫描整棵树。
后台 maintenance 按周期调用 `sweep_stale_nodes()`，统一回收无 Worker 且无子节点的空节点。
`Cleared` 会同时删除清空后的外层 `WorkerLookup` key；Worker 注销则由
`remove_worker_all_media` 清除该实例/DP 在所有介质上的索引。这样避免删除事件周期性出现
全树扫描延迟尖峰，同时保证停止 ingest 后孤儿节点仍会被回收。

### CPU/Disk 索引：LowerTierIndexer

CPU/DISK 不使用完整 RadixTree，而是轻量的 **continuation-edge 图**：

```text
  TransitionKey: (parent_seq_hash, local_hash) -> child_seq_hash

  Example:
    (None,    H0)  --> seq100    <- from root
    (seq100,  H1)  --> seq200    <- continue
    (seq200,  H2)  --> seq300    <- continue
```

**为什么不用 RadixTree？**

- CPU/Disk block 数量远大于 HBM（可能千万级），完整树内存开销过大
- Continuation-edge 图只存 `(parent, local_hash) → child` 边，内存高效；首边
  `(None, H₀)` 即 root 入口，断点处的边则可直接续接，无需物化整棵前缀树
- 查询时对拥有首边的 worker **无条件做 root 走查**，并与上游断点续查并列，
  每 worker 取绝对终点最远者——更长副本不被上游较短命中掩盖（旧 `skip_root`
  语义已弃用，见下文「匹配逻辑与查询流程」）
- 边以 `(parent_seq_hash, tokens_hash)` 双键定位——`parent_hash` 保证续接自正确的
  上游块，`tokens_hash` 保证命中相同内容（见 commit 29157f9）

**边所有权**：每条边有一个或多个 owner Worker（`Single` / `Multi` 双形态存储），
同一 block 被多个 Worker 共享时边自动升级为 `Multi`。查询要求走查路径上**每个 block
都由该 Worker 持有**——与 HBM 树的"Worker 集合逐层求交"语义一致。

**续查语义**（断点续查与 root 走查并列，取绝对终点最远者）：

```text
  query: [H0, H1, H2, H3, H4]
  HBM tree returns: W1 depth=2, last_seq=seq200

  Candidate a) breakpoint resume from (seq200, H2):
    edge(seq200, H2) -> seq300  OK
    edge(seq300, H3) -> seq400  OK
    edge(seq400, H4) -> ???     MISSING -> stop
    -> absolute end = 4

  Candidate b) root walk (always, if W1 owns edge (None, H0)):
    edge(None, H0) -> ... walk until first missing edge
    -> compare with a); keep farther absolute end

  Absolute ends: npu_end=2, cpu_end=<farthest absolute end>
  Exclusive: npu_blocks=npu_end, cpu_blocks=max(0, cpu_end-npu_end)
  matched_tokens = (npu + cpu + disk) × block_size   // unweighted coverage
  // Coordinator affinity: round((npu×w_npu + cpu×w_cpu + disk×w_disk) × block_size)
```

---

## 后端适配抽象

```text
                    ┌────────────────┐
                    │  StoreBackend  │  (enum)
                    └────────┬───────┘
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌───────────┐  ┌───────────┐  ┌───────────┐
        │  Mooncake │  │  Memcache │  │  YuanRong │
        │           │  │           │  │           │
        │  Central  │  │  Central  │  │   Per-DP  │
        │    Pool   │  │    Pool   │  │   Ports   │
        └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
              ▼              ▼              ▼
        ┌───────────┐  ┌───────────┐  ┌───────────┐
        │   IpOnly  │  │   IpOnly  │  │    None   │
        │  IP->DPs  │  │  IP->DPs  │  │ port = DP │
        └───────────┘  └───────────┘  └───────────┘
```

**设计要点**：

- HBM 事件来自引擎 Worker，**不经过 Pool**。Worker 身份 = `(instance_id, dp_rank)`，后端无关。
- CPU/Disk 事件来自 Pool Master/Daemon，携带 `backend_id`（节点 IP 或端口）。
  - Mooncake/Memcache：`backend_id` = 节点 IP → 通过 `hbm_ip_index` 关联到该节点所有 DP
  - YuanRong：每个 DP 独立端口，`backend_id` = 端口号 → 精确匹配 DP

**MatchMode 策略**：

| Backend | MatchMode | Pool `backend_id` | 事件如何关联 Worker |
|---------|-----------|-------------------|-------------------|
| Mooncake | `IpOnly` | 节点 IP（如 `10.0.0.1`） | `hbm_ip_index[IP]` → 该节点所有 DP |
| Memcache | `IpOnly` | 节点 IP | 同 Mooncake |
| YuanRong | `None` | 端口号（如 `15558`） | ZMQ 订阅端口 → 唯一 DP |

注意：`MatchMode::None`（YuanRong）下，事件内的 `backend_id` 会被**忽略**，改用订阅者
注册时的 backend_id（即引擎 `instance_id`）——否则 pool daemon 的 IP:port 会产生与 HBM
块不同的实例标识，破坏跨介质聚合。枚举另有 `IpAndDpRank`（按 IP + DP 精确匹配），
当前后端未选用，保留作扩展。

**注册流程差异**：

```text
  Mooncake/Memcache registration:
    HBM:  medium_endpoints={"npu": "tcp://IP:50090"}  -> hbm_ip_index
    Pool: endpoint="tcp://master:5557"               -> one global ZMQ SUB

  YuanRong registration:
    HBM:  medium_endpoints={"npu": "tcp://IP:15557"}
    CPU:  medium_endpoints={"cpu": "tcp://IP:15558"}  -> per-endpoint ZMQ SUB
    Disk: medium_endpoints={"disk": "tcp://IP:15558"} -> dedupe if shared port
```

### 注册、重注册与注销生命周期

- 首次注册会创建 `(model_name, tenant_id)` 对应的 `IndexerEntry`，并按去重后的 endpoint
  创建 ZMQ subscriber；HTTP-only 注册允许 endpoint 为空。
- 同一 `(instance_id, dp_rank)` 重注册时，旧 subscriber 会先停止。后端未变化时保留已有
  索引，仅更新 endpoint；后端变化时清除该实例/DP 的 NPU/CPU/Disk 索引和旧 HBM IP 映射。
- `replay_endpoint` 只在该 `instance_id` 首次出现时通过 `spawn_blocking` 执行历史事件回放。
- 注销会停止该实例/DP 的全部 subscriber，删除 HBM IP 映射及三层索引；删除最后一个 DP
  时，使用**注册记录中的** model/tenant 回收空 `IndexerEntry`，不信任注销请求里的同名字段。
- `POST /events` 的 `shutdown=true` 当前只记录日志，完整释放仍需显式调用 `/unregister`。

---

## 哈希设计

### 两种哈希的职责分离

```text
                Engine computes                 Conductor computes
              ┌──────────────────┐             ┌──────────────────┐
              │ SequenceBlockHash│             │ LocalBlockHash   │
              │ (chained, parent │             │ (independent     │
              │ hash as seed)    │             │ XXH3, seed 1337) │
              ├──────────────────┤             ├──────────────────┤
              │ algo: engine def │             │ algo: XXH3       │
              │ seed: dynamic    │             │ seed: 1337       │
              │ deps: parent hash│             │ deps: tokens only│
              │ use:  reverse    │             │ use:  tree key   │
              │      lookup      │             │                  │
              └─────────┬────────┘             └─────────┬────────┘
                        │                                │
                        └───────────────┬────────────────┘
                                        │
                                        ▼
                              ┌───────────────────┐
                              │ KvCacheStored     │
                              │ BlockData         │
                              │ {block_hash,      │
                              │  tokens_hash}     │
                              └───────────────────┘
```

**为什么独立计算 XXH3？**

- 引擎的 `BlockHash` 是链式滚动哈希（`H_i = hash(H_{i-1}, tokens_i)`），编码了整个前缀
- Conductor 的 `LocalBlockHash` 是独立 XXH3（`H_i = XXH3(1337, tokens_i)`），仅编码本块内容
- 独立哈希使同一父节点下、相同 token 内容稳定映射到同一 child key，多个 Worker 可共享该节点
- 不同父路径仍对应不同树节点；若直接使用引擎链式哈希，节点 key 会额外绑定整个历史前缀，
  不适合作为 Conductor 的本地内容键

### 计算细节

```text
  fn compute_block_hash_for_seq(token_ids: &[i64], block_size: u32)
      -> Vec<LocalBlockHash>

  Input:  token_ids = [t0, t1, ..., tn]
          block_size = B

  Output: [XXH3(1337, [t0 .. t_B-1]),
           XXH3(1337, [t_B .. t_2B-1]),
           ...]

  Implementation:
    - Cast each block's tokens i64 -> u32 (engine wire width), then XXH3 over LE bytes
    - little-endian: from_raw_parts(&[u32]) as &[u8] (not whole i64 zero-copy)
    - big-endian: write to_le_bytes per token
    - Trailing partial block still counts (tokens.len().div_ceil(block_size))
    - >2048 blocks: rayon parallel, batches of 1024
```

---

## 匹配逻辑与查询流程

### find_matches_by_hash 完整流程

```text
  Input: [LocalBlockHash; N]   (query sequence)

  ┌─ Phase 1: HBM Prefix Match ──────────────────────────────┐
  │  tree.find_matches_detailed(hashes)                      │
  │                                                          │
  │  root -> H0 -> H1 -> H2 -> (H3 missing)                  │
  │                                                          │
  │  Result: {                                               │
  │    W1: PrefixMatch { depth: 3, last_seq_hash: seq300 }   │
  │    W2: PrefixMatch { depth: 1, last_seq_hash: seq100 }   │
  │  }                                                       │
  │  -> overlap.npu_blocks[worker] = depth                   │
  │  -> collect TierBreakpoint {instance, dp_rank,           │
  │       end_pos: depth, last_seq: last_seq_hash}           │
  └──────────────────────────────────────────────────────────┘
                           │
                           ▼
  ┌─ Phase 2: CPU Continuation ──────────────────────────────┐
  │  lower_tier_lookup(hashes, hbm_breaks, cpu_tiers)        │
  │                                                          │
  │  Continuation sources:                                   │
  │    a) breakpoint resume: edge(seq300, H3) -> ...         │
  │       (only when end_pos < N)                            │
  │    b) root walk: always (report first-block              │
  │       replicas; longer replicas not masked by            │
  │       shorter upstream hits)                             │
  │                                                          │
  │  query_contiguous_hits: per worker, walk each            │
  │    candidate chain (root + breakpoints); stop at         │
  │    first missing edge; keep farthest absolute end        │
  │  -> overlap.cpu_blocks[worker] = winning length          │
  │    (root win = full span, may overlap NPU; not           │
  │     "tail continuation only")                            │
  └──────────────────────────────────────────────────────────┘
                           │
                           ▼
  ┌─ Phase 3: Disk Continuation ─────────────────────────────┐
  │  disk_breaks = merge_tier_breakpoints(hbm_breaks,        │
  │                                      cpu_breaks)         │
  │  # per (instance, dp_rank): keep farther end_pos;        │
  │  # prefer CPU on tie -> resume from max(HBM, CPU)        │
  │                                                          │
  │  lower_tier_lookup(hashes, disk_breaks, disk_tiers)      │
  │  -> overlap.disk_blocks[worker] = winning length         │
  └──────────────────────────────────────────────────────────┘
                           │
                           ▼
  ┌─ Phase 4: Aggregate ─────────────────────────────────────┐
  │  build_response:                                         │
  │    per (instance, dp_rank):                              │
  │      collect npu_end / cpu_end / disk_end                │
  │      exclusive *_blocks (NPU > CPU > Disk)               │
  │      matched_tokens = (npu + cpu + disk) × block_size    │
  │    longest_matched = max(matched_tokens) across          │
  │      DP ranks                                            │
  │  (tier weights applied later by Coordinator affinity)    │
  └──────────────────────────────────────────────────────────┘
```

### 为什么用 "断点续查" 而不是每层独立从 root 走？

| | HBM RadixTree | CPU/Disk Continuation |
|---|---|---|
| 匹配方式 | root 出发，树遍历 | 边遍历：断点续接链 + root 链并列候选 |
| 缺失处理 | 第一个缺失即停 | 第一个缺失边/缺失 Worker 即停 |
| root 走查条件 | 总是 | **无条件**（拥有首边即走；与续接链取绝对终点最远者） |
| 保证 | 匹配的块形成合法前缀链 | 与 HBM 衔接的续接链 + 本层更长副本均可如实报告 |

断点续查保证三层命中块是**同一条连续前缀**，与 vLLM prefix cache 的查找语义
（NPU → CPU → Disk 依次续接）一致。root 链无条件并行走查是为了发现下层更长根副本
或仅下层命中；响应侧再按绝对终点做互斥切分，同前缀副本不会重复计入 `*_blocks` /
加权 `matched_tokens`——这正是弃用旧 `skip_root` 规则的原因（旧规则只会制造低估）。

### 为什么是连续匹配而不是平铺索引？

如果不做连续匹配而只用平铺 HashMap（更早实现），会出现"block 0, 1, 3, 4 命中，
block 2 缺失"的虚高计数。`LowerTierIndexer` 的边图和 HBM 树遍历共同保证了连续匹配。

---

## 事件接入

### 双协议支持

```text
  ┌──────────────────────────────────────────────────────┐
  │                  KvEventWirePayload                  │
  │                     .normalize()                     │
  └─────────────┬───────────────────────────┬────────────┘
                │                           │
                ▼                           ▼
      ┌───────────────────┐    ┌────────────────────────┐
      │ Engine format     │    │ RFC #1527 format       │
      │ (vLLM msgspec)    │    │ (Mooncake pool)        │
      │                   │    │                        │
      │ {type: "stored",  │    │ {event_type: "stored", │
      │  blocks: [...],   │    │  seq_hashes: [...],    │
      │  parent_hash,     │    │  medium: "cpu",        │
      │  token_ids,       │    │  backend_id: "..."}    │
      │  block_size}      │    │                        │
      └─────────┬─────────┘    └────────────┬───────────┘
                │                           │
                └─────────────┬─────────────┘
                              ▼
                   ┌─────────────────────┐
                   │   KvCacheEventData  │
                   │ (canonical internal)│
                   │                     │
                   │  Stored / Removed / │
                   │       Cleared       │
                   └─────────────────────┘
```

### 两阶段 Offload 匹配

CPU/Disk 事件分两阶段到达：引擎先上报 offload 事件（含 `token_ids`，可算 `tokens_hash`），
pool daemon 后上报 store 事件（含 `seq_hashes`，即 `block_hash`）。核心数据结构：

```rust
pub struct OffloadPoolState {
    // Phase 1 先到: block_hash → (tokens_hash, parent_hash, inserted_at)
    // 等待首次 pool 确认；默认 TTL 600s，无容量上限
    offload: FxHashMap<u64, OffloadCacheEntry>,
    // 首次确认后始终写入（无需配置）：供后续 Disk 复用
    // TTL 300s（CONTENT_TTL）清扫；跨 tier 移除存活，为 CPU→Disk 迁移窗口兜底
    content: FxHashMap<u64, ContentEntry>,   // BlockContent + content inserted_at
    // Phase 2 先到: block_hash → {waiting workers}，TTL 60s（PENDING_TTL）
    pending_pool: FxHashMap<u64, FxHashSet<PendingPoolEvent>>,
}
// 不变量: 一个 block_hash 不同时存在于 offload 与 content
```

- **Phase 1 先到**：引擎 offload 事件到达 → 计算 `tokens_hash` → 连同该块自己的
  `parent_hash` 缓存到 `offload` → 等待 Pool 确认。
- **Phase 2 先到**：Pool store 事件到达 → 排入 `pending_pool`（按 Worker 去重）→ 等待
  引擎 offload 事件。
- **双方到齐**：使用 block hash 关联两侧事件，取得 `tokens_hash` + `parent_hash` → 构建
  continuation edge → 插入 CPU/Disk 索引，同时将映射迁入 `content`。

**content 保留（无需配置，始终生效）**：

- 首次 pool 确认后，`(tokens_hash, parent_hash)` 始终迁入 `content`，供 Disk 晋升复用
  （block 仍在 CPU tier 上时也可直接 `cpu_tiers` 反查）。
- content **跨 tier 移除存活**（不随 CPU 驱逐清除）：即便 CPU 先驱逐、Disk store
  事件后到，仍可在 `content` 保留窗口（`CONTENT_TTL`，300s）内解析入 Disk。
- `content` 按 `CONTENT_TTL`（300s）TTL 清扫：迁移窗口关闭后自动清除，内存有界；
  条目是 tier 数据的拷贝加短暂迁移残留，无 Disk 部署的额外开销可忽略。

**为什么 offload 缓存要携带 parent_hash？**

Phase 1 的 offload 事件是一条链（引擎一次 offload 多个连续块）。首个块的 parent 是
事件携带的 `parent_block_hash`，其后每块的 parent 是链中前一个块的 `block_hash`。
携带 parent_hash 后，Phase 2 的确认事件能把每个块**从正确的父块续接**，而不是统一从
root 挂接——否则 continuation-edge 图会丢失链式关系，CPU/Disk 断点续查无法工作。

**匹配后的应用**：命中条目**逐 block** 构造 `Stored` 事件（每个 block 自带
`parent_hash`）应用，绝不批量合并成单个 `parent_hash: None` 的事件——批量会静默丢失
块间续接。

**缓存清理语义**：ingest 路径不再触发惰性全量扫描。后台 maintenance 默认每 30 秒
调用 `sweep_stale_caches()`，分别清理 `pending_pool`（默认 60s）、`content`（默认 300s）
和 `offload`（默认 600s）。三类缓存均记录插入时间；实际最长驻留时间约为对应 TTL 加一个
maintenance 周期。`offload` 没有容量上限，但不会永久驻留，同时仍会在匹配成功或引擎侧
`evict_pending_blocks` 时提前删除。Worker 注销只按 Worker 清理 `pending_pool`；共享的
`offload` / `content` 由匹配、显式移除或 TTL 维护回收。

### 后台 Maintenance

服务启动时创建独立 Tokio 周期任务，默认每 30 秒执行一次完整维护：

1. 对每个 `IndexerEntry` 清理过期的 offload/pending/content；
2. 调用 `sweep_stale_nodes()` 回收 HBM 空节点；
3. 扫描顶层 `DashMap`，删除缓存已空且没有活跃注册引用的 `(model, tenant)` Entry。

维护任务通过 `Weak<WorkerRegistry>` 持有服务，Registry 释放后任务自行退出。扫描空 Entry 时
持有注册表读锁，避免注册过程与“活跃 Entry 保护集合”之间出现竞态。匹配缓存的 maintenance
与 ingest 共用 `RwLock<OffloadPoolState>` 写锁，因此不会并发修改 Map；代价是清扫期间 ingest
可能短暂等待。

| CLI 参数 | 默认值 | 说明 |
|----------|--------|------|
| `--maintenance-interval-secs` | `30` | 后台维护周期；实现至少按 1 秒执行 |
| `--pending-ttl-secs` | `60` | Pool-first 等待项 TTL |
| `--content-ttl-secs` | `300` | CPU→Disk promotion 映射保留 TTL |
| `--offload-ttl-secs` | `600` | 未确认 engine offload TTL；无容量上限 |

### vLLM 事件过滤

vLLM msgspec 事件按 attention group 过滤（`is_main_attention_kind`，deny-list）：

- **保留**：`FullAttention` / `MlaAttention` / `SinkFullAttention`（无 `kv_cache_spec_kind`
  的旧版本事件也保留，向后兼容）
- **丢弃**：`SlidingWindow` / `SlidingWindowMla` / `Mamba` / `ChunkedLocalAttention` /
  `EncoderOnlyAttention` / `CrossAttention`（与 Dynamo kv-router 一致）
- **未知 kind**：前向兼容，默认**保留**（`_ => true`），避免未来引擎新增主注意力类型被静默丢弃

匹配忽略大小写与下划线（`MlaAttention` 与 `mla_attention` 等价）。此外，`BlockStored`
事件携带的 `block_size` 与注册值不一致时丢弃，防止污染索引。

### ZMQ Wire Format

事件通过 ZMQ PUB 以 3 段消息送达：`[topic][seq: u64 BE][msgpack payload]`。

ZMQ 路径（`zmq_subscriber::process_payload`）依次尝试 **2** 种 payload：

1. **vLLM msgspec batch**（`parse_vllm_batch`）：
   - Format A：`[ts, events, dp_rank]`
   - Format B：`[ts, dp_rank, events]`
   - 单条事件为 array_like：`[tag, block_hashes, parent_hash?, token_ids, block_size, medium, ...]`
2. **Pool backend batch**：`(timestamp_ms, [PoolEvent...], dp_rank)`（Mooncake/Memcache 等）

二者均失败则记 parse error。事件中缺失的 `model_name` / `block_size` / `dp_rank` /
`medium` 使用注册时的默认值补齐。

### HTTP `/events` Wire Format

Coordinator / 引擎也可经 `POST /events` 推送 JSON（`KvEventBatch` / `KvEventWirePayload`）：
支持引擎 map 形态、RFC #1527 pool 形态，以及 `type` / `event_type` / legacy `BlockStored`
等字段别名（见 `protocols.rs`）。该路径与 ZMQ msgpack 解析相互独立，勿与上节混淆。

---

## 错误处理

| 错误 | HTTP 状态码 | 场景 |
|------|-----------|------|
| `InstanceNotFound` | 404 | 对未注册实例执行操作 |
| `NoIndexer` | 404 | 查询时不存在对应的 (model, tenant) IndexerEntry |
| `NoWorkers` | 200 `{tenant_id: {}}` | 无缓存命中——正常，不视为错误 |
| `ParentBlockNotFound` | 500 | Store 事件引用了未知 parent hash |
| `InvalidBlockSequence` | 500 | 检测到自引用 block |

---

## 相关文件索引

| 文件 | 说明 |
|------|------|
| `motor/kv_conductor/src/indexer/mod.rs` | 顶层索引、两阶段缓存和 maintenance |
| `motor/kv_conductor/src/concurrent_tree.rs` | HBM 并发 Radix Tree |
| `motor/kv_conductor/src/lower_tier.rs` | CPU/Disk continuation-edge 索引 |
| `motor/kv_conductor/src/registry.rs` | Worker 注册生命周期与 subscriber 管理 |
| `motor/kv_conductor/src/main.rs` | CLI、后台 maintenance 和 HTTP 服务启动 |
| `motor/kv_conductor/__init__.py` | Python 包入口，`is_available()`, `start()` |
| `motor/kv_conductor/__main__.py` | `python -m motor.kv_conductor` 入口 |
| `build.sh` | 条件编译，`KV_CONDUCTOR_PREBUILT` 支持预构建二进制 |
| `setup.py` | 条件 `package_data`，按需打包二进制到 wheel |
| `docs/zh/user_guide/features/kvcache_affinity.md` | 用户部署文档 |
| `motor/kv_conductor/README.md` | 功能简介 |
