# D2D权重加载特性

## 特性介绍

D2D权重加载是MindIE Motor提供的模型权重启动加速能力。新实例启动时，可从集群内已就绪（ACTIVE）的同角色实例通过网络直接拉取权重，替代全量从磁盘加载，从而缩短启动时间。

### 工作原理

1. 在user_config.json的`engine_config`中开启D2D配置。
2. 首个实例无可用peer时，Engine从本地磁盘加载权重，并通过`listen_port`对外提供权重服务（seed模式）。
3. 后续实例：Controller自动发现同角色ACTIVE实例，收集peer IP（Peer发现路由，由Controller/NodeManager自动完成，无需手动填写peer IP）并下发给NodeManager；Engine以netloader方式从peer拉取对应分片权重。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 约束维度 | 要求 |
|----------|------|
| 硬件 | 内容缺失，需要人工补齐。 |
| 部署场景 | 内容缺失，需要人工补齐。 |
| 引擎 | 仅vLLM |
| 特性互斥 | 内容缺失，需要人工补齐。 |
| 软件依赖 | 内容缺失，需要人工补齐。 |
| 其他限制 | Peer匹配：仅匹配同角色（Prefill对Prefill、Decode对Decode等）且状态为ACTIVE的实例，排除自身。端口：`listen_port`需在集群网络内可达，且不与已有服务端口冲突。权重路径：首个实例（seed）仍需可访问本地模型权重目录；后续实例可依赖D2D拉取 |

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

**内容缺失，需要人工补齐**

### 使用场景

**内容缺失，需要人工补齐**

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

D2D权重加载特性通过user_config.json配置文件中的`engine_config`字段进行配置。Controller判定D2D权重加载特性开启需同时满足以下条件：

- odel_loader_extra_config 存在且为合法 JSON 对象；
- source参数配置为"auto"；
- 已配置listen_port参数。

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

   **依据原文上下文内容重组，请进行人工校验。**

   **表 1** 配置参数说明

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | source | 内容缺失，需要人工补齐。 | auto | 是 | 内容缺失，需要人工补齐。 | 固定为"auto"，表示peer地址由Controller自动填充。 |
   | listen_port | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 是 | 内容缺失，需要人工补齐。 | 本实例对外提供权重服务的起始端口；各device实际端口为`listen_port+device_rank`（含dp偏移）。 |
   | int8_cache | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 选填 | 不开启 | 是否启用INT8缓存，全量参数直传。 |
   | int8_cache_name | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 选填 | 内容缺失，需要人工补齐。 | INT8缓存名称。 |
   | output_prefix | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 选填 | 内容缺失，需要人工补齐。 | 权重输出前缀。 |

   >[!NOTE] 说明
   >
   >- 若模型启用了投机推理（如`speculative-config` / MTP），主模型与draft模型共用同一组`source`和`listen_port`配置，无需额外配置。draft权重服务端口会在`listen_port`基础上自动偏移10000，此时建议port配置 < 55535 - device_rank。
   >- 配置键名支持小写（如`listen_port`）或大写（如`LISTEN_PORT`），二者等价。

2. 部署首个实例。

   **内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

   在模型对应的user_config.json中按步骤1添加`model_loader_extra_config`（Prefill / Decode / Union按实际角色分别配置）。部署首个实例，等待实例进入ACTIVE状态。（**具体部署命令需要人工补齐。**）

3. 扩容或部署同角色新实例。

   扩容或部署同角色新实例时，Controller会自动向新实例下发peer IP。

4. 确认部署结果。

   **内容缺失，需要人工补齐。**

### 验证特性

**内容缺失，需要人工补齐。**

## 常见问题

**内容缺失，需要人工补齐。**
