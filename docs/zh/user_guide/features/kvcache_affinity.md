# KV Cache 亲和性调度

---

## 功能介绍

KV Cache 亲和性调度通过自研 **kv-conductor** 组件（Rust 实现），维护全局 KV Cache 前缀树索引，
将请求路由到已缓存最长 token 前缀的 Worker，减少跨实例 KV Cache 传输开销，提升推理吞吐。

kv-conductor 已集成在 motor Python 包内，随 `build.sh` 条件编译进 wheel 包。
部署时通过 `python -m motor.kv_conductor` 启动，无需额外安装独立二进制。

**子策略**：Coordinator 调度器支持两种评分模式——

| 模式 | 行为 |
|------|------|
| `unified`（默认） | 单一评分 = 亲和性加分 × 折扣系数 + 实时负载 × 负载权重，选最优 endpoint |
| `load_gated` | 先保留负载最低的 N 个 endpoint，再从中选择缓存前缀最长的 |

---

## 前置说明

- 已使用 MindIE Motor 部署 PD 分离推理服务，KV Cache 亲和性调度在该服务基础上开启。
- 开启前请参考 [MindIE Motor 快速开始](../quick_start.md)，确保基础服务部署正常。
- 镜像需包含 kv-conductor 二进制。若使用官方发布镜像，二进制已随 motor wheel 打包；若自行构建，需 Rust 工具链（cargo），详见 [构建说明](#镜像构建)。
- 后续操作均在 K8s 集群管理节点（master 节点）执行。

---

## 快速实践

### 1. 确认基础服务

已使用 motor 部署 PD 分离推理服务且正常运行。

<a id="镜像构建"></a>

### 2. 镜像构建

kv-conductor 已集成在 motor wheel 包内，无需额外构建。`build.sh` 会检测 Rust 工具链：

```bash
bash build.sh
# 有 cargo 环境：自动编译 kv-conductor 并打包进 motor wheel
# 无 cargo 环境：跳过编译，输出 [WARNING]，kv-conductor 不包含在 wheel 中（其他功能不受影响）
```

构建产物 `motor/kv_conductor/bin/kv-conductor` 会自动随 `setup.py` 的 `package_data` 打包进 wheel。

> **注意**：如果构建环境无 Rust 工具链，可安装后重试：
>
> ```bash
> curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
> source "$HOME/.cargo/env"
> ```

### 3. 修改 `user_config.json`

在 `examples/infer_engines/vllm/user_config.json` 中修改以下配置项（详见[典型配置](#典型配置)）：

- `motor_coordinator_config.scheduler_config.scheduler_type` → `"kv_cache_affinity"`
- `motor_engine_prefill_config.engine_config` → 增加 `kv-events-config`
- 新增顶层 `kv_conductor_config`

### 4. 部署服务

```bash
cd examples/deployer
# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

### 5. 验证结果

```bash
kubectl get pod -A -owide
```

预期 P/D 实例和 kv-conductor 均启动成功，Coordinator 日志中可看到"KV Conductor registered"字样。

---

## 典型配置

KV Cache 亲和性只需在已有 PD 分离配置的基础上增加三项：

- `scheduler_type: "kv_cache_affinity"` — 启用亲和性调度器
- `kv-events-config` — P 实例发布 KV Cache 事件
- `kv_conductor_config` — kv-conductor 服务参数

引擎的 `kv-transfer-config`（PD 传输）属于 PD 分离基础配置，非亲和性子系统引入；
KV Cache Store 池化功能单独通过 `kv_cache_store_config` 开启，详见
[KV Cache Store](kv_cache_store/README.md)。

### PD 分离配置

以 [快速开始](../quick_start.md) 的 PD 分离配置为基线，仅展示增量部分（`...` 为已有不变配置）：

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "..."
  },
  "motor_coordinator_config": {
    "scheduler_config": {
      "scheduler_type": "kv_cache_affinity",
      "kv_affinity_mode": "unified",
      "kv_affinity_load_weight": 1.0,
      "kv_affinity_overlap_credit": 1.0
    }
  },
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "engine_config": {
      "..."
      "kv-events-config": {
        "publisher": "zmq",
        "enable_kv_cache_events": true,
        "endpoint": "tcp://*:5557",
        "topic": "kv-events",
        "replay_endpoint": "tcp://*:6667"
      }
    }
  },
  "motor_engine_decode_config": {
    "engine_type": "vllm",
    "engine_config": {
      "..."
    }
  },
  "kv_conductor_config": {
    "block_size": 128,
    "xpu_endpoint": "tcp://*:50090",
    "http_server_port": 13333
  }
}
```

### PD 混部配置

PD 混部使用 `motor_engine_union_config`，将 `kv-events-config` 配置在 union 段中：

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "..."
  },
  "motor_coordinator_config": {
    "scheduler_config": {
      "deploy_mode": "single_node",
      "scheduler_type": "kv_cache_affinity",
      "kv_affinity_mode": "unified"
    }
  },
  "motor_engine_union_config": {
    "engine_type": "vllm",
    "enable_multi_endpoints": true,
    "engine_config": {
      "..."
      "kv-events-config": {
        "publisher": "zmq",
        "enable_kv_cache_events": true,
        "endpoint": "tcp://*:5557",
        "topic": "kv-events",
        "replay_endpoint": "tcp://*:6667"
      }
    }
  },
  "kv_conductor_config": {
    "block_size": 128,
    "xpu_endpoint": "tcp://*:50090",
    "http_server_port": 13333
  }
}
```

PD 混部部署详细说明请参考 [PD 混部服务部署](../deployment/k8s/pd_aggregation_deployment.md)。

---

## 参数说明

### `kv_conductor_config`（kv-conductor 全局配置）

| 配置项 | 类型 | 取值范围 | 说明 |
|--------|------|----------|------|
| **block_size** | uint | ≥ 1 | 事件广播的 hash 粒度（token 数）。标准模型 = 引擎 `--block-size`（默认 128）；DeepSeek V4 等混合模型 = 各 KV group block_size 的 GCD（如 4） |
| **xpu_endpoint** | string | `tcp://*:<port>` | Per-DP HBM 端口模式，conductor 在此端口监听引擎的 XPU KV 事件 |
| **http_server_port** | int | 1024–65535 | kv-conductor HTTP API 端口，Coordinator 通过此端口查询缓存命中，默认 `13333` |
| **replay_endpoint** | string | `tcp://*:<port>` | Per-DP replay 端口，conductor 重启恢复时回放缓冲的 KV 事件（可选） |
| **re_register_interval_sec** | int | ≥ 0 | 周期性重注册间隔（秒），0 或负数禁用（默认 0） |
| **hbm_weight** | uint | ≥ 0 | HBM 块的单块匹配权重，默认 `3` |

以下参数为 CPU/Disk 二级缓存的配置项，当前文档仅涉及 HBM 亲和性，L2 配置不在本文范围内：

| 配置项 | 类型 | 取值范围 | 说明 |
|--------|------|----------|------|
| **pool_endpoint** | string | `tcp://<host>:<port>` | 中心化后端（Mooncake/Memcache）的池服务地址（L2） |
| **cpu_endpoint** | string | `tcp://*:<port>` | Per-DP CPU/DDR 端口（L2） |
| **disk_endpoint** | string | `tcp://*:<port>` | Per-DP DISK/SSD 端口（L2） |
| **store_backend** | string | `Mooncake` / `Memcache` / `YuanRong` | 池化后端类型（L2） |
| **cpu_weight** | uint | ≥ 0 | CPU 块的单块匹配权重，默认 `2`（L2） |
| **disk_weight** | uint | ≥ 0 | Disk 块的单块匹配权重，默认 `1`（L2） |

### `scheduler_config`（调度器亲和性参数）

| 配置项 | 类型 | 取值范围 | 说明 |
|--------|------|----------|------|
| **scheduler_type** | string | `kv_cache_affinity` | 启用 KV Cache 亲和性调度 |
| **kv_affinity_mode** | string | `unified` / `load_gated` | 评分子策略。`unified`（默认）：单一评分融合亲和性与实时负载；`load_gated`：先过滤负载最低的 N 个 endpoint，再按亲和性选择 |
| **kv_affinity_load_weight** | float | `[0, +∞)` | `unified` 模式下 endpoint 实时负载权重。`1.0`（默认）表示负载与亲和折扣后的 prefill 成本同等重要；`0` 表示纯亲和性，不感知负载 |
| **kv_affinity_overlap_credit** | float | `[0, +∞)` | 缓存前缀对 prefill 成本的折扣系数。值越大，已缓存前缀的折扣越高。默认 `1.0` |
| **kv_affinity_prefill_load_scale** | float | `[0, +∞)` | `unified` 模式下亲和折扣后的 prefill 成本权重。默认 `1.0` |
| **kv_affinity_load_gate_topn** | int | `[0, +∞)` | `load_gated` 模式下保留负载最低的 N 个 endpoint。`0` 时回退为 `2`（默认 `0`） |

### `kv-events-config`（引擎侧 KV 事件发布配置）

| 配置项 | 类型 | 取值范围 | 说明 |
|--------|------|----------|------|
| **publisher** | string | `zmq` | 事件发布后端，当前仅支持 `zmq` |
| **enable_kv_cache_events** | bool | `true` / `false` | 是否启用 KV Cache 事件，设为 `true` |
| **endpoint** | string | `tcp://*:<port>` | P 实例发布 KV 事件的 ZMQ 端点 |
| **topic** | string | 自定义 | 事件 ZMQ 主题 |
| **replay_endpoint** | string | `tcp://*:<port>` | 事件回放端点，供 conductor 重启后恢复索引（可选） |

> **注意**：`kv-events-config` 是 vLLM 原生配置，控制引擎侧的 KV 事件发布行为；
> `kv_conductor_config` 是 Motor 配置，控制 Coordinator 如何注册和查询 kv-conductor。
> 两者分离，互不干扰。二级缓存（CPU/Disk）亲和性配置请参考后续版本的 L2 亲和性文档。

---

## DeepSeek V4 / 混合 KV Cache 模型

DeepSeek V4 开启了 `--no-disable-hybrid-kv-cache-manager`，引擎内部有**多种 KV cache group**，每种 block_size 不同：

| KV Group | block_size | 说明 |
|----------|-----------|------|
| Full MLA | 128（随 `--block-size`） | 全注意力层 |
| SWA MLA  | 64 | Sliding Window MLA |
| C128 状态 | 8 | 压缩 KV 状态 |
| C4 状态   | 4 | 压缩 KV 状态 |

引擎内部用 `hash_block_size = GCD([128, 64, 8, 4]) = 4` 计算事件的 block hashes。
因此 `kv_conductor_config.block_size` **必须设为 4**：

```json
"kv_conductor_config": {
  "block_size": 4,
  "xpu_endpoint": "tcp://*:50090"
}
```

引擎启动日志会打印实际的 hash_block_size，可据此确认：

```text
# vLLM 日志输出
hash_block_size = 4
```

> **警告**：若 `block_size` 配置错误（如用了 128），conductor 查询时用 128 粒度的 hash 去匹配引擎用 4 粒度存入的 hash，命中率始终为 0。

---

## 原理说明

### 整体流程

1. **KV Cache 事件发布**：P 实例完成 prefill 计算后，通过 `kv-events-config` 中配置的 ZMQ 端点发布 KV Cache 事件（包含 block hashes、token IDs、parent hash 等）。
2. **Conductor 索引**：kv-conductor 通过 ZMQ SUB 订阅引擎事件，根据 token IDs 重算 XXH3 内容哈希，构建 HBM RadixTree + CPU/Disk continuation-edge 索引。
3. **亲和性调度决策**：Coordinator 调度器（`scheduler_type: kv_cache_affinity`）在分配新请求时，将 token IDs 发给 kv-conductor 查询各 Worker 的缓存命中情况，按评分策略选择最优 Worker。

### 评分模型

kv-conductor 对三层介质分别计分，按权重加权求和：

| 介质 | 权重 | 匹配方式 |
|------|------|----------|
| HBM（XPU） | `hbm_weight`（默认 3） | RadixTree 前缀链匹配 |
| CPU | `cpu_weight`（默认 2） | continuation-edge 连续边匹配，从 HBM 断点续查 |
| Disk | `disk_weight`（默认 1） | continuation-edge 连续边匹配，从 HBM 断点续查 |

`scheduler_config` 中的亲和性参数（`kv_affinity_mode`、`kv_affinity_load_weight` 等）进一步将 kv-conductor 返回的原始命中分数与 endpoint 实时负载融合，产生最终调度决策。

### 部署流程

`deploy.py` 执行后的关键动作：

- 创建/更新 ConfigMap `motor-config`（内容来自 `user_config.json`）
- 生成各服务 YAML 到 `output/deployment/`
- kv-conductor 以 sidecar 形式部署，通过 `python -m motor.kv_conductor` 启动
- Coordinator 调度器按 `kv_cache_affinity` 策略进行亲和性路由

---

## 调优建议

| 场景 | 建议 |
|------|------|
| 纯吞吐优先 | `kv_affinity_mode: unified`，`kv_affinity_load_weight: 0`（纯亲和性，不感知负载） |
| 负载均衡优先 | `kv_affinity_mode: unified`，`kv_affinity_load_weight: 2.0`（负载权重更高） |
| 延迟敏感（保守） | `kv_affinity_mode: load_gated`，`kv_affinity_load_gate_topn: 3`（只在低负载中选最优前缀） |
| DeepSeek V4 混合模型 | `block_size: 4`（= GCD of all group block_sizes） |
| `http_server_port` | 确保不与集群其他服务端口冲突，默认 `13333` |

---

## 常见问题

### 服务启动后 P/D 实例间无法传输 KV Cache

检查 `kv_transfer_config` 中 `kv_role` 是否正确（P 为 `kv_producer`，D 为 `kv_consumer`），以及 `kv_port` 是否一致。

### Coordinator 无法连接到 kv-conductor

1. 确认 kv-conductor pod 已启动：`kubectl get pod -A | grep kv-conductor`
2. 检查 `kv_conductor_config.http_server_port` 是否配置正确且未被占用
3. 查看 kv-conductor 日志：`kubectl logs <kv-conductor-pod>`

### P 实例发布 KV Cache 事件失败

检查 `kv-events-config` 中 `endpoint` 和 `replay_endpoint` 配置是否正确，以及 P 实例与 kv-conductor 之间的网络是否可达。

### 命中率始终为 0

1. 检查 `kv_conductor_config.block_size` 是否与引擎实际的 `hash_block_size` 一致（见引擎日志）
2. 确认 `kv-events-config.enable_kv_cache_events` 设为 `true`
3. 查看 Coordinator 日志检查 kv-conductor 注册和查询是否有报错

### kv-conductor 未包含在 wheel 包中

构建环境缺少 Rust 工具链，`build.sh` 已自动跳过。安装 rustup 后重新执行 `bash build.sh` 即可。详见 [镜像构建](#镜像构建)。
