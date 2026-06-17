# D2D 权重加载

D2D权重加载是 PyMotor 提供的**模型权重启动加速**能力。新实例启动时，可从集群内已就绪（ACTIVE）的同角色实例通过网络直接拉取权重，替代全量从磁盘加载，从而缩短启动时间。

当前仅 **vLLM** 引擎支持该特性。

## 工作原理

1. 在 `user_config.json` 的 `engine_config` 中开启 D2D 配置（见下文）。
2. **首个实例**：无可用 peer 时，Engine 从本地磁盘加载权重，并通过 `listen_port` 对外提供权重服务（seed 模式）。
3. **后续实例**：Controller 自动发现同角色 ACTIVE 实例，收集 peer IP 并下发给 NodeManager；Engine 以 `netloader` 方式从 peer 拉取对应分片权重。

Peer 发现与路由由 Controller / NodeManager 自动完成，**无需手动填写 peer IP**。

## 配置方法

在对应角色的 `motor_engine_*_config.engine_config` 中的 `model_loader_extra_config`下填写source及listen_port。以 Prefill 为例：

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

### 关键字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `source` | 是 | 固定为 `"auto"`，表示 peer 地址由 Controller 自动填充 |
| `listen_port` | 是 | 本实例对外提供权重服务的起始端口；各 device 实际端口为 `listen_port + device_offset` |

若模型启用了投机推理（如 `speculative-config` / MTP），主模型与 draft 模型共用同一组 `source` 和 `listen_port`配置，无需额外配置。draft 权重服务端口会在 `listen_port` 基础上**自动偏移 10000**。

### 可选字段

| 字段 | 说明 |
|------|------|
| `int8_cache` | 是否启用 INT8 缓存，由于当前把接收端反量化做在了接收权重之后，量化版权重建议配置"INT8_CACHE":"dram" |
| `int8_cache_name` | INT8 缓存名称 |
| `output_prefix` | 权重输出前缀 |

> 配置键名支持小写（如 `listen_port`）或大写（如 `LISTEN_PORT`），二者等价。

### 启用条件

Controller 判定 D2D 开启需同时满足：

- `model_loader_extra_config` 存在且为合法 JSON 对象
- `source == "auto"`
- `listen_port` 已配置

满足后，Engine 启动时会自动设置 `load_format = "netloader"`。

## 部署步骤

1. 在模型对应的 `user_config.json` 中按上文添加 `model_loader_extra_config`（Prefill / Decode / Union 按实际角色分别配置）。
2. 使用 [Deployer](../../deployer/README.md) 部署首个实例，等待实例进入 ACTIVE 状态。
3. 扩容或部署同角色新实例时，Controller 会自动向新实例下发 peer IP。

## 使用约束

- **引擎**：仅 vLLM。
- **Peer 匹配**：仅匹配**同角色**（Prefill 对 Prefill、Decode 对 Decode 等）且状态为 ACTIVE 的实例，排除自身。
- **端口**：`listen_port` 需在集群网络内可达，且不与已有服务端口冲突。
- **权重路径**：首个实例（seed）仍需可访问本地模型权重目录；后续实例可依赖 D2D 拉取。

## 已测试模型

以下模型已在 PyMotor 示例配置中验证 D2D 启动加速：

| 模型 | 配置目录 |
|------|----------|
| Qwen3-30B | `examples/infer_engines/vllm/models/qwen/3/30b/` |
| DeepSeek-V3.1 | `examples/infer_engines/vllm/models/deepseek/v3_1/` |

其他未测试模型如有问题，欢迎至官方提 [ISSUE](https://gitcode.com/Ascend/MindIE-PyMotor/issues)。
