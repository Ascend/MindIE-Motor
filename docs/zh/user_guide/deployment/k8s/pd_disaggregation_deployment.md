# PD分离服务部署（基于vLLM推理引擎）

本文皆在指导开发者基于MindIE Motor的vLLM推理引擎完成Kubernetes环境下的PD分离部署，并提供生成环境配置优化实践指导。若底层推理引擎为SGLang，请参考[PD分离服务部署（基于SGLang推理引擎）](./pd_disaggregation_sglang.md)。

## 环境预检查

使用Ascend 950PR&950DT系列产品部署推理服务时，需要准备**hixlep配置路径**。（其他硬件产品忽略此步骤）

请在每台服务器检查/lib/route.conf、/etc/hccl_rootinfo.json配置文件以及/etc/hixlep目录（UB链路拓扑结构）是否存在且正确；若不存在或不正确，请参考[hixlep配置文件生成文档](https://gitcode.com/cann/hixl/wiki/A5%20LocalCommRes%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md)生成对应内容，在生成/etc/hixlep时使用“D2D场景”即可。

## 获取启动脚本

1. 按照以下要求准备环境。
   - 准备足够存放模型权重的推理服务器。
   - 所有服务器均已完成[环境准备](../../environment_preparation.md)。
   - 已下载模型权重，并保存至服务器可访问的路径。

2. 准备镜像。

   通过以下方式获取镜像，并将镜像加载至K8s集群的所有节点：

   - 方式一：下载官方完整的MindIE Motor镜像

      进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框输入`motor`，单击“mindie-motor”卡片，在“镜像下载”页签根据设备型号下载对应的MindIE Motor镜像。
   - 方式二：在已有镜像中安装MindIE Motor

      基础镜像已安装CANN、vLLM、vLLM Ascend等组件，请参考[基于vllm-ascend/sglang镜像安装MindIE Motor](../../maintenance/build_motor_image_from_vllm_ascend.md#基于vllm-ascendsglang镜像安装mindie-motor) 额外安装MindIE Motor。

    获取镜像后，请使用以下命令将镜像加载至服务器：

    ```bash
    docker load -i xxxx.tar
    ```

    待镜像导入后，请使用以下命令查看Docker镜像是否存在：

      ```bash
      docker images
      ```

3. 准备服务启动脚本。

    请将examples目录上传至K8s集群的master节点。examples可使用以下两种方式获取：

   - 方式一：下载官方完整的MindIE Motor镜像

      镜像内路径为`/tmp/motor/examples`，可执行以下命令获取：

        ```bash
        # 自定义一个存镜像名或镜像ID的变量IMAGE
        IMAGE="<镜像名或镜像ID>"
        # 创建一个未启动的临时容器，并记录容器ID
        cid=$(docker create "$IMAGE")
        # 把容器中/tmp/motor/examples目录复制到宿主机的当前目录
        docker cp "$cid:/tmp/motor/examples" ./examples
        # 删除以上创建的临时容器$cid，清理资源，避免残留无用容器
        docker rm "$cid"
        ```

   - 方式二：在已有镜像中安装MindIE Motor

     git clone代码仓后，启动脚本位于`MindIE-Motor/examples`目录。

    examples目录结构如下所示：

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
    └── model_configs/             # 各引擎配置示例
        └── vllm/                  # vLLM 引擎配置
            ├── user_config.json   # 用户配置
            ├── env.json           # 环境变量配置
            └── models/            # 特定模型配置
    ```

    >[!NOTE] 说明
    >
    >- 配置文件位于examples/model_configs目录下，按引擎类型和模型选择对应配置。
    >- 部署工具用法详见examples/deployer/README.md。

## 生成配置文件

请参考[MindIE Motor配置自动生成指导](../../../../../model_configs/vllm/models/README.md)，自动生成user_config.json与env.json配置文件。

## 服务部署与验证

以下操作均在K8s集群的master节点执行。

1. 使用以下命令启动服务。

    ```bash
    # 创建命名空间：<namespace>必须与user_config.json中job_id一致，默认为mindie-motor
    kubectl create namespace <namespace>

    # 进入部署工具目录
    cd examples/deployer
    # 拉起PD分离服务：--config_dir指定含user_config.json与env.json的目录
    python3 deploy.py --config_dir ../../model_configs/vllm
    ```

    >[!NOTE] 说明
    >- 镜像与权重：确保image_name在集群内可正常拉取；weight_mount_path在宿主机上存在。
    >- 部署失败：可先按步骤5的终止命令卸载当前服务，排查并修改配置后重新部署。
    >- 加载权重超时：部分vLLM版本权重加载超过约10分钟可能报`timeout`，通常不影响程序运行；以所用镜像/引擎版本说明为准。
    >- 实例重调度约束：实例重调度能力依赖MindCluster；P/D实例含多个Pod时，直接删除其中一个Pod不会触发实例重调度。
    >- Prefix Cache对性能测试的影响：Prefix Cache默认开启。若需基线性能，可在engine_config参数配置块中增加以下内容：
    >   - vLLM推理引擎："no-enable-prefix-caching": true
    >   - SGLang推理引擎："disable_radix_cache": true

2. 使用以下命令查看服务状态。

   ```bash
   # 查看Pod状态：<namespace> 与上文job_id一致
   kubectl get pods -n <namespace> -o wide
   ```

   回显中各Pod/Deployment的命名可能随模板与engine_type变化，可按以下方式识别：

   | 运行时类型 | 说明 |
   | --- | --- |
   | mindie-motor-controller-xxxxx | 服务管理Pod，监控各实例健康状态。 |
   | mindie-motor-coordinator-xxxx | 请求调度Pod，业务请求入口。 |
   | vllm-dx-xxxx | D实例Pod，负责Decode推理。 |
   | vllm-px-xxxx | P实例Pod，负责Prefill推理。 |

   Pod状态为`Running`仅表示已成功调度并启动，是否业务就绪仍需结合日志进一步确认。

3. 使用以下命令查看日志。

   ```bash
   # 编辑 log_collect/log_config.ini：将 name_space 改为与 job_id 相同的命名空间
   vim log_collect/log_config.ini
   # 采集并持续跟踪日志：输出目录 log_collect/log/
   bash show_log.sh
   ```

4. 使用以下命令验证服务。

   ```bash
   # 替换占位符：<master_node_ip>为master节点IP，<served_model_name>为user_config.json中served_model_name字段值
   curl -X POST http://<master_node_ip>:31015/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "<served_model_name>",
       "messages": [{"role": "user", "content": "who are you?"}],
       "max_tokens": 36,
       "stream": true
     }'
   ```

   - 返回{"detail":"Service is not available"}：表示服务尚未就绪，多为P/D或Coordinator尚未完全就绪，等待后重试，并查看各Pod日志。
   - 返回流式JSON：推理正常。更多接口请参考[业务接口](../../api/service_interfaces.md)。

5. 使用以下命令终止服务。

   ```bash
   # 卸载服务：<namespace>与上文job_id一致
   bash delete.sh <namespace>
   ```

## 特性配置

[生成配置文件](#生成配置文件)中生成的user_config.json与env.json配置文件已默认开启主备倒换、异常实例重启、服务限流、KV 亲和性调度、KV 池化等能力。若只需调整某项能力，可参照本节内容做最小配置修改。

### 主备倒换

创建多个Controller和Coordinator并运行在不同的服务器上，当某一台服务器出现异常（宕机、重启、维护升级），部署于其他服务器的Controller和Coordinator组件将接管集群，避免单个服务器异常导致业务请求无法调度。

**特性原理**

创建多个管理面和业务面的Pod，分布于多台服务器。当主Pod异常时，选择一套备份Pod承担当前业务。对外表现上整个服务集群没有任何异常。

**开启特性**

>[!NOTE] 说明
>须提前部署ETCD（服务端至少需要3个副本，以保证ETCD集群的可靠性）。该特性详细文档请参考[主备倒换特性](../../features/fault_tolerance/standby.md)。

  ```json
  "motor_controller_config": {
    "standby_config": { "enable_master_standby": true }
  },
  "motor_coordinator_config": {
    "standby_config": { "enable_master_standby": true }
  }
  ```

**关闭特性**

删除standby_config参数配置块，或将enable_master_standby参数设为：false。

### 异常实例重启

P/D实例出现异常时，重启推理实例，避免实例长时间处于异常状态，影响集群推理功能。

**原理原理**

推理主进程异常退出等情况时，Motor 自动重启对应实例。重启期间吞吐能力下降，重启后恢复正常。
**开启特性**

默认开启，无需额外操作。

**关闭特性**

该特性不支持关闭。

### 服务限流

限制推理入口Coordinator在单位时间内的最大请求数，防止服务过载。

**特性原理**

Coordinator记录一段时间内接收到的请求数量，达到阈值后停止接收外部请求。

**开启特性**

  ```json
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,
      "max_requests": 10000,
      "window_size": 60
    }
  }
  ```

上述配置表示60秒内最多处理10000条请求。

**关闭特性**

删除rate_limit_config参数配置块，或将enable_rate_limit参数设置为：false。

>[!NOTE] 说明
>该特性详细文档请参考[服务限流](../../features/rate_limiting.md)。字段说明请参考 [motor_coordinator_config](../../configuration/config_reference.md#motor_coordinator_config)中的rate_limit_config字段。

### 虚拟推理健康探测

NodeManager负责原生引擎的就绪探测、进程监管和虚推健康探测：

- **就绪探测**：轮询原生引擎业务端口的`/health`，在startup_timeout窗口内保持STARTING，成功后才允许调度。
- **进程存活**：监管原生引擎进程组，主进程或工作进程异常退出时触发实例恢复。
- **vLLM虚推**：对DP0实例发送轻量级推理请求，并结合AI Cube利用率识别静默故障；达到失败阈值后降级状态，但不直接终止进程。
- **SGLang 健康探测**：使用SGLang原生生成式`GET /health`，不创建Motor虚推monitor。
- **软件故障**：引擎提供`/fault_tolerance/status`时，可由EngineFtManager补充软件故障上报。

**开启特性**

  ```json
  "motor_engine_prefill_config": {
    "health_check_config": {
      "enable_virtual_inference": true,
      "npu_usage_threshold": 10,
      "health_collector_timeout": 5,
      "health_collector_timeout_retry_attempts": 3,
      "startup_timeout": 1800
    }
  }
  ```

**关闭特用**

将enable_virtual_inference参数设为`false`，不会关闭SGLang原生生成式`GET /health`。

**日志级别限制**

vLLM虚推仅允许在最终引擎环境ASCEND_GLOBAL_LOG_LEVEL=3（未配置时默认为ERROR）时开启。该特性详细文档请参考[虚拟推理健康探测](../../features/sim_inference.md)。

### KV Cache亲和调度

将具有相同前缀的请求调度到同一实例，复用已有KV Cache，减少Prefill耗时。

**特性原理**

MindIE Motor的KV Cache亲和性调度能力依赖 Mooncake 社区的 Mooncake Conductor 组件，允许调度器根据 KV Cache 位置优先将请求调度到缓存了对应 KV 的实例，从而减少 KV Cache 跨实例传输开销，提升推理吞吐与响应速度。

**开启特性**

- motor_coordinator_config字段：开启KV Cache亲和性调度。
- motor_engine_prefill_config字段：开启KV事件发布、开启prefix cache特性。
- kv_conductor_config字段：kv conductor配置，与motor_engine_prefill_config处于同一层级。

```json
"motor_coordinator_config": {
  "scheduler_config": { "scheduler_type": "kv_cache_affinity" }
},
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
"kv_conductor_config": {
  "kvevent_instance": { "mooncake_master": { "type": "Mooncake" } },
  "http_server_port": 13333
}
```

**关闭特性**

删除上述配置项。

>[!NOTE] 说明
>需要确保镜像中已安装KV Conductor组件，该特性详细文档请参考[KV Cache亲和性调度](../../features/kvcache_affinity.md)。

### PD异构（PR/DT）调度

Ascend 950PR&950DT系列产品分为PR、DT两类，两者的内存存在差异，DT的内存宽带更大、性能更好，更适合部署Decode；Prefill可部署在PR上。因此**PR + DT 组网**时，需要把P实例调度到PR节点，D实例调度到DT节点。

**特性原理**

在PD分离的Prefill/Decode Pod上追加`huawei.com/npu.chip.name`到`nodeSelector`，与节点上已有的芯片标签匹配。

**特性约束**

开启后Prefill只能调度到PR节点、Decode只能调度到DT节点，同角色恢复/扩容也只能使用对应类型的空闲节点。

| 特性 | PR+DT 异构 | 说明 |
|------|------------|------|
| ScaleP2D（缩P保D） | 不支持 | 停掉P后释放的是PR节点，D仍只能调度到DT，无法占用这些节点完成恢复。 |
| MindCluster实例重调度、容器快照默认重调度 | 受限 | P只能再调度到空闲PR，D只能再到空闲DT，对应类型没有空闲节点时无法恢复。 |
| 自动弹性扩缩容 | 受限 | 只能按角色在PR/DT上分别增减，不能靠缩P把PR资源转给D扩容。扩P需要空闲PR，扩D需要空闲DT。 |
| 手动扩缩容 | 受限 | 只能按角色在PR/DT上分别增减，不能靠缩P把PR资源转给D扩容。扩P需要空闲PR，扩D需要空闲DT。 |
| 异常实例原地重启、引擎重拉 | 支持 | 仍在原Pod/原节点，不换卡类型。 |
| Controller/Coordinator主备倒换 | 支持 | 管理面不使用PR/DT的`nodeSelector`。 |
| Coordinator故障场景请求重调度 | 支持 | 将请求转到其他健康实例，不改变P/D的节点类型绑定。 |
| 故障隔离、虚推健康探测、token 级重推 | 支持 | 不依赖跨PR/DT占节点。 |
| KV Cache亲和调度、KV池化、服务限流、Tracing | 支持 | 与卡类型调度无关。 |

建议在PR+DT组网下保持`enable_scale_p2d`为`false`。实例重调度与弹性扩缩容仅在对应类型节点有空闲容量时按原能力工作。

**开启特性**

在Prefill/Decode各自配置中填写对应字段；不填则不加该条`nodeSelector`。

  ```json
  "motor_engine_prefill_config": {
    "npu_chip_name": "Ascend950PR"
  },
  "motor_engine_decode_config": {
    "npu_chip_name": "Ascend950DT"
  }
  ```

**关闭特性**

删除上述字段或不填。

>[!NOTE] 说明
>仅Kubernetes PD分离生效。`hardware_type`为`Ascend950`时基础`nodeSelector`为`accelerator: huawei-npu`；芯片名标签是额外一条，全PR或全DT组网无需填写。基于Docker部署无调度器，按[基于Docker的多容器PD分离服务部署指导](../docker/host/pd_disaggregation_deployment.md#启动服务)的打印结果部署即可（即Prefill命令放到 PR上，Decode命令放到DT上）。

### KV池化能力

通过MultiConnector将KV缓存卸载到共享池，支持跨实例复用，降低显存压力。

**特性原理**

允许P/D实例通过KV缓存池共享KV Cache，P实例将计算好的KV Cache推入缓存池，D实例从缓存池拉取并复用，从而在PD分离场景下提升显存利用率和推理吞吐。

**开启特性**

该特性详细文档请参考[KV池化能力](../../features/kv_cache_store/README.md)。

**关闭特性**

改用非MultiConnector的单一connector，并删除kv_cache_pool_config参数配置块。
