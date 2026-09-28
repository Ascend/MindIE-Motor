# KV Conductor

基于 Rust 的 KV Cache 索引服务。订阅引擎 KV 事件，维护前缀树索引，为 Coordinator 提供
缓存感知的请求路由。Coordinator 对缓存命中按介质加权，结合负载及命中率门槛选点。
Conductor 负责索引查询，实际 KV 复用和数据传输由引擎及存储后端完成。已集成在 motor Python 包内。

## 快速开始

kv-conductor 以**可选组件**的形式随 motor wheel 发布。完整的构建和启动流程：

### 1. 编译二进制

```bash
cd motor/kv_conductor && cargo build --release
# 二进制产出：target/release/kv-conductor
```

`Cargo.lock` 由构建生成，当前未纳入版本管理。需要固定依赖版本时，CI 应保存并复用锁文件，
再使用 `--locked` 构建。流水线加速可缓存：

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

- `KV_CONDUCTOR_PREBUILT=/path/to/binary` → 使用指定的预构建二进制
- `bin/kv-conductor` 已存在且未设 `SKIP_KV_CONDUCTOR_BUILD=0` → 跳过 cargo，直接打包
- `SKIP_KV_CONDUCTOR_BUILD=1` → 跳过 cargo（无 bin 则省略该 crate）
- 缺二进制、有 `cargo` 且能探测到 libzmq（`pkg-config --exists libzmq` 或 `zmq.h`）→ `cargo build --release` 并复制到 `bin/`
- 有 `cargo` 但缺 libzmq → **WARNING 后自动跳过**（不因此让整个 `build.sh` 失败）
- 需要编译而没有可用 cargo → 先尝试准备 Rust 工具链；缺必需的 `libmindie_workload_shm.so` 且无法构建时，整个 wheel 构建失败。离线需同时准备该库和 conductor 二进制，不能把缺 cargo 一概当成可选组件跳过

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

缓存维护参数（均有默认值）：

```bash
kv-conductor \
  --maintenance-interval-secs 30 \
  --pending-ttl-secs 60 \
  --content-ttl-secs 300 \
  --offload-ttl-secs 600
```

后台维护会定期清理过期的 offload/pending/content 匹配缓存、HBM 空节点和无注册引用的空索引。
TTL 清理由后台周期任务完成，ingest 路径不再执行惰性全量扫描；因此即使事件流量停止，
过期数据也会被回收。实际最长驻留时间约为对应 TTL 加一个 maintenance 周期。

Python 入口依次查找包内 `motor/kv_conductor/bin/kv-conductor`、源码
`target/release/kv-conductor` 和 `PATH`，不要求固定安装到 `/usr/local/bin`。
启动脚本 `kv_conductor.sh` 使用 `exec python -m motor.kv_conductor`。

## 功能

KV Conductor 维护三层存储介质的 KV Cache 索引，按绝对覆盖终点做互斥切分后
返回各介质 `*_blocks` 与未加权覆盖长度 `matched_tokens`；介质亲和权重由
Coordinator 调度器在 `scheduler_config.kv_affinity.w_npu/w_cpu/w_disk` 中配置并应用。
旧的平铺 `kv_affinity_*` 参数仅作兼容，新配置使用嵌套结构。

### HBM（NPU）— 统一模型

引擎 Worker 通过 ZMQ PUB 或 HTTP 将 KV 事件发给 conductor。
ZMQ 模式下事件端点由引擎 Worker 绑定，conductor 作为 SUB **主动 connect 到引擎**
（连接方向 conductor → 引擎，事件数据流引擎 → conductor）；HTTP 模式下引擎 Worker
将 KV 事件 POST 到 conductor 的 `/events` 接口。
所有后端（Mooncake / Memcache / YuanRong）的 HBM 事件链路一致：

```text
Engine Worker                        KV Conductor
(vLLM/SGLang)
      │                                │
      │  ZMQ PUB（引擎绑定，conductor   │
      │  SUB 主动 connect）/ HTTP POST │
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
  - CPU：从 HBM 断点续查（仅同一 `(instance_id, dp_rank)`）；root 链（首块副本）无条件走——更长副本不会被上游较短命中掩盖
  - Disk：从 `max(HBM, CPU)` 断点续查（同样按 `(instance_id, dp_rank)` 对齐；CPU 更长时优先接 CPU）；root 链同 CPU 层无条件走
- **连续匹配**：走到第一个缺失边即停；同一 worker 多条候选链（root + 断点）取绝对终点最远者
- **匹配缓存保留**：pool 确认后将 `(tokens_hash, parent_hash)` 保留在 `content`，默认 TTL 300 秒，跨 tier 移除存活，供后续 Disk store 使用。未确认的 `offload` 默认 TTL 600 秒，`pending_pool` 默认 TTL 60 秒；后台每 30 秒清扫，匹配成功或显式移除也会提前清理。这些 TTL 约束临时匹配状态，不是引擎 KV 缓存寿命，也不构成固定内存容量上限

各后端的 CPU/Disk 适配差异：

| 后端 | Pool 模型 | Worker 识别 |
|------|----------|-------------|
| Mooncake | 中心化 master，一个 ZMQ PUB | IP 匹配 → 节点上所有 DP |
| Memcache | 中心化 master，一个 ZMQ PUB | 同 Mooncake |
| YuanRong | 每节点 CPU/Disk PUB + 每 DP NPU | NPU：精确 DP；CPU/Disk：IP 匹配 → 节点上所有 DP |

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
      │            "matched_tokens": 640,      │  <- exclusive sum × block_size
      │            "npu_blocks": 3,            │  <- exclusive NPU blocks
      │            "cpu_blocks": 2,            │  <- exclusive CPU beyond NPU
      │            "disk_blocks": 0            │  <- exclusive Disk beyond max(NPU,CPU)
      │          }                             │
      │        }                               │
      │      }                                 │
      │    }                                   │
      │  }                                     │
      │<───────────────────────────────────────│
```

字段计算（每 DP / rank）：

1. 收集各介质绝对覆盖终点 `npu_end` / `cpu_end` / `disk_end`
2. 互斥切分（优先级 NPU > CPU > Disk）：
   - `npu_blocks = npu_end`
   - `cpu_blocks = max(0, cpu_end - npu_end)`
   - `disk_blocks = max(0, disk_end - max(npu_end, cpu_end))`
3. `matched_tokens = (npu + cpu + disk) × block_size`（未加权覆盖）
4. `longest_matched = max(各 DP matched_tokens)`

| 字段 | 含义 |
|------|------|
| `npu_blocks` / `cpu_blocks` / `disk_blocks` | 该 DP 互斥真实命中块数（同前缀副本只归最高优先级介质） |
| `matched_tokens` | 互斥块数之和 × `block_size`（真实覆盖长度） |
| `longest_matched` | 该实例所有 DP 的 `matched_tokens` 最大值 |

调度器读取 `DP[<dp_rank>]` 的 `*_blocks`，按 `scheduler_config.kv_affinity`
中的 `w_npu/w_cpu/w_disk`（默认 `1.0/1.0/0.0`）加权后再算亲和分（见亲和性调度文档）。

## 启动参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` / `-p` | `13333` | HTTP 服务端口 |
| `--host` | `::` | 绑定地址（默认双栈） |
| `--maintenance-interval-secs` | `30` | 后台维护周期，至少按 1 秒执行 |
| `--pending-ttl-secs` | `60` | Pool-first 等待项 TTL |
| `--content-ttl-secs` | `300` | 跨介质迁移映射保留 TTL |
| `--offload-ttl-secs` | `600` | 未确认 engine offload TTL |

`python -m motor.kv_conductor` 将参数透传给二进制；介质评分权重配置在 Coordinator，非本组件 CLI。

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
      "prefill_scheduler_type": "kv_cache_affinity",
      "decode_scheduler_type": "load_balance"
    }
  },
  "kv_conductor_config": {
    "block_size": 128,
    "npu_endpoint": "tcp://*:5557",
    "http_server_port": 13333
  }
}
```

以上为已有部署的增量，还需在 P/U 引擎启用 `kv-events-config`。
`npu_endpoint` 用于覆盖自动从引擎配置推导的端口；应与引擎 `--kv-events-config` 的 `endpoint` 一致；
模式中的 `*` 会被替换为 endpoint IP，端口会加上 `dp_rank`，conductor 主动 connect 到各引擎节点绑定的事件端口。
注册时写入 conductor 的 `medium_endpoints` key 为 `"npu"`。

YuanRong CPU/Disk 按 `(节点 IP, model_name)` 注册，使用 `cpu_endpoint` / `disk_endpoint` 基础端口，
不加 DP 秩；这些 pool 事件按 IP 关联节点内各 DP。周期重注册默认关闭，启用后会补齐缺失的 HBM DP
和 YuanRong 节点池；Mooncake/Memcache 中心 pool 不在当前周期补注册分支中。
回放只在首次实例注册携带回放地址时启动，查询不会等待回放完成，不能以注册成功代替索引恢复验收。

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
