# KV Conductor E2E Mock 测试

使用 Mock ZMQ Publisher 对 kv-conductor 进行端到端测试，验证
ZMQ 订阅 → 事件注入 → 前缀树存储 → KV 缓存查询的完整链路。

所有操作通过 `conductor_cli.sh` 一键完成。

## 快速开始

### 单端口模式（N 个 Publisher，每个一端口）

```bash
cd kv-conductor

# 1. 构建镜像（kv-conductor + zmq-publisher）
./mock/conductor_cli.sh build

# 2. 部署（8 个 Publisher dp=0~7 + KV Conductor）
./mock/conductor_cli.sh up

# 3. port-forward
kubectl -n mindie-motor port-forward deploy/mindie-motor-kv-conductor 13333:13333 &

# 4. 注册 + 查看状态
./mock/conductor_cli.sh quick

# 5. Benchmark（常见 token ID，模拟真实场景）
./mock/conductor_cli.sh bench --count 100 --tokens 1024
```

### 多端口模式（一个 Publisher，双端口，多介质）

```bash
cd kv-conductor

# 1. 构建镜像（同上，复用同一镜像）
./mock/conductor_cli.sh build

# 2. 部署（多端口 publisher + KV Conductor）
./mock/conductor_cli.sh up-multi

# 3. port-forward
kubectl -n mindie-motor port-forward deploy/mindie-motor-kv-conductor 13333:13333 &

# 4. 健康检查 → 注册 → 查看状态
./mock/conductor_cli.sh quick-multi
```

### DeepSeek V4 模拟（block_size=4）

```bash
cd kv-conductor

# 1. 构建镜像
./mock/conductor_cli.sh build

# 2. 部署 + 注册，全部用 block_size=4
./mock/conductor_cli.sh up-multi --block-size 4
kubectl -n mindie-motor port-forward deploy/mindie-motor-kv-conductor 13333:13333 &
./mock/conductor_cli.sh register-multi --block-size 4

# 3. 压测（202400 tokens → 50600 blocks）
./mock/conductor_cli.sh bench --count 16 --tokens 202400 --block-size 4
```

> ``--block-size`` 统一控制 publisher 的事件粒度和注册时的 block_size。查询时 bench 的 ``--block-size`` 也必须一致才能命中。标准模型用 128，DeepSeek V4 用 4。

多端口模式架构：

```text
zmq-publisher-multi
  ├── ZMQ PUB :15557 ──→ XPU/HBM 事件
  └── ZMQ PUB :15558 ──→ CPU (DDR) + DISK (SSD) 事件
         │                        │
         ▼                        ▼
    kv-conductor ── ZMQ SUB :15557 (订阅 XPU)
                 ── ZMQ SUB :15558 (订阅 CPU + DISK，共享同一端口)
         │
         ▼
    Radix Tree: XPU tree / CPU tree / DISK tree（三个独立前缀树）
```

注册使用 `medium_endpoints` 协议（RFC 新协议）：

```json
{
  "medium_endpoints": {
    "xpu":  "tcp://<ip>:15557",
    "cpu":  "tcp://<ip>:15558",
    "disk": "tcp://<ip>:15558"
  },
  "store_backend": "YuanRong"
}
```

cpu 和 disk 指向同一端口 — kv-conductor 自动去重，只创建一个 ZMQ SUB 连接。
该端口的事件无 `medium` 字段时，广播到 CPU 和 DISK 两棵前缀树。有 `medium` 字段时，只写入指定介质。

## 架构

### 单端口模式

```text
zmq-publisher-0  (dp=0, port 5557) ──┐
zmq-publisher-1  (dp=1, port 5558) ──┤
zmq-publisher-2  (dp=2, port 5559) ──┤
zmq-publisher-3  (dp=3, port 5560) ──┤
                                     ├── kv-conductor (HTTP :13333)
zmq-publisher-4  (dp=4, port 5561) ──┤   8 个 ZMQ SUB 连接
zmq-publisher-5  (dp=5, port 5562) ──┤   事件注入 → 前缀树
zmq-publisher-6  (dp=6, port 5563) ──┤
zmq-publisher-7  (dp=7, port 5564) ──┘
```

### 多端口模式（YuanRong 后端模拟）

```text
zmq-publisher-multi
  ├── ZMQ PUB :15557 ──→ XPU/HBM 事件
  └── ZMQ PUB :15558 ──→ CPU (DDR) + DISK (SSD) 事件
         │
         ▼
    kv-conductor (HTTP :13333)
     ├── ZMQ SUB :15557 → XPU radix tree
     └── ZMQ SUB :15558 → CPU radix tree + DISK radix tree
```

每个 Publisher 独立模拟一个引擎 Worker：

- **STORE / REMOVE / CLEAR** 事件，80%/15%/5% 比例
- **Token 分布**：60% 高频词 (100-3000) + 30% 中频 (3000-25000) + 10% 低频 (25000-50000)
- **容量增长**：每 2 分钟 +30%，模拟累积缓存
- **vLLM array 格式**：默认使用 msgspec 的 array_like 编码（`["BlockStored", hashes, ...]`），`--mooncake-format` 切换为旧 map 格式
- **多端口模式**：支持 `--multi-port` 双端口广播（XPU + CPU/DISK），使用 `medium_endpoints` 新协议注册，`cpu` 和 `disk` 共享端口时自动去重。通过 `--store-backend` 切换池化后端（Mooncake / Memcache / YuanRong）
- **加权评分**：HBM ×3, CPU ×2, Disk ×1（可通过 `--hbm-weight/--cpu-weight/--disk-weight` 在 conductor 侧配置）

## CLI 命令参考

### 环境搭建

| 命令 | 说明 |
|---|---|
| `build` | 构建所有镜像：`cargo build --release --features zmq` + docker build × 2 |
| `up` | 部署：ConfigMap + KV Conductor + N 个单端口 Publisher（默认 N=8） |
| `up-multi [--block-size N] [--initial-blocks N]` | 部署：KV Conductor + N 个多端口 Publisher。默认 block_size=128，DeepSeek V4 设 4；`--initial-blocks` 控制初始缓存块数（默认 8192）|
| `down` | 清理所有 K8s 资源 |
| `logs [filter]` | 采集日志（封装 `collect_logs.sh`） |
| `logs-save [file]` | 保存最近 30 分钟 Conductor 日志到文件（默认 `/tmp/conductor_full.log`） |
| `logs-profile [file]` | 提取并保存 profiling 日志（`hash_computed` / `find_matches` / `query profile`），默认 `/tmp/conductor_profile.log` |

控制单端口 Publisher 数量：

```bash
NUM_PUBLISHERS=4 ./mock/conductor_cli.sh up    # 只起 4 个
NUM_PUBLISHERS=16 ./mock/conductor_cli.sh up   # 起 16 个
```

block_size 与 initial_blocks 配置：

```bash
# 标准模式
./mock/conductor_cli.sh up-multi --block-size 4 --initial-blocks 8192
./mock/conductor_cli.sh register-multi --block-size 4
./mock/conductor_cli.sh bench --count 16 --tokens 202400 --block-size 4
```

`--initial-blocks` 控制每 Publisher 预填充的缓存块数（默认 8192），`--block-size` 三者必须一致。`up-multi` 发布间隔为 0.1s/批，事件积累快速。注册由 Publisher 启动后自动完成，无需手动调用 `register-multi`，除非需要重新注册。

### 运行时操作

| 命令 | 说明 |
|---|---|
| `register` | 注册全部 N 个单端口 Publisher（`type: Mooncake`） |
| `register-multi [--block-size N]` | 注册多端口 Publisher（`medium_endpoints` 协议） |
| `unregister` | 注销全部 Publisher |
| `status` | 查看 workers + blocks 计数 + per-DP 分布 |
| `query <hash...>` | 按显式 block hash 查询（`/query_by_hash`） |
| `query-tokens --count N` | 按 token ID 查询（`/query`），连续序列 [0..N-1] |
| `bench` | Benchmark（默认 realistic 模式，常见 token） |
| `bench --throughput` | 吞吐压测（100% 命中，测极限延迟） |
| `smoke` | 接口冒烟测试：遍历全部 6 个 HTTP 接口，报告 pass/fail |
| `quick` | 一键：健康检查 → 注册 → 状态（单端口模式） |
| `quick-multi` | 一键：健康检查 → 注册 → 状态（多端口模式） |
| `health` | 健康检查 |

### Benchmark

默认用常见 LLM token ID（101, 2023, 318...）发 `/query`，模拟真实请求命中率：

```bash
# 标准模式（block_size=128）
./mock/conductor_cli.sh bench --count 100 --tokens 1024

# DeepSeek V4 模拟（block_size=4，202400 tokens → 50600 blocks）
./mock/conductor_cli.sh bench --count 16 --tokens 202400 --block-size 4
```

输出示例：

```text
Benchmark: 100 queries, bs=128, mode=realistic
  [50/100] hits=0 miss=50 avg_lat=12.3ms

  Results:
    queries:         100
    hits:            0  (0%)
    misses:          100  (100%)
    latency (ms):
      p50=10.0  p90=15.2  p99=42.1  max=55.0
```

用 `--throughput` 测极限延迟（100% 命中，测纯查询吞吐）：

```bash
./mock/conductor_cli.sh bench --count 200 --tokens 512 --throughput
```

## 池化后端

Publisher 支持通过 `--store-backend` 切换三种池化后端模式：

| 后端 | 说明 | 注册协议 | 事件匹配 |
|------|------|---------|---------|
| `Mooncake` | 中心化 master 广播，`backend_id`=IP 匹配同节点所有 DP | `endpoint` (pool) + `medium_endpoints` (HBM) | IpOnly（IP→全部 DP） |
| `Memcache` | 中心化 master 广播，`backend_id`=IP + 精确 `dp_rank` | `endpoint` (pool) + `medium_endpoints` (HBM) | IpAndDpRank（IP+rank→唯一 DP） |
| `YuanRong` | 每节点多端口广播 | `medium_endpoints` (multi-port) | None（端口即 DP） |

```bash
# Mooncake: IP 匹配到同节点所有 DP
python3 zmq_publisher.py --multi-port --store-backend Mooncake ...

# Memcache: IP + dp_rank 精确匹配
python3 zmq_publisher.py --multi-port --store-backend Memcache ...

# YuanRong: 端口即 DP
python3 zmq_publisher.py --multi-port --store-backend YuanRong ...
```

## 配置

### 可配置项 (`mock-zmq-config`)

| Key | 默认值 | 说明 |
|---|---|---|
| `model` | `opt-125m` | 所有 Publisher 的模型名 |
| `block_size` | `128` | KV block 大小（tokens/block） |
| `initial_blocks` | `6` | 每 Publisher 初始缓存容量 |
| `interval` | `2.0` | 发布间隔（秒） |
| `tenant_id` | `default` | 租户 ID |
| `num_publishers` | `8` | Publisher 数量（dp_rank 0..N-1） |
| `store_backend` | `Mooncake` | 池化后端类型：`Mooncake` / `Memcache` / `YuanRong` |

### 修改配置

```bash
kubectl -n mindie-motor edit configmap mock-zmq-config
kubectl -n mindie-motor rollout restart deploy/zmq-publisher-0
```

## 日志级别

kv-conductor 日志由 `RUST_LOG` 控制（`e2e_test.yaml` 中设置，默认 `debug`）。

| RUST_LOG | register/unregister | 查询请求 | 事件注入 | 建议场景 |
|---|---|---|---|---|
| `info` | ✅ | ❌ | ✅ (关键) | 生产 / 正常运行 |
| `debug` | ✅ | ✅ | ✅ (详细) | **压测 / 调试**（默认） |
| `warn` | ❌ | ❌ | ❌ | 仅告警和错误 |
| `trace` | ✅ | ✅ | ✅ (遍历细节) | 深度排查前缀树 |

> **注意**：`info` 级别不记录查询日志（避免洪泛）。压测或调试时用 `debug`。

**临时切换：**

```bash
# 切到 debug（实时看查询日志）
kubectl -n mindie-motor set env deploy/mindie-motor-kv-conductor RUST_LOG=debug
kubectl -n mindie-motor rollout status deploy/mindie-motor-kv-conductor --timeout=20s

# 终端 A：follow 日志
./mock/conductor_cli.sh logs conductor -f

# 终端 B：跑压测
./mock/conductor_cli.sh bench --count 50

# 恢复 info
kubectl -n mindie-motor set env deploy/mindie-motor-kv-conductor RUST_LOG=info
```

```bash
./mock/conductor_cli.sh logs events      # 事件处理
./mock/conductor_cli.sh logs zmq         # ZMQ 连接
./mock/conductor_cli.sh logs blocks      # blocks 快照
./mock/conductor_cli.sh logs conductor -f  # 实时追踪 conductor
```

## HTTP API 速查

| 接口 | 说明 |
|---|---|
| `GET /health` | → `OK` |
| `GET /workers` | Worker 列表 + indexer |
| `POST /register` | 注册（`type: Mooncake` → ZMQ SUB） |
| `POST /unregister` | 注销 |
| `POST /query` | 按 token IDs 查询 |
| `POST /query_by_hash` | 按 hash 查询 |
| `POST /events` | HTTP 注入事件 |

## 性能分析

### 耗时分布

Conductor 查询链路由三个阶段组成，可通过 `logs-profile` 命令提取各阶段耗时：

```bash
# 跑压测
./mock/conductor_cli.sh bench --count 16 --tokens 102400

# 提取 profiling 日志
./mock/conductor_cli.sh logs-profile /tmp/bench.log

# 或手动分析
grep 'hash_computed\|find_matches\|query profile' /tmp/bench.log
```

### 关键字对照

| 关键字 | 含义 | 优化方向 |
|--------|------|---------|
| `hash_computed` | XXH3 哈希计算耗时（`hash_us`） | 大序列 + 小 `block_size` 时耗时高，已通过 rayon 并行加速 |
| `find_matches` | 前缀树遍历耗时（`elapsed_us`、`depth`） | `depth` 表示命中块数，miss 多则遍历提前结束 |
| `query profile` | 全链路总耗时（`total_us`） | `total_us = hash_us + find_matches + 序列化` |

### 示例输出

```text
hash_computed num_tokens=102400 block_size=128 num_hashes=800 hash_us=234
find_matches seq_len=800 depth=4 active_workers=3 elapsed_us=2913
query profile num_tokens=102400 block_size=128 hash_us=291500 total_us=292100
```

hash 计算与树遍历各自耗时比例清晰，便于定位瓶颈。

## 文件

| 文件 | 说明 |
|---|---|
| `mock/conductor_cli.sh` | 主 CLI（build / up / up-multi / down / bench / ...） |
| `mock/zmq_publisher.py` | Mock ZMQ 发布器（单端口 + 多端口模式，Store/Remove/Clear + 增长） |
| `mock/e2e_test.yaml` | 单端口 K8s 部署（ConfigMap + kv-conductor） |
| `mock/e2e_multi_port.yaml` | 多端口 K8s 部署（ConfigMap + kv-conductor + 多端口 publisher） |
| `mock/collect_logs.sh` | 日志采集 |
| `mock/Dockerfile` | Publisher 独立镜像（python:3.11-slim + pyzmq + msgpack + requests + xxhash） |
| `mock/Dockerfile.e2e` | Publisher e2e 镜像（FROM motor-vllm-e2e + msgpack + xxhash） |
| `Dockerfile` | kv-conductor 镜像 |
