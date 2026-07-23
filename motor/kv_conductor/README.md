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

KV Conductor 维护三层存储介质的 KV Cache 索引，每层独立追踪 block 归属：

### HBM（XPU）— 统一模型

引擎 Worker 通过 ZMQ PUB 或 HTTP 将 KV 事件**直接推送给** conductor。
所有后端（Mooncake / Memcache / YuanRong）的 HBM 事件链路一致：

```text
  Engine Worker                    KV Conductor
  (vLLM/SGLang)
      │                                │
      │  ZMQ PUB / HTTP POST           │
      │  {type: "stored",              │
      │   token_ids, block_hashes,     │
      │   parent_hash, medium: "xpu"}  │
      │───────────────────────────────►│
      │                                ├─ XXH3(token_ids) → LocalBlockHash
      │                                ├─ RadixTree.apply_store()
      │                                │    └─ 按 parent_hash 建前缀链
      │                                └─ 查询时：树遍历 → 最长连续前缀
```

- **索引结构**：`ConcurrentRadixTree`，按 token 内容哈希（XXH3）建前缀链
- **匹配语义**：最长连续前缀——从 root 走到第一个缺失即停
- **权重**：每匹配一个 block = 3 分
- **事件源**：Worker 自行上报，无需中心化 Pool

### CPU / DISK — 可选，后端相关

当启用 KV Cache 池化（Mooncake / Memcache / YuanRong）且配置了 CPU/DISK 副本时，
conductor 通过**两阶段匹配**索引二级缓存：

```text
  Engine Worker          Pool Master           KV Conductor
      │                      │                      │
      │  [Phase 1]           │                      │
      │  offload event       │                      │
      │  {token_ids,         │                      │
      │   block_hashes,      │                      │
      │   parent_hash}       │                      │
      │────────────────────────────────────────────►│
      │                      │                      │  缓存 token_ids 的 hash
      │                      │                      │  (等待 pool 确认)
      │                      │                      │
      │                      │  [Phase 2]           │
      │                      │  pool store event    │
      │                      │  {seq_hashes,        │
      │                      │   medium: "cpu"}     │
      │                      │─────────────────────►│
      │                      │                      │  匹配：组装 tokens_hash
      │                      │                      │  插入 CPU/Disk 索引
```

- **索引结构**：`LowerTierIndexer`，按 `(parent_seq_hash, tokens_hash)` 记录 continuation edge
- **匹配语义**：从 HBM 断点续查，连续匹配到第一个缺失
- **权重**：CPU 每 block = 2 分，Disk 每 block = 1 分

各后端的 CPU/Disk 适配差异：

| 后端 | Pool 模型 | Worker 识别 |
|------|----------|-------------|
| Mooncake | 中心化 master，一个 ZMQ PUB | IP 匹配 → 节点上所有 DP |
| Memcache | 中心化 master，一个 ZMQ PUB | 同 Mooncake |
| YuanRong | 每节点多端口 ZMQ PUB | Port 匹配 → 精确 DP |

### 查询与评分

Coordinator 发起查询，conductor 综合三层介质计算每个 DP 的加权分数：

```text
  Coordinator                      KV Conductor
      │                                │
      │  POST /query                   │
      │  {model, block_size,           │
      │   token_ids}                   │
      │───────────────────────────────►│
      │                                │
      │  200 {                         │
      │    "inst-1": {                 │
      │      "longest_matched": 384,   │  ← 最长可用前缀 (tokens)
      │      "DP": {                   │
      │        "0": {                  │
      │          "XPU": 9,             │  ← HBM: 3 blocks × 3
      │          "CPU": 4,             │  ← CPU: 2 blocks × 2
      │          "DISK": 0,            │
      │          "total": 13           │  ← XPU + CPU + DISK
      │        }                       │
      │      }                         │
      │    }                           │
      │  }                             │
      │◄───────────────────────────────│
```

## 启动参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port` | `13333` | HTTP 服务端口 |
| `--host` | `::` | 绑定地址 |
| `--hbm-weight` | `3` | HBM block 权重 |
| `--cpu-weight` | `2` | CPU block 权重 |
| `--disk-weight` | `1` | Disk block 权重 |

## API

| 端点 | 方法 | 用途 |
|------|------|------|
| `/register` | POST | 注册 Worker |
| `/unregister` | POST | 注销 Worker |
| `/query` | POST | 查询 KV Cache 命中分数 |
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

Coordinator 通过 `ConductorApiClient` 与 conductor 通信，调度器配置：

```json
{
  "scheduler_type": "kv_cache_affinity",
  "kv_conductor_config": {
    "block_size": 128,
    "xpu_endpoint": "tcp://*:50090",
    "http_server_port": 13333
  }
}
```

详见 [KV Cache 亲和性调度文档](../../docs/zh/user_guide/features/kvcache_affinity.md)。

## 详细设计

架构细节、多介质适配、哈希与匹配算法见 [设计文档](../../docs/zh/design/kv_conductor.md)。
