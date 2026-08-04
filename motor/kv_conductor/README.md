# KV Conductor

基于 Rust 的 KV Cache 索引服务。订阅引擎 KV 事件，维护前缀树索引，为 Coordinator 提供
缓存感知的请求路由——将请求导向已缓存最长 token 前缀的 Worker。已集成在 motor Python 包内。

## 快速开始

kv-conductor 以**可选组件**的形式随 motor wheel 发布。完整的构建和启动流程：

### 1. 编译二进制

```bash
cd motor/kv_conductor && cargo build --release
# 二进制产出：target/release/kv-conductor
```

仓库提交了 `Cargo.lock`；本地/CI 建议使用 `--locked` 以固定依赖版本。流水线加速请缓存：

- `~/.cargo/registry`、`~/.cargo/git`
- `motor/kv_conductor/target`

缓存 key 建议：`{os}-{rustc版本}-{Cargo.lock hash}`。

如果已有预编译的二进制，可跳过此步，后续 `build.sh` 会自动发现并打包。

### 2. 构建 motor wheel

```bash
# 在项目根目录执行
bash build.sh
```

`build.sh` 会自动检测 kv-conductor 二进制：

- `target/release/kv-conductor` 已存在 → 直接复制到 `bin/`，打包进 wheel
- 不存在但有 `cargo` → 自动编译
- 设置了 `KV_CONDUCTOR_PREBUILT=/path/to/binary` → 使用指定的预构建二进制
- 都没有 → 跳过，wheel 不含 kv-conductor（其他功能不受影响）

产物：`dist/motor-*.whl`

### 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `RUST_LOG` | `info` | 日志级别 |

### 3. 安装 wheel

```bash
pip install dist/motor-*.whl
```

安装后 `python -m motor.kv_conductor` 即可使用。验证：

```bash
python -c "from motor.kv_conductor import is_available; print(is_available())"
# True → kv-conductor 可用
```

### 4. 启动

```bash
python -m motor.kv_conductor --port 13333

# 或直接运行二进制
./motor/kv_conductor/target/release/kv-conductor --port 13333
```

> **注意**：容器部署时，镜像内二进制路径为 `/usr/local/bin/kv-conductor`，
> 启动脚本 `kv_conductor.sh` 通过 `exec python -m motor.kv_conductor` 启动。

## 功能

KV Conductor 维护三层存储介质的 KV Cache 索引，每层独立追踪连续匹配的 block 数。
查询结果返回**各介质 block 计数**与换算后的 `matched_tokens`；亲和性评分由
Coordinator 调度器基于 `matched_tokens` 完成。

### HBM（NPU）— 统一模型

引擎 Worker 通过 ZMQ PUB 或 HTTP 将 KV 事件**直接推送给** conductor。
所有后端（Mooncake / Memcache / YuanRong）的 HBM 事件链路一致：

```text
Engine Worker                        KV Conductor
(vLLM/SGLang)
      │                                │
      │  ZMQ PUB / HTTP POST           │
      │  {type: "stored",              │
      │   token_ids, block_hashes,     │
      │   parent_hash, medium: "npu"}  │
      │───────────────────────────────>│
      │                                ├─ XXH3(token_ids) -> LocalBlockHash
      │                                ├─ RadixTree.apply_store()
      │                                │    └─ build prefix chain via parent_hash
      │                                └─ query: tree walk -> longest contiguous prefix
```

- **索引结构**：`ConcurrentRadixTree`，按 token 内容哈希（XXH3）建前缀链
- **匹配语义**：最长连续前缀——从 root 走到第一个缺失即停
- **事件源**：Worker 自行上报，无需中心化 Pool
- **介质 key**：注册与事件中使用 `"npu"`（兼容旧值 `"gpu"` / `"xpu"`）

### CPU / DISK — 可选，后端相关

当启用 KV Cache 池化（Mooncake / Memcache / YuanRong）且配置了 CPU/DISK 副本时，
conductor 通过**两阶段匹配**索引二级缓存：

```text
Engine Worker           Pool Master               KV Conductor
      │                      │                      │
      │  [Phase 1]           │                      │
      │  offload event       │                      │
      │  {token_ids,         │                      │
      │   block_hashes,      │                      │
      │   parent_hash}       │                      │
      │──────────────────────┼─────────────────────>│
      │                      │                      │  cache hash(token_ids)
      │                      │                      │  (wait for pool confirm)
      │                      │                      │
      │                      │  [Phase 2]           │
      │                      │  pool store event    │
      │                      │  {seq_hashes,        │
      │                      │   medium: "cpu"}     │
      │                      │─────────────────────>│
      │                      │                      │  match -> insert CPU index
      │                      │                      │  keep content (TTL 300s)
      │                      │                      │
      │                      │  [Disk promote]      │
      │                      │  (optional)          │
      │                      │  {seq_hashes,        │
      │                      │   medium: "disk"}    │
      │                      │─────────────────────>│
      │                      │                      │  lookup CPU tier (or kept
      │                      │                      │   content; survives cross-
      │                      │                      │   tier remove) -> Disk index
```

- **索引结构**：`LowerTierIndexer`，按 `(parent_seq_hash, tokens_hash)` 记录 continuation edge
- **匹配语义**：
  - CPU：从 HBM 断点续查；root 链（首块副本）无条件走——更长副本不会被上游较短命中掩盖
  - Disk：从 `max(HBM, CPU)` 断点续查（CPU 更长时优先接 CPU）；root 链同 CPU 层无条件走
- **连续匹配**：走到第一个缺失边即停；同一 worker 多条候选链（root + 断点）取绝对终点最远者
- **content 保留**：pool 确认后始终保留 `(tokens_hash, parent_hash)`（无需配置），跨 tier 移除存活，CPU 已驱逐后、保留窗口（300s TTL）内仍可解析 Disk store；窗口关闭自动清除，内存有界（条目为 tier 数据拷贝 + 短暂迁移残留）。未确认的 offload **无 TTL、无硬容量上限**，随未确认块增长，仅在匹配成功或引擎驱逐时清除

各后端的 CPU/Disk 适配差异：

| 后端 | Pool 模型 | Worker 识别 |
|------|----------|-------------|
| Mooncake | 中心化 master，一个 ZMQ PUB | IP 匹配 → 节点上所有 DP |
| Memcache | 中心化 master，一个 ZMQ PUB | 同 Mooncake |
| YuanRong | 每节点多端口 ZMQ PUB | Port 匹配 → 精确 DP |

### 查询

Coordinator 发起查询，conductor 汇总三层介质的**连续匹配 block 数**：

```text
Coordinator                                  KV Conductor
      │                                        │
      │  POST /query                           │
      │  {model, block_size,                   │
      │   token_ids, tenant_id?}               │
      │───────────────────────────────────────>│
      │                                        │
      │  200 {                                 │
      │    "default": {                        │  <- tenant_id (default "default")
      │      "inst-1": {                       │
      │        "longest_matched": 640,         │  <- max matched_tokens across DPs
      │        "DP": {                         │
      │          "0": {                        │
      │            "matched_tokens": 640,      │  <- coverage_end * block_size
      │            "npu_blocks": 3,            │  <- HBM contiguous hit length
      │            "cpu_blocks": 2,            │  <- CPU winning segment length
      │            "disk_blocks": 0            │  <- Disk winning segment length
      │          }                             │
      │        }                               │
      │      }                                 │
      │    }                                   │
      │  }                                     │
      │<───────────────────────────────────────│
```

字段计算：

| 字段 | 含义 |
|------|------|
| `npu_blocks` / `cpu_blocks` / `disk_blocks` | 该 DP 在各介质上的最长连续匹配 block 数（段长；各层如实报告，层间可重叠——副本不会被上游较短命中掩盖） |
| `matched_tokens` | 各介质绝对覆盖终点的最大值 × `block_size`（永不等于段长之和；同前缀多副本不重复计，≤ 输入长度） |
| `longest_matched` | 该实例所有 DP 的 `matched_tokens` 最大值 |

调度器实际只消费 `DP[<dp_rank>].matched_tokens`（见亲和性调度文档）。

## 启动参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` / `-p` | `13333` | HTTP 服务端口 |
| `--host` | `::` | 绑定地址（默认双栈） |

## API

| 端点 | 方法 | 用途 |
|------|------|------|
| `/register` | POST | 注册 Worker（`medium_endpoints`: `npu` / `cpu` / `disk`） |
| `/unregister` | POST | 注销 Worker |
| `/query` | POST | 按 token_ids 查询各 Worker 命中 block 数 |
| `/query_by_hash` | POST | 使用预计算 hash 查询 |
| `/events` | POST | 接入 KV 事件 |
| `/health` | GET | 存活检查 |
| `/workers` | GET | 已注册 Worker 列表 |

详细 API 契约见 [设计文档](../../docs/zh/design/kv_conductor.md)。

## Motor 集成

kv-conductor 已随 motor wheel 打包。部署脚本 `kv_conductor.sh` 通过以下命令启动：

```bash
exec python -m motor.kv_conductor --host "$KV_CONDUCTOR_HOST" --port "$KV_CONDUCTOR_PORT"
```

Coordinator 通过 `ConductorApiClient` 与 conductor 通信。`user_config.json` 典型配置：

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "scheduler_type": "kv_cache_affinity"
    }
  },
  "kv_conductor_config": {
    "block_size": 128,
    "npu_endpoint": "tcp://*:50090",
    "http_server_port": 13333
  }
}
```

`npu_endpoint` 模式中的 `*` 会被替换为 endpoint IP，端口会加上 `dp_rank`。
注册时写入 conductor 的 `medium_endpoints` key 为 `"npu"`。

详见 [KV Cache 亲和性调度文档](../../docs/zh/user_guide/features/kvcache_affinity.md)。

## 详细设计

架构细节、多介质适配、哈希与匹配算法见 [设计文档](../../docs/zh/design/kv_conductor.md)。

## 许可证与第三方声明

本组件主体采用 **Mulan PSL v2**。

以下文件（或部分）为 NVIDIA Dynamo kv-router 的 **Apache-2.0 衍生作品 / 策略对齐**，
已保留 NVIDIA 版权与 SPDX / 归因声明；分发时须同时提供 Apache-2.0 许可证文本：

| 本地文件 | 上游路径 |
|----------|----------|
| `src/lower_tier.rs` | `lib/kv-router/src/indexer/lower_tier.rs` |
| `src/concurrent_tree.rs` | `lib/kv-router/src/indexer/concurrent_radix_tree.rs` |
| `src/hashing.rs` | `lib/kv-router/src/protocols.rs`（XXH3 哈希） |
| `src/protocols.rs`（部分） | `lib/kv-router/src/protocols.rs` |
| `src/events/vllm.rs`（attention 过滤策略） | `lib/kv-router/src/zmq_wire/filter.rs` |

详见：

- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- [licenses/Apache-2.0.txt](licenses/Apache-2.0.txt)
