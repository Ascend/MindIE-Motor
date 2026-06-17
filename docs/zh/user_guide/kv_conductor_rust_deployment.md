# Rust版KV Conductor部署

## 特性介绍

KV Conductor 是 PyMotor KV Cache 亲和性调度能力的核心索引服务，负责维护每个
`(model, tenant)` 的 KV 缓存块前缀树（radix tree），为 Router/Scheduler 提供
缓存重叠查询，实现请求向最优 Worker 的亲和性路由。

Rust 版 KV Conductor（`kv-conductor`）是对 Mooncake conductor 的高性能替代实现：

- **低延迟查询**：并发前缀树使用 `parking_lot::RwLock`，读路径无锁竞争，多查询并行
- **多租户隔离**：每个 `(model, tenant)` 独立前缀树，天然隔离
- **多级存储感知**：遵循 Mooncake RFC #1527，支持 XPU/CPU/DISK 三级存储独立追踪
- **Push-based 事件注入**：支持 HTTP `POST /events` 和 ZMQ SUB（可选 feature）双通道
- **无外部依赖**：纯 Rust 编译为单二进制，无需 Go 运行时、无需 Mooncake 源码

KV 事件与交互接口遵循 [Mooncake RFC #1527](https://github.com/kvcache-ai/Mooncake/issues/1527)，
与 PyMotor 的 `ConductorApiClient` 完全兼容，可直接替换 Mooncake conductor。

## 镜像准备

将 `kv-conductor` 二进制打入 PyMotor 基础镜像。基础镜像中不含 Rust 编译环境，
可以选择以下任一方式完成编译和镜像制作。

### 方式一：容器内编译（与 Mooncake Conductor 制作方式一致）

此方式在容器内安装 Rust 工具链并编译，适合目标架构与编译机一致（均为 aarch64）的场景。

1. 启动基础镜像容器：

   ```bash
   docker run -it --name kv_conductor_patch --privileged=true --net=host \
       --shm-size=128g <基础镜像 commit ID> bash
   ```

2. 安装 Rust 编译环境：

   ```bash
   # 安装 rustup（Rust 官方安装器）
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
   source "$HOME/.cargo/env"

   # 验证安装
   rustc --version
   cargo --version
   ```

   如果容器无法直接访问外网，可预先下载 rustup 离线安装包或通过代理安装：

   ```bash
   # 方式 A：配置代理后安装
   export https_proxy=http://<代理地址>:<端口>
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y

   # 方式 B：使用国内镜像源加速
   export RUSTUP_DIST_SERVER=https://mirrors.ustc.edu.cn/rust-static
   export RUSTUP_UPDATE_ROOT=https://mirrors.ustc.edu.cn/rust-static/rustup
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
   ```

3. （可选）如需 ZMQ 订阅能力，安装 libzmq 开发依赖：

   ```bash
   # Ubuntu 基础镜像
   apt update && apt install -y libzmq3-dev pkg-config

   # OpenEuler 基础镜像
   # dnf install -y zeromq zeromq-devel pkg-config
   ```

4. 拉取代码并编译：

   ```bash
   cd /tmp

   # 拉取 kv-conductor 源码
   git clone <kv-conductor 仓库地址>
   cd kv-conductor

   # 编译（默认 feature，不启用 ZMQ）
   cargo build --release

   # 或启用 ZMQ 订阅能力
   # cargo build --release --features zmq

   # 安装到系统路径
   cp target/release/kv-conductor /usr/local/bin/
   ```

5. 清理编译缓存（减小镜像体积）：

   ```bash
   # 删除 Rust 工具链和源码缓存
   rm -rf "$HOME/.cargo" "$HOME/.rustup" /tmp/kv-conductor
   ```

6. 保存镜像：

   ```bash
   exit  # 退出容器
   docker commit -a "add Rust KV Conductor" kv_conductor_patch \
       mindie-motor-vllm:<版本号>-patch
   ```

### 方式二：宿主机编译 + 拷贝二进制

如果开发机上已有 Rust 环境，可直接在宿主机编译后将二进制文件拷贝进容器：

```bash
# 1. 在宿主机上编译（需要与容器相同的目标架构）
cd kv-conductor
cargo build --release
# 若宿主机与容器架构不同（如 x86_64 编译 aarch64），需配置交叉编译：
# rustup target add aarch64-unknown-linux-gnu
# apt install gcc-aarch64-linux-gnu  # 交叉编译链接器
# cargo build --release --target aarch64-unknown-linux-gnu

# 2. 启动容器并拷贝二进制
docker run -it --name kv_conductor_patch --privileged=true --net=host \
    --shm-size=128g <基础镜像 commit ID> bash
# 在另一个终端中：
docker cp target/release/kv-conductor kv_conductor_patch:/usr/local/bin/

# 3. 保存镜像
docker commit -a "add Rust KV Conductor" kv_conductor_patch \
    mindie-motor-vllm:<版本号>-patch
```

### 方式三：多阶段 Dockerfile（推荐用于 CI/CD）

创建 `Dockerfile.kv-conductor`，多阶段构建自动完成编译和镜像制作：

```dockerfile
# Stage 1: 编译
FROM rust:1.85-bookworm AS builder

# 若需 ZMQ feature，取消以下注释：
# RUN apt update && apt install -y libzmq3-dev

COPY kv-conductor /src/kv-conductor
WORKDIR /src/kv-conductor
RUN cargo build --release
# RUN cargo build --release --features zmq

# Stage 2: 打入基础镜像
FROM <基础镜像>

COPY --from=builder /src/kv-conductor/target/release/kv-conductor /usr/local/bin/

EXPOSE 13333
ENTRYPOINT ["kv-conductor"]
CMD ["--port", "13333"]
```

构建并推送镜像：

```bash
docker build -f Dockerfile.kv-conductor -t <镜像仓库>/mindie-motor-kv-conductor:<版本号> .
docker push <镜像仓库>/mindie-motor-kv-conductor:<版本号>
```

### 验证

```bash
# 在容器或宿主机上
kv-conductor --help
# 预期输出：
# KV Conductor — Radix-tree-based KV cache indexer for MindIE-PyMotor
# Usage: kv-conductor [OPTIONS]
# Options:
#       --host <HOST>  Host address to bind to [default: 0.0.0.0]
#   -p, --port <PORT>  Port to listen on [default: 13333]
#   -h, --help         Print help
#   -V, --version      Print version
```

## 部署流程

与 Mooncake conductor 的部署流程一致：修改 `user_config.json` 后通过
`deploy.py` 一键部署。

### 前置条件

请参考 [PyMotor 快速开始](../user_guide/quick_start.md)，确保环境能正常完成基础服务部署。

### 配置 user_config.json

在快速开始的 `user_config.json` 基础上，仅需新增/修改以下三个配置段（其余配置保持
不变）：

**1. `motor_coordinator_config` — 启用 KV Cache 亲和性调度：**

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "scheduler_type": "kv_cache_affinity"
    },
    "prefill_kv_event_config": {
      "conductor_service": "kv-conductor",
      "http_server_port": 13333,
      "block_size": 128,
      "engine_type": "vllm"
    }
  }
}
```

**2. `motor_engine_prefill_config.engine_config` — 新增 `kv-events-config`：**

```json
{
  "motor_engine_prefill_config": {
    "engine_config": {
      "kv-events-config": {
        "publisher": "zmq",
        "enable_kv_cache_events": true,
        "endpoint": "tcp://*:5557",
        "topic": "kv-events",
        "replay_endpoint": "tcp://*:6667"
      }
    }
  }
}
```

> **注意**：`enable-prefix-caching` 默认为 `true`，无需显式配置。请确保引擎启动参数中
> **不要**包含 `--no-enable-prefix-caching`，否则前缀缓存关闭后将无法产生 KV 缓存块，
> Conductor 查询始终返回空结果。

**3. `kv_conductor_config` — 新增 Conductor 服务配置（顶层顶级字段）：**

```json
{
  "kv_conductor_config": {
    "kvevent_instance": {
      "mooncake_master": {
        "type": "Mooncake"
      }
    },
    "http_server_port": 13333
  }
}
```

配置说明：

| 配置项 | 说明 |
|---|---|
| `scheduler_type: "kv_cache_affinity"` | 启用 KV Cache 亲和性调度算法 |
| `prefill_kv_event_config.conductor_service` | Conductor 的 K8s Service 名称，固定 `"kv-conductor"` |
| `prefill_kv_event_config.http_server_port` | Conductor HTTP 服务端口，默认 `13333` |
| `prefill_kv_event_config.block_size` | KV 缓存块大小（tokens/block），需与引擎实际 block_size 一致 |
| `kv-events-config.publisher: "zmq"` | P 实例通过 ZMQ PUB 发布 KV 事件 |
| `kv-events-config.endpoint` | ZMQ 绑定地址，conductor 通过此地址订阅事件 |
| `kv-events-config.replay_endpoint` | ZMQ 重放地址，可选 |
| `kv_conductor_config.kvevent_instance` | 订阅 Mooncake master 广播的 KV 事件实例 |
| **重要** | `enable-prefix-caching` 默认为 `true`，请勿通过 `--no-enable-prefix-caching` 关闭，否则无法产生 KV 缓存块 |

> **说明**：`kv_conductor_config` 配置字段与 Mooncake conductor 完全兼容，
> 无需修改任何字段名或结构。若此前已配置 Mooncake conductor，可直接复用原配置。

### 部署服务

在 `examples/deployer` 目录下通过 deploy.py 一键部署：

```bash
cd examples/deployer

# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm

# 方式二：单独指定配置文件
python deploy.py \
    --user_config_path ../infer_engines/vllm/user_config.json \
    --env_config_path ../infer_engines/vllm/env.json
```

执行后看到如下内容说明部署成功：

```text
...... all deploy end.
```

### 验证

部署完成后，检查 KV Conductor Pod 是否正常运行：

```bash
# 查看 Pod 状态
kubectl get pods -n <job_id> -l app=mindie-motor-kv-conductor

# 查看 Conductor 日志
kubectl logs -n <job_id> -l app=mindie-motor-kv-conductor

# 健康检查
kubectl exec -n <job_id> deploy/mindie-motor-kv-conductor -- \
    curl -s http://localhost:13333/health
# 预期输出: OK
```

查看已注册的 Worker：

```bash
kubectl exec -n <job_id> deploy/mindie-motor-kv-conductor -- \
    curl -s http://localhost:13333/workers | python3 -m json.tool
```

预期输出类似：

```json
{
  "workers": [
    {
      "instance_id": "vllm-prefill-0",
      "model_name": "qwen3-8B",
      "tenant_id": "default",
      "block_size": 128,
      "endpoints": { "0": "tcp://10.0.0.1:5557" }
    }
  ],
  "indexer": [
    {
      "model_name": "qwen3-8B",
      "tenant_id": "default",
      "block_size": 128,
      "worker_count": 1,
      "total_blocks": 42
    }
  ]
}
```

### E2E Mock 测试工具

项目提供了 Mock ZMQ Publisher 和日志采集工具，可在没有真实引擎的情况下对
kv-conductor 进行端到端测试。

**构建镜像：**

```bash
cd kv-conductor

# kv-conductor（需 ZMQ feature）
cargo build --release --features zmq
docker build -t kv-conductor:latest .

# Mock Publisher（基于 motor-vllm-e2e，预装 msgpack）
docker build -t zmq-publisher:latest -f mock/Dockerfile.e2e .
```

**部署 Mock 环境：**

```bash
# 部署 2 个 ZMQ Publisher + KV Conductor
kubectl apply -f mock/e2e_test.yaml
kubectl -n mindie-motor wait --for=condition=ready pod --all --timeout=120s
```

**注册 Publisher（触发 ZMQ 订阅）：**

```bash
# 建立 port-forward
kubectl -n mindie-motor port-forward deploy/mindie-motor-kv-conductor 13333:13333 &

# 一键注册 + 查看状态
./mock/conductor_cli.sh --quick

# 查询缓存命中率
./mock/conductor_cli.sh query --blocks 3
./mock/conductor_cli.sh query-tokens --count 512
```

**日志采集工具 `collect_logs.sh`：**

```bash
./mock/collect_logs.sh blocks          # 查看当前 blocks 快照
./mock/collect_logs.sh events -f       # 实时追踪事件处理日志
./mock/collect_logs.sh zmq             # 查看 ZMQ 连接日志
./mock/collect_logs.sh errors          # 查看错误和告警
./mock/collect_logs.sh conductor -n 100  # 查看 conductor 最近 100 行日志
```

**配置 Mock Publisher 参数：**

```bash
# 修改发布参数（模型名、dp_rank、block 数量等）
kubectl -n mindie-motor edit configmap mock-zmq-config

# 重启使配置生效
kubectl -n mindie-motor rollout restart deploy/zmq-publisher-0
```

**清理：**

```bash
kubectl delete -f mock/e2e_test.yaml
```

详细使用说明见 [kv-conductor/mock/README.md](../../../kv-conductor/mock/README.md)。

## 与 Mooncake Conductor 的差异

| 维度 | Mooncake Conductor (Go) | Rust KV Conductor |
|---|---|---|
| 运行时 | Go runtime | 纯 Rust 单二进制 |
| 编译 | `go build`，需 Go 1.23+ | `cargo build --release`，需 Rust 1.85+ |
| 外部依赖 | libzmq（运行时） | libzmq（仅在 `--features zmq` 时需要） |
| 内存占用 | ~50-100MB | ~10-30MB |
| 配置兼容性 | — | `kv_conductor_config` 字段完全兼容，无需修改 |
| API 兼容性 | — | `/register`, `/unregister`, `/query` 完全兼容 Mooncake RFC #1527 |
| ZMQ 订阅 | 内置 | Feature-gated (`--features zmq`)，支持 Mooncake master 广播 |
| HTTP 事件注入 | 不支持 | 支持 `POST /events`，引擎可直接 HTTP 推送 KV 事件 |
| 查询方式 | `/query`（token IDs） | `/query` + `/query_by_hash`（预计算哈希） |
| 多级存储 | — | XPU/CPU/DISK 独立追踪，查询响应含 per-tier 匹配深度 |

## 常见问题

### Q: ZMQ 订阅连接失败

确认 P 实例的 `kv-events-config.endpoint` 可被 Conductor Pod 访问，且 engine_type
设置为 `"Mooncake"`（用于 ZMQ 订阅）或 `"vllm"`（仅 HTTP 事件注入）。

查看 Conductor 日志中是否有 ZMQ 连接错误：

```bash
kubectl logs -n <job_id> -l app=mindie-motor-kv-conductor | grep ZMQ
```

Conductor 内置了指数退避重连机制（100ms → 30s），ZMQ publisher 重启后会自动恢复连接。

### Q: 查询返回空结果

确认 Worker 已经成功注册：

```bash
# 查看 Coordinator 日志中是否有注册成功日志
kubectl logs -n <job_id> -l app=mindie-motor-coordinator | grep "Register success"
```

确认 KV 事件正在被注入（blocks 计数应持续增长）：

```bash
# 多次查询 /workers，观察 total_blocks 变化
kubectl exec -n <job_id> deploy/mindie-motor-kv-conductor -- \
    curl -s http://localhost:13333/workers
```

### Q: 查询超时

单个查询的超时设置为 0.5 秒。如果频繁超时：

1. 检查 Conductor Pod 的 CPU/Memory 资源是否充足
2. 增大 `kv_conductor_template.yaml` 中的 `resources.requests.cpu`
3. 考虑增加 Conductor 副本数（当前为单副本，可通过 K8s Service 的 ClusterIP 做负载均衡）

### Q: 如何调整日志级别

通过 `kv_conductor_template.yaml` 中的 `RUST_LOG` 环境变量控制：

- `info`（默认）：注册/注销/错误信息
- `debug`：包含每次查询的详细信息
- `warn`：仅告警和错误
- `trace`：包含前缀树每次遍历的细节（仅在调试时使用）

## 附录：user_config.json 完整示例

以下为开启 KV Cache 亲和性调度的完整 `user_config.json` 示例，以 qwen3-8B 模型、
vllm 引擎、Mooncake KV 传输为基线。**标注 "新增" 的配置段**为在快速开始基线配置
之外需要增加的字段，其余字段保持不变。

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "p_instances_num": 1,
    "d_instances_num": 1,
    "single_p_instance_pod_num": 1,
    "single_d_instance_pod_num": 1,
    "p_pod_npu_num": 4,
    "d_pod_npu_num": 4,
    "image_name": "",
    "job_id": "mindie-motor",
    "hardware_type": "800I_A2",
    "weight_mount_path": "/mnt/weight/"
  },
  "motor_controller_config": {},
  "motor_coordinator_config": {
    "scheduler_config": {
      "scheduler_type": "kv_cache_affinity"
    },
    "prefill_kv_event_config": {
      "conductor_service": "kv-conductor",
      "http_server_port": 13333,
      "block_size": 128,
      "engine_type": "vllm"
    }
  },
  "kv_conductor_config": {
    "kvevent_instance": {
      "mooncake_master": {
        "type": "Mooncake"
      }
    },
    "http_server_port": 13333
  },
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "engine_config": {
      "served_model_name": "qwen3-8B",
      "model": "/mnt/weight/qwen3_8B",
      "gpu_memory_utilization": 0.9,
      "data_parallel_size": 2,
      "tensor_parallel_size": 2,
      "pipeline_parallel_size": 1,
      "enable_expert_parallel": false,
      "data_parallel_rpc_port": 9000,
      "kv-events-config": {
        "publisher": "zmq",
        "enable_kv_cache_events": true,
        "endpoint": "tcp://*:5557",
        "topic": "kv-events",
        "replay_endpoint": "tcp://*:6667"
      },
      "api-server-count": 1,
      "enforce-eager": true,
      "max_model_len": 2048,
      "kv_transfer_config": {
        "kv_connector": "MultiConnector",
        "kv_role": "kv_producer",
        "kv_connector_extra_config": {
          "use_layerwise": false,
          "connectors": [
            {
              "kv_connector": "MooncakeLayerwiseConnector",
              "kv_role": "kv_producer",
              "kv_port": "20001",
              "kv_connector_extra_config": {
                "send_type": "PUT"
              }
            },
            {
              "kv_connector": "AscendStoreConnector",
              "kv_role": "kv_producer",
              "kv_connector_extra_config": {
                "lookup_rpc_port": "0",
                "backend": "mooncake"
              }
            }
          ]
        }
      }
    }
  },
  "motor_engine_decode_config": {
    "engine_type": "vllm",
    "engine_config": {
      "served_model_name": "qwen3-8B",
      "model": "/mnt/weight/qwen3_8B",
      "gpu_memory_utilization": 0.9,
      "data_parallel_size": 2,
      "tensor_parallel_size": 2,
      "pipeline_parallel_size": 1,
      "enable_expert_parallel": false,
      "data_parallel_rpc_port": 9000,
      "api-server-count": 1,
      "max_model_len": 2048,
      "kv_transfer_config": {
        "kv_connector": "MultiConnector",
        "kv_role": "kv_consumer",
        "kv_connector_extra_config": {
          "use_layerwise": false,
          "connectors": [
            {
              "kv_connector": "MooncakeLayerwiseConnector",
              "kv_role": "kv_consumer",
              "kv_port": "20002",
              "kv_connector_extra_config": {
                "send_type": "PUT"
              }
            },
            {
              "kv_connector": "AscendStoreConnector",
              "kv_role": "kv_consumer",
              "kv_connector_extra_config": {
                "lookup_rpc_port": "1",
                "backend": "mooncake"
              }
            }
          ]
        }
      }
    }
  }
}
```

以上配置相对于快速开始基线配置**新增**了三个配置段：

- `motor_coordinator_config.scheduler_config.scheduler_type` — 启用 KV Cache 亲和性调度
- `motor_coordinator_config.prefill_kv_event_config` — Conductor 连接参数
- `motor_engine_prefill_config.engine_config.kv-events-config` — P 实例 KV 事件发布
- `kv_conductor_config` — Conductor 服务自身配置
