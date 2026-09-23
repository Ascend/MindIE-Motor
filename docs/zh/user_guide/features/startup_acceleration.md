# D2D权重加载特性

## 特性介绍

D2D权重加载是MindIE Motor提供的模型权重启动加速能力。新实例启动时，可从集群内已就绪（ACTIVE）的同角色实例通过网络直接拉取权重，替代全量从磁盘加载，从而缩短启动时间。

### 工作原理

D2D权重加载特性的工作原理大致分为以下三步：

1. 在user_config.json的`engine_config`中开启D2D配置。
2. 首个实例无可用peer时，Engine从本地磁盘加载权重，并通过`listen_port`对外提供权重服务（seed模式）。
3. 对于后续实例，Controller自动发现**同角色**ACTIVE实例，收集peer IP（Peer发现路由，由Controller/NodeManager自动完成，无需手动填写peer IP）并下发给NodeManager；Engine以netloader方式从peer拉取对应分片权重。

### 约束与限制

| 约束维度 | 要求 |
|----------|------|
| 硬件 | Atlas 800I A2推理服务器、Atlas 800I A3超节点服务器 |
| 部署场景 | PD分离服务部署、PD混部服务部署 |
| 引擎 | 仅vLLM |
| 特性互斥 | 无 |
| 软件依赖 | vLLM Ascend版本需大于等于27.1 |
| 其他限制 | <ul><li>Peer匹配：仅匹配同角色（Prefill对Prefill、Decode对Decode等）且状态为ACTIVE的实例，排除自身。</li><li>端口：`listen_port`需在集群网络内可达，且不与已有服务端口冲突。</li><li>权重路径：首个实例（seed）仍需可访问本地模型权重目录；后续实例可依赖D2D拉取。</li></ul> |

以下模型已在MindIE Motor示例配置中验证D2D启动加速：

| 模型 | 配置目录(参考) |
|------|----------|
| Qwen3-30B | examples/infer_engines/vllm/models/qwen_235b/ |
| DeepSeek-V3.1-w8a8-mtp | examples/infer_engines/vllm/models/deepseek_v3.1/ |
| DeepSeek-V4-Flash-w8a8-mtp | examples/infer_engines/vllm/models/deepseek_v4_flash/ |
| DeepSeek-V4-Pro-w4a8 | examples/infer_engines/vllm/models/deepseek_v4_pro/ |
| GLM-5.1-w4a8 | examples/infer_engines/vllm/models/glm_5.1/ |
| GLM-5.1-w8a8 | examples/infer_engines/vllm/models/glm_5.1/ |

## 特性使用

### 环境准备

已按照 [PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md) 或 [PD 混部服务部署](../deployment/k8s/pd_aggregation_deployment.md) 完成服务部署，并且服务运行正常。后续所有操作只在 K8s 集群的管理节点（master 节点）执行。

### 使用场景

- 故障重调度场景：初始实例拉起后若发生故障，重调度时将自动从**同角色**的ACTIVE实例拉取权重。
- 实例扩容场景：已有P/D/U实例部署完毕正常运行时，若需要扩容，扩容节点将自动从**同角色**的ACTIVE实例拉取权重，无需从磁盘冷加载权重。

两种场景共用同一套配置，操作步骤相同：

- 故障重调度场景由Controller自动触发，无需额外操作。
- 实例扩容场景需执行扩容命令。

### 使用样例

D2D权重加载特性通过user_config.json配置文件中的`engine_config`字段进行配置。Controller判定D2D权重加载特性开启需同时满足以下条件：

- model_loader_extra_config 存在且为合法 JSON 对象；
- source参数配置为"auto"；
- 配置listen_port参数。

满足后，Engine启动时会自动设置`load_format = "netloader"`。

**操作步骤**

1. 修改user_config.json配置文件。

   在对应角色的`motor_engine_*_config.engine_config`中的`model_loader_extra_config`下填写`source`及`listen_port`。以Prefill为例：

   ```json
   {
     "motor_engine_prefill_config": {
       "engine_type": "vllm",
       "engine_config": {
         "model": "/data01/models/DeepSeek-V3.1",
         "model_loader_extra_config": {
           "source": "auto",
           "listen_port": 10000
         }
       }
     }
   }
   ```

   **表 1** 配置参数说明

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | source | string | auto | 是 | 无 | 固定为"auto"，表示peer地址由Controller自动填充。 |
   | listen_port | int | 端口号 | 是 | 无 | 本实例对外提供权重服务的起始端口；各device实际端口为`listen_port+device_rank`（含dp偏移）。 |
   | int8_cache | string | ["hbm", "dram", "no"] | 选填 | 不开启 | 是否启用INT8缓存，全量参数直传。 |
   | int8_cache_name | list | INT8缓存名称 | 选填 | None | vLLM Ascend内部int8参数名称，若配置，d2d将只传输对应int8参数。默认不过滤，全量参数传输。|
   | output_prefix | string | 文件名前缀 | 选填 | None | 若设置，每个rank将产出{OUTPUT_PREFIX}{RANK}.txt，内容为每个rank的IP:Port配对信息。|

   >[!NOTE] 说明
   >
   >- 若模型启用了投机推理（如`speculative-config` / MTP），主模型与draft模型共用同一组`source`和`listen_port`配置，无需额外配置。draft权重服务端口会在`listen_port`基础上自动偏移10000，此时建议port配置 < 55535 - device_rank。
   >- 配置键名支持小写（如`listen_port`）或大写（如`LISTEN_PORT`），二者等价。

2. 部署首个实例。

   按照 [PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md) 相关文档获取 P/D 分离推理服务部署配置，在对应的user_config.json中按步骤1添加`model_loader_extra_config`（Prefill / Decode / Union按实际角色分别配置）。

   执行以下命令部署首个实例，等待实例进入ACTIVE状态：

   ```bash
   python3 deploy.py --config_dir .../examples/infer_engines/vllm/models/...
   ```

   >[!NOTE] 说明
   >
   >`--config_dir`为`user_config.json`与`env.json`所在目录。上述`.../examples/infer_engines/vllm/models/...`为路径样例，需替换为实际使用的引擎与模型对应的配置目录（可参考本文档“已测试模型”表中的配置目录）。

3. 扩容或部署同角色新实例。

   扩容或部署同角色新实例时，Controller会自动向新实例下发peer IP。

   可通过如下命令执行扩容：

   ```bash
   python3 deploy.py --config_dir .../examples/infer_engines/vllm/models/... --update_instance_num
   ```

4. <a id="step004"></a>确认部署结果。

   日志中有如下打印，则已启动对外服务，后续新实例可从本实例拉取权重。

   ```text
   [netloader.py:664] Start elastic Netloader server, rank: 3, listen port: 10.244.66.130:12348, group: netloader
   [netloader.py:708] Elastic server started, rank: 3, group: netloader
   ```

### 验证特性

新实例启动时将自动从seed实例拉取权重。

```text
[netloader.py:514] Netloader manifest build time: 0.8685483103618026, rank: 6, manifest=2682
[elastic.py:136] Start connection to server: 10.244.66.130:12351
[elastic.py:138] Finish connection to server: 10.244.66.130:12351
[elastic.py:252] Receive ack: label=JOIN_ACK name=10.244.169.204:38392 transfer_shape_count=2682
[elastic_load.py:505] [netloader_p2p] HCCL recv time: 3.1666774908080697, transfer=2682 group=netloader processed_layout=True addr=10.244.66.130:58045
[load.py:90] Netloader P2P load time: 3.5929042901843786, device_id: 6, group: netloader
[netloader.py:664] Start elastic Netloader server, rank: 6, listen port: 10.244.169.204:12351, group: netloader
[netloader.py:708] Elastic server started, rank: 6, group: netloader
```

## 常见问题

### 端口冲突导致Elastic server启动失败

**问题描述**

Elastic server启动失败，该实例后续无法作为seed实例供给其他实例拉取权重。

**原因分析**

`listen_port`（各device实际端口为`listen_port+device_rank`，含dp偏移）与集群内已有服务端口冲突。

**解决步骤**

1. 参考本文档“约束与限制”中的端口要求，修改`model_loader_extra_config.listen_port`，避免与已有服务端口冲突。
2. 重新部署实例，确认日志中出现`Elastic server started`，日志详情请参见[步骤4](#step004)。

### 新模型HCCL传输长时间卡住或回退为从磁盘加载

**问题描述**

测试新模型时出现HCCL传输长时间卡住，或特性执行失败fallback到从磁盘加载权重。

**原因分析**

本特性与模型权重强相关，未验证的模型可能与D2D权重传输存在兼容性问题。

**解决步骤**

携带模型名称与实例日志，至官方社区提[ISSUE](https://gitcode.com/Ascend/MindIE-Motor/issues)。
