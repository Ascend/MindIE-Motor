# KV Cache亲和性调度能力部署

---

## 功能介绍

MindIE Motor KV Cache亲和性调度能力依赖 Mooncake 社区的 Mooncake Conductor 组件，允许调度器根据 KV Cache 位置优先将请求调度到缓存了对应 KV 的实例，从而减少 KV Cache 跨实例传输开销，提升推理吞吐与响应速度。相关能力和接口的介绍可参考 [Mooncake Conductor 介绍文档](https://github.com/yejj710/Mooncake/blob/6dca8cc76ce074fa9c41f02e9a2195c7c1c9308f/docs/source/design/conductor/indexer-api-design.md)。

通过修改 `user_config.json` 配置文件后即可通过 `deploy.py` 脚本完成服务部署。

---

## 前置说明

- 必须已使用 MindIE Motor 部署 PD 分离推理服务，KV Cache 亲和性调度在该服务基础上开启。
- 开启亲和性调度能力前请先参考 [MindIE Motor 快速开始](../quick_start.md)，确保环境能正常完成基础的服务部署。
- 当前 Mooncake Conductor 组件相关代码还未上库主线分支，当前镜像中不含 Mooncake Conductor，**需要基于现有镜像额外安装 Mooncake Conductor 服务组件**（见快速实践步骤二）。
- 后续所有操作只在 k8s 集群的管理节点（master 节点）执行。

---

## 快速实践

1. 已预先使用 motor 部署 PD 分离推理服务，且该服务正常运行。

2. 准备包含 Mooncake Conductor 的镜像

   1. 启动容器。

      ```bash
      docker run -it --name mooncake_patch --privileged=true --net=host --shm-size=128g <commit ID> bash
      # 需要替换基础镜像的commit ID
      ```

   2. 准备 go 环境。

      ```bash
      # 下载 golang 安装文件
      wget https://mirrors.aliyun.com/golang/go1.23.8.linux-arm64.tar.gz
      tar -C /usr/local -xzf go1.23.8.linux-arm64.tar.gz
      echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc

      # golang 环境变量设置
      go env -w GOSUMDB=off   # 不验证 CA 证书
      go env -w GOPROXY=direct # 直接访问 github 拉取
      ```

   3. 安装 libzmq 相关依赖。

      ```bash
      # Ubuntu
      apt update
      apt install libzmq5 libzmq3-dev
      ```

      ```bash
      # openEuler
      dnf install zeromq zeromq-devel
      ```

   4. 下载 Mooncake 源码并编译 `mooncake_conductor`。

      ```bash
      git clone https://github.com/kvcache-ai/Mooncake.git -b dev/kv-indexer
      cd Mooncake/mooncake-conductor/conductor-ctrl/
      go mod tidy
      go build -o mooncake_conductor main.go
      mv mooncake_conductor /usr/local/bin/
      ```

   5. 保存镜像。

      ```bash
      docker commit -a "add Mooncake Conductor" mooncake_patch mindie-motor-vllm:dev-26.0.0.B060-800I-A3-py311-Ubuntu24.04-lts-aarch64-patch
      ```

3. 修改 `user_config.json` 配置文件

   在 `examples/infer_engines/vllm/user_config.json` 中，添加或修改 `kv_conductor_config` 以及 `kv-events-config` 配置项。具体配置格式参见下方[典型配置](#典型配置)章节。

   关键要点：
   - 在 `motor_coordinator_config` 中将 `scheduler_config.scheduler_type` 配置为 `kv_cache_affinity`。
   - 在 `motor_engine_prefill_config.engine_config` 中增加 `kv-events-config`，开启 P 实例的 KV Cache 事件发布能力。
   - 新增 `kv_conductor_config` 全局配置，用于指定 Conductor 服务端口等参数。
   - 其余配置项与不开启亲和性调度时保持一致。

4. 部署服务

   在 `examples/deployer` 目录下执行部署命令：

   ```bash
   cd examples/deployer
   # 方式一：指定配置目录（推荐）
   python deploy.py --config_dir ../infer_engines/vllm

   # 方式二：单独指定配置文件
   python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
   ```

5. 验证结果

   ```bash
   kubectl get pod -A -owide
   ```

   预期 P/D 实例启动成功，服务正常运行。

---

## 典型配置

### 1. PD 分离配置示例

以 [MindIE Motor 快速开始](../quick_start.md) 中的 `user_config.json` 为基线，开启 KV Cache 亲和性调度后的完整配置示例如下：

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "..."
  },
  "motor_controller_config": {},
  "motor_coordinator_config": {
    "scheduler_config": {
      "scheduler_type": "kv_cache_affinity"
    }
  },
  "motor_nodemanger_config": {},
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
    "http_server_port": 13333
  }
}
```

### 2. PD 混部配置示例

PD 混部不使用 `motor_engine_prefill_config`，应将 `kv-events-config` 与
`enable-prefix-caching` 配置在 `motor_engine_union_config.engine_config` 中；Coordinator
启动时会从 union 引擎段自动合并 `prefill_kv_event_config`（`endpoint`、`replay_endpoint`、
`model_path` 等），无需手写 prefill 段配置。

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "..."
  },
  "motor_controller_config": {},
  "motor_coordinator_config": {
    "scheduler_config": {
      "deploy_mode": "single_node",
      "scheduler_type": "kv_cache_affinity"
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
    "http_server_port": 13333
  }
}
```

PD 混部部署详细说明请参考 [PD 混部服务部署](../deployment/k8s/pd_aggregation_deployment.md)。

### 3. 参数说明

各项参数功能说明：

**`kv_conductor_config`（KV Conductor 全局配置）**

| 配置项 | 取值类型 | 取值范围 | 配置说明 |
| --- | --- | --- | --- |
| **kvevent_instance** | dict | - | KV 事件实例配置，当前仅支持 `Mooncake` 类型。 |
| kvevent_instance.mooncake_master.type | string | `Mooncake` | KV 事件后端类型，固定为 `Mooncake`。 |
| http_server_port | int | 1024~65535 | KV Conductor 的 HTTP 服务端口；未配置时 `deploy.py` 默认补充为 `13333`。 |

**`motor_coordinator_config.scheduler_config`（调度器配置）**

| 配置项 | 取值类型 | 取值范围 | 配置说明 |
| --- | --- | --- | --- |
| **scheduler_type** | string | `kv_cache_affinity` | 设置为 `kv_cache_affinity` 表示采用 KV Cache 亲和性调度算法。 |

**`motor_engine_prefill_config.engine_config.kv-events-config`（P 实例 KV 事件配置）**

| 配置项 | 取值类型 | 取值范围 | 配置说明 |
| --- | --- | --- | --- |
| **publisher** | string | `zmq` | 事件发布后端，当前仅支持 `zmq`。 |
| **enable_kv_cache_events** | bool | `true` / `false` | 是否启用 KV Cache 事件，设置为 `true`。 |
| **endpoint** | string | `tcp://*:<port>` | P 实例发布事件端点。 |
| **topic** | string | 自定义 | 事件主题。 |
| **replay_endpoint** | string | `tcp://*:<port>` | 事件回放端点。 |

> **关于 Connector**：示例中 `kv_connector` 使用 `MultiConnector`，其中 `connectors[0]`
（`MooncakeLayerwiseConnector`，传输层）决定 P/D 协同 capability，`connectors[1]`
（`AscendStoreConnector`，KV 池后端）不参与判定、无需在识别白名单中。识别白名单与
`dispatch_profile` 逃生口详见
[PD 分离特性说明](../../design/pd_disaggregation.md#connector-驱动执行计划)。

---

## 原理说明

### KV Cache 亲和性调度整体流程

MindIE Motor KV Cache 亲和性调度能力基于 Mooncake Conductor 组件实现。整体流程如下：

1. **KV Cache 事件发布**：P 实例完成 PreFill 计算后，通过 `kv-events-config` 中配置的 ZMQ 端点发布 KV Cache 事件（包含 sequence 的 KV Cache 位置信息）。
2. **Conductor 事件收集**：Mooncake Conductor 组件接收并索引 P 实例发布的 KV Cache 事件，维护一张全局的 KV Cache 位置映射表。
3. **亲和性调度决策**：Coordinator 中的调度器（`scheduler_type: kv_cache_affinity`）在分配请求时查询 Conductor 中的 KV Cache 位置信息，优先将请求调度到缓存了对应 KV Cache 的 D 实例，从而减少 KV Cache 跨节点传输。
4. **P/D 协同**：P 与 D 实例之间通过 `kv_transfer_config` 配置的 `kv_connector` 建立传输通道，由 `kv_role` 区分生产者/消费者角色。

### 部署流程

在 `examples/deployer` 目录下执行全量部署：

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm
```

执行后看到如下内容，说明执行成功：

```bash
...... all deploy end.
```

完成后：

- 集群中会创建/更新 ConfigMap `motor-config`（内容来自当前输入的 `user_config.json`），后续扩缩容与刷新的基线。
- `output/deployment/` 下会生成各服务 YAML。
- Coordinator 中调度器会根据 `kv_cache_affinity` 策略进行亲和性调度。

### 关键配置调优建议

- **`http_server_port`**：KV Conductor 服务端口，需确保不与集群中其他服务端口冲突，默认 `13333`。
- **`endpoint` 与 `replay_endpoint`**：P 实例的事件发布与回放端口，需确保 P/D 实例间网络互通，且端口未被占用。
- **`use_layerwise`**：在 KV Cache 亲和性调度场景下，建议设置为 `false`，由 Conductor 管理全局 KV Cache 位置信息，无需按 layer 粒度单独传输。

---

## 常见问题

1. **服务启动后 P/D 实例间无法传输 KV Cache**

   请检查 `kv_transfer_config` 中的 `kv_role` 是否正确（P 为 `kv_producer`，D 为 `kv_consumer`），以及 `kv_port` 是否配置一致。

2. **Coordinator 无法连接到 Conductor 服务**

   检查 `kv_conductor_config` 中的 `http_server_port` 是否配置正确，确保 Conductor 服务端口未被占用。

3. **P 实例发布 KV Cache 事件失败**

   检查 `kv-events-config` 中的 `endpoint` 和 `replay_endpoint` 配置是否正确，以及 P 实例与 Conductor 之间的网络是否可达。
