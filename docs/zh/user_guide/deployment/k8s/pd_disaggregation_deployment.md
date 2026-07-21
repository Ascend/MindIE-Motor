# PD分离服务部署指导

本文档通过**完整详细**的部署案例，指导开发者体验基于 Motor 的 PD 分离服务部署，并指导生产环境配置优化实践。

<a id="setup-and-image-preparation"></a>

## 获取启动脚本 (Setup and Image Preparation)

1. **环境要求**
   - 准备足够存放模型权重的推理服务器。
   - 所有服务器均已完成[环境准备](../../environment_preparation.md)。
   - 已下载模型权重，并保存至服务器可访问的路径。

2. **准备镜像**

   通过以下方式获取镜像，并**将镜像加载至 K8s 集群的所有节点**：

   - **方式一**：下载官方完整的 PyMotor 镜像
     进入 [昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，搜索 `motor`，按设备型号选择对应 PyMotor 镜像。
   - **方式二**：在已有镜像中安装 PyMotor
     基础镜像已安装 CANN、vLLM、vllm-ascend 等组件，可参考 [从 vllm-ascend 构建 MindIE Motor 镜像](../../maintenance/build_motor_image_from_vllm_ascend.md#基于vllm-ascendsglang镜像安装mindie-motor) 额外安装 PyMotor。

   获取镜像后，请使用以下命令将镜像加载至服务器：

     ```bash
     docker load -i xxxx.tar
     ```

   待镜像导入后，请使用以下命令查看docker镜像是否存在

     ```bash
     docker images
     ```

3. **准备服务启动脚本**

   将 `examples` 目录上传至 K8s 集群 master 节点：

   - 使用**官方完整 PyMotor 镜像**：镜像内路径为 `/tmp/motor/examples`，可执行：

     ```bash
     IMAGE="<镜像名或镜像ID>"
     cid=$(docker create "$IMAGE")
     docker cp "$cid:/tmp/motor/examples" ./examples
     docker rm "$cid"
     ```

   - 使用**手动安装 PyMotor 的镜像**：`git clone` 代码仓后，启动脚本位于 `MindIE-PyMotor/examples`。

   更多 `examples` 目录内容，详见章末附录。

---

## 生成配置文件

参考 [MindIE Motor 配置自动生成指导](../../../../../examples/infer_engines/vllm/models/README.md)，自动生成配置文件 `user_config.json` 与 `env.json`。

---

## 服务部署与验证

以下操作均在 K8s 集群 master 节点执行。

1. **拉起服务**

   ```bash
   # 创建命名空间：<namespace> 须与 user_config.json 中 job_id 一致，默认 mindie-motor
   kubectl create namespace <namespace>

   # 进入部署工具目录
   cd examples/deployer
   # 拉起 PD 分离服务：--config_dir 指定含 user_config.json 与 env.json 的目录
   python3 deploy.py --config_dir ../infer_engines/vllm
   ```

2. **查看状态**

   ```bash
   # 查看 Pod 状态：<namespace> 与上文 job_id 一致
   kubectl get pods -n <namespace> -owide
   ```

   回显中各 `Pod` / `Deployment` 的命名可能随模板与 `engine_type` 变化，可按以下方式识别：

   | 运行时类型 | 说明 |
   | --- | --- |
   | `mindie-motor-controller-xxxxx` | 服务管理 Pod，监控各实例健康状态 |
   | `mindie-motor-coordinator-xxxx` | 请求调度 Pod，业务请求入口 |
   | `vllm-dx-xxxx` | D 实例 Pod，负责 Decode 推理 |
   | `vllm-px-xxxx` | P 实例 Pod，负责 Prefill 推理 |

   Pod 状态为 `Running` 仅表示已成功调度并启动，是否业务就绪仍需结合日志进一步确认。

3. **查看日志**

   ```bash
   # 进入部署工具目录
   cd examples/deployer
   # 编辑 log_collect/log_config.ini：将 name_space 改为与 job_id 相同的命名空间
   vim log_collect/log_config.ini
   # 采集并持续跟踪日志：输出目录 log_collect/log/
   bash show_log.sh
   ```

4. **验证服务**

   ```bash
   # 替换占位符：<master_node_ip> 为 master 节点 IP，<served_model_name> 为 user_config.json 中 served_model_name 字段值
   curl -X POST http://<master_node_ip>:31015/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "<served_model_name>",
       "messages": [{"role": "user", "content": "who are you?"}],
       "max_tokens": 36,
       "stream": true
     }'
   ```

   - 返回 `{"detail":"Service is not available"}`：服务尚未就绪，稍后重试。
   - 返回流式 JSON：推理正常。更多接口见 [业务接口](../../api/service_interfaces.md)。

5. **终止服务**

   ```bash
   # 进入部署工具目录
   cd examples/deployer
   # 卸载服务：<namespace> 与上文 job_id 一致
   bash delete.sh <namespace>
   ```

---

## 特性配置指导

上文 `user_config.json` 与 `env.json` 全量示例已默认开启主备倒换、异常实例重启、服务限流、虚推、KV 亲和性调度、KV 池化等能力。若只需调整某项能力，可对照本节做最小配置修改。

### 主备倒换

创建多个 Controller 和 Coordinator 并运行在不同的服务器上，当某一台服务器出现异常（宕机、重启、维护升级），部署于其他服务器的 Controller 和 Coordinator 组件将接管集群，避免单个服务器异常导致业务请求无法调度。

- **原理**：创建多个管理面和业务面的 Pod，分布于多台服务器。当主 Pod 异常时，选择一套备份 Pod 承担当前业务。对外表现上整个服务集群没有任何异常。
- **开启**：

  ```json
  "motor_controller_config": {
    "standby_config": { "enable_master_standby": true }
  },
  "motor_coordinator_config": {
    "standby_config": { "enable_master_standby": true }
  }
  ```

- **关闭**：删除 `standby_config` 配置块，或将 `enable_master_standby` 设为 `false`。
- **注意**：须提前部署 etcd（建议 3 副本）。详见 [主备特性说明](../../features/standby.md)。

### 异常实例重启

P/D 实例出现异常时，重启推理实例，避免实例长时间处于异常状态，影响集群推理功能。

- **原理**：推理主进程异常退出等情况时，Motor 自动重启对应实例。重启期间吞吐能力下降，重启后恢复正常。
- **开启**：默认开启，无需额外操作。
- **关闭**：无需关闭。
- **注意**：详见 [P/D 实例异常重启](../../../design/fault_tolerance/overview.md)。

### 服务限流

限制推理入口 Coordinator 在单位时间内的最大请求数，防止服务过载。

- **原理**：Coordinator 记录一段时间内接收到的请求数量，达到阈值后停止接受外部请求。
- **开启**：

  ```json
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,  // 开启限流
      "max_requests": 10000,      // 最大请求数量
      "window_size": 60           // 时间窗口
    }
  }
  ```

  上述配置表示 60 秒内最多处理 10000 条请求。

- **关闭**：删除 `rate_limit_config` 配置块，或将 `enable_rate_limit` 设为 `false`。
- **注意**：字段说明见 [config_reference — motor_coordinator_config](../../configuration/config_reference.md#motor_coordinator_config)。

<a id="virtual-inference-health-check"></a>

### 虚推健康检查 (Virtual Inference Health Check)

探测服务健康状态，避免静默故障带来业务损失。静默故障表现为：部分进程卡死，服务看似无问题，但无法正常推理。

- **原理**：业务流量较小时发送轻量级推理请求；业务流量较大时查看 NPU 计算核心使用率。不健康的 P/D 实例会被重启以消除静默故障。
- **开启**：

  ```json
  // P 和 D 实例需要单独开启虚推功能：P 实例虚推健康检查开启方式如下，D 实例的开启方式相同
  "motor_engine_prefill_config": {
    "health_check_config": {
      "enable_virtual_inference": true,  // 开启虚推
      "npu_usage_threshold": 10          // 业务流量较大时，NPU 计算核心的使用率超过 10% 就认为服务健康
    }
  }
  ```

- **关闭**：删除 `health_check_config` 配置块，或将 `enable_virtual_inference` 设为 `false`。
- **注意**：详见 [虚推健康检查](../../features/sim_inference.md)。

### KV Cache 亲和调度

将具有相同前缀的请求调度到同一实例，复用已有 KV Cache，减少 Prefill 耗时。

- **原理**：PyMotor KV Cache 亲和性调度能力依赖 Mooncake 社区的 Mooncake Conductor 组件，允许调度器根据 KV Cache 位置优先将请求调度到缓存了对应 KV 的实例，从而减少 KV Cache 跨实例传输开销，提升推理吞吐与响应速度。
- **开启**：

  ```json
  // 开启 KV 亲和性调度
  "motor_coordinator_config": {
    "scheduler_config": { "scheduler_type": "kv_cache_affinity" }
  },
  // 开启 KV 事件发布、开启 prefix cache 特性
  "motor_engine_prefill_config": {
    "engine_config": {
      "kv-events-config": {
        "publisher": "zmq",
        "enable_kv_cache_events": true,
        "endpoint": "tcp://*:5557",
        "topic": "kv-events",
        "replay_endpoint": "tcp://*:6667"
      },
      "enable-prefix-caching": true
    }
  },
  // kv conductor 配置，与 motor_engine_prefill_config 处于同一层级
  "kv_conductor_config": {
    "kvevent_instance": { "mooncake_master": { "type": "Mooncake" } },
    "http_server_port": 13333
  }
  ```

- **关闭**：删除上述配置项。
- **注意**：需要确保镜像中已安装 KV Conductor 组件，详见 [KV Cache 亲和性调度](../../features/KV_cache_affinity.md)。

### KV 池化

通过 `MultiConnector` 将 KV 缓存卸载到共享池，支持跨实例复用，降低显存压力。

- **原理**：允许 P/D 实例通过 KV 缓存池共享 KV Cache，P 实例将计算好的 KV Cache 推入缓存池，D 实例从缓存池拉取并复用，从而在 PD 分离场景下提升显存利用率和推理吞吐。
- **开启**：参数较多，篇幅有限，详见 [KV 池化部署指南](../../features/kv_cache_store/README.md)。
- **关闭**：改用非 `MultiConnector` 的单一 connector，并删除根节点 `kv_cache_pool_config`。
- **注意**：详见 [KV 池化部署指南](../../features/kv_cache_store/README.md)。

---

## 附录

### 运维技巧

- **服务未就绪**：推理接口返回 `{"detail":"Service is not available"}` 时，多为 P/D 或 Coordinator 尚未完全就绪；等待后重试，并查看各 Pod 日志。
- **镜像与权重**：确保 `image_name` 在集群内可正常拉取；`weight_mount_path` 在宿主机上存在。
- **部署失败**：可先按 `终止服务` 小节的命令卸载，排查并修改配置后重新部署。
- **加载权重超时**：部分 vLLM 版本权重加载超过约 10 分钟可能报 `timeout`，通常不影响程序运行；以所用镜像/引擎版本说明为准。
- **实例重调度约束**：实例重调度能力依赖 MindCluster；P/D 实例含多个 Pod 时，直接删除其中一个 Pod 不会触发实例重调度。
- **Prefix Cache 对性能测试的影响**：Prefix Cache 默认开启。若需基线性能，可在 `engine_config` 中增加 `"no-enable-prefix-caching": true`（vLLM）或 `"disable_radix_cache": true`（SGLang）。

### examples 目录结构

```text
examples/
├── deployer/                  # 部署工具目录
│   ├── deploy.py              # 部署入口脚本
│   ├── delete.sh              # 卸载脚本
│   ├── show_log.sh            # 日志查看脚本
│   ├── README.md              # 部署工具使用说明
│   ├── yaml_template/         # K8s YAML 模板
│   ├── startup/               # 启动脚本
│   ├── probe/                 # 探针脚本
│   ├── log_collect/           # 日志采集
│   └── output_yamls/          # 生成的 YAML 输出目录
└── infer_engines/             # 各引擎配置示例
    └── vllm/                  # vLLM 引擎配置
        ├── user_config.json   # 用户配置
        ├── env.json           # 环境变量配置
        └── models/            # 特定模型配置
```

- 配置文件位于 `examples/infer_engines/`，按引擎类型和模型选择对应配置。
- 部署工具用法详见 `examples/deployer/README.md`。
