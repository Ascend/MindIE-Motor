# max_tokens 自适应

## 功能介绍

`max_tokens` 自适应用于避免请求的输入 token 数与期望输出 token 数之和超过模型上下文窗口。
开启该功能后，Coordinator 在请求路由前使用模型 tokenizer 计算实际输入 token 数，并在必要时
缩小请求中的 `max_tokens` 或 `max_completion_tokens`，再将调整后的请求转发给推理引擎。

该功能具有以下特点：

- 对 `load_balance`、`round_robin` 和 `kv_cache_affinity` 三种调度策略均生效。
- 仅在客户端请求的输出 token 上限超出剩余上下文时进行裁剪，不会增大客户端指定的值。
- 默认关闭。关闭时 Coordinator 不修改请求，由后端推理引擎按自身规则校验上下文长度。
- 不依赖 KV Cache 亲和调度，也不要求部署 kv-conductor 服务。

## 工作原理

Coordinator 收到推理请求后，先完成 tokenization，再按以下规则计算实际输出上限：

```text
模型上下文上限 = min(p_max_seqlen, d_max_seqlen)
剩余上下文     = 模型上下文上限 - 输入 token 数
实际输出上限   = min(客户端请求的输出上限, 剩余上下文)
```

`p_max_seqlen` 或 `d_max_seqlen` 只有一个有效正整数时，使用有效值；两者均有效时取较小值，
确保请求同时满足 Prefill 和 Decode 实例的上下文约束。

例如，Prefill 和 Decode 的最大序列长度分别为 `131072` 和 `114688`，请求经 tokenizer 计算后
包含 `100000` 个输入 token，客户端指定 `max_tokens: 20000`：

```text
模型上下文上限 = min(131072, 114688) = 114688
剩余上下文     = 114688 - 100000 = 14688
实际 max_tokens = min(20000, 14688) = 14688
```

Coordinator 会将该请求的 `max_tokens` 修改为 `14688` 后再进行调度和转发。

### 参数选择规则

| 请求形式 | 参与裁剪的参数 |
|---------|---------------|
| Chat 请求包含有效的 `max_completion_tokens` | `max_completion_tokens` |
| Chat 请求未提供有效的 `max_completion_tokens`，但提供有效的 `max_tokens` | `max_tokens` |
| Completion 请求 | `max_tokens` |

当 Chat 请求同时携带 `max_completion_tokens` 和 `max_tokens` 时，只调整优先级更高的
`max_completion_tokens`，不修改 `max_tokens`。

### Token 计算

- Chat 请求使用模型的 chat template 计算 token，并包含 `messages`、`tools`、
  `chat_template_kwargs`、`reasoning_effort` 等会影响引擎输入的字段。
- Completion 请求支持字符串 `prompt` 和 token ID 列表形式的 `prompt`。
- 同一个请求计算得到的 token ID 会被复用，避免在上下文裁剪与 KV Cache 亲和调度之间重复计算。

## 前置条件

- Prefill（PD 分离）或 Union（PD 混部）引擎配置中的 `model` 必须指向 Coordinator
  可访问的模型目录，用于加载与推理引擎一致的 tokenizer。
- 必须提供有效的上下文上限。PD 分离配置会自动将 Prefill、Decode 引擎的 `max_model_len`
  分别填充为 `aigw.p_max_seqlen`、`aigw.d_max_seqlen`；无法自动填充时需要显式配置 `aigw`。
- 启用该功能时，每个 Coordinator Inference worker 会在启动阶段初始化进程内的
  `TokenizerManager` 单例，并从模型目录加载 tokenizer；不会创建独立的 tokenizer 服务进程。
  模型目录不可访问或 tokenizer 加载失败时，对应 Inference worker 将无法正常启动。

## 配置方法

在 `user_config.json` 的 `motor_coordinator_config` 中将
`context_budget_mode` 设置为 `on`。以下示例仅展示相关配置：

```json
{
  "motor_coordinator_config": {
    "context_budget_mode": "on",
    "scheduler_config": {
      "scheduler_type": "load_balance"
    }
  },
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "engine_config": {
      "model": "/mnt/weight/your-model",
      "served_model_name": "your-model",
      "max_model_len": 131072
    }
  },
  "motor_engine_decode_config": {
    "engine_type": "vllm",
    "engine_config": {
      "model": "/mnt/weight/your-model",
      "served_model_name": "your-model",
      "max_model_len": 114688
    }
  }
}
```

部署流程会从 Prefill（PD 分离）或 Union（PD 混部）引擎配置自动获取 tokenizer 的
`model_path`；PD 分离场景还会从 Prefill、Decode 引擎配置获取上下文上限。无需为该功能
启用 `kv-events-config` 或配置 `conductor_service`。

PD 混部或其他无法同时提取 Prefill、Decode 上下文上限的场景，需要在
`motor_coordinator_config.aigw` 中显式配置。例如 Union 引擎的最大序列长度为 `114688`：

```json
{
  "motor_coordinator_config": {
    "context_budget_mode": "on",
    "aigw": {
      "p_max_seqlen": 114688,
      "d_max_seqlen": 114688
    }
  }
}
```

配置项说明如下：

| 配置项 | 取值 | 说明 |
|-------|------|------|
| `motor_coordinator_config.context_budget_mode` | `off` | 默认值，不修改请求中的输出 token 上限 |
| `motor_coordinator_config.context_budget_mode` | `on` | 按模型剩余上下文裁剪输出 token 上限 |
| Prefill 或 Union 的 `engine_config.model` | 模型目录 | Inference worker 中 `TokenizerManager` 加载 tokenizer 的路径 |
| `motor_engine_prefill_config.engine_config.max_model_len` | 正整数 | Prefill 端最大序列长度 |
| `motor_engine_decode_config.engine_config.max_model_len` | 正整数 | Decode 端最大序列长度 |

完整配置字段请参考[全量配置参数说明](../configuration/config_reference.md)。

## 验证功能

发送一个输出上限大于模型剩余上下文的请求：

```bash
curl -X POST http://{coordinator-ip}:1025/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "your-model",
    "messages": [{"role": "user", "content": "your-long-prompt"}],
    "max_tokens": 20000
  }'
```

发生裁剪时，Coordinator 会输出如下 INFO 日志：

```text
Context budget clamped req_id=<request-id> parameter=max_tokens requested=20000 effective=14688 prompt_tokens=100000 max_model_len=114688
```

各字段含义如下：

| 字段 | 说明 |
|------|------|
| `parameter` | 本次调整的请求参数 |
| `requested` | 客户端请求的输出 token 上限 |
| `effective` | Coordinator 实际转发给引擎的输出 token 上限 |
| `prompt_tokens` | Coordinator 使用模型 tokenizer 计算的输入 token 数 |
| `max_model_len` | Prefill、Decode 上下文上限中的较小值 |

如果请求的输出上限未超过剩余上下文，则不会打印裁剪日志，请求参数保持不变。

## 使用限制

- 该功能只调整有效的正整数 `max_tokens` 或 `max_completion_tokens`。请求未携带这些参数，
  或参数不是正整数时，Coordinator 不做调整。
- 请求中携带非正整数（如 0、负数、布尔、非 int 类型）的 `max_tokens` 或 `max_completion_tokens`
  时，Coordinator 不会返回 400，而是移除该参数并记录 WARNING 日志，请求按未携带该参数处理
  （即按模型默认输出上限继续执行）。如需感知此类配置错误，请检查 Coordinator 日志中的
  `Invalid max_tokens=` / `Invalid max_completion_tokens=` 告警。
- 当输入 token 数已经达到或超过模型上下文上限时，Coordinator 不修改输出上限，
  请求仍由后端推理引擎完成上下文校验并返回对应错误。
- `TokenizerManager` 无法得到有效 token ID 时，Coordinator 不根据估算值裁剪请求，
  而是保留原始输出上限并交由后端推理引擎校验。
- Prefill 和 Decode 应使用兼容的 tokenizer。两端 `max_model_len` 不一致时，功能始终按较小值裁剪。

## 常见问题

### 开启后 Coordinator 启动失败

检查 Prefill 引擎配置是否包含有效的 `model` 和 `max_model_len`，Decode 引擎配置是否包含
有效的 `max_model_len`，并确认 Coordinator 容器能够访问模型目录。启动配置校验要求能够获得
tokenizer 路径，以及至少一个有效的 `p_max_seqlen` 或 `d_max_seqlen`。

### 请求没有发生裁剪

依次检查：

1. `motor_coordinator_config.context_budget_mode` 是否为 `on`。
2. 请求是否携带有效的正整数 `max_tokens` 或 `max_completion_tokens`。
3. 客户端指定的输出上限是否确实大于“模型上下文上限减去输入 token 数”。
4. Inference worker 日志中 `TokenizerManager` 是否完成初始化，以及 Coordinator 是否能够使用模型
   tokenizer 处理当前请求。

### 为什么没有将超长输入的 max_tokens 改为 1

当输入本身已经用完模型上下文时，不存在可用的正数输出预算。Coordinator 不会通过把
`max_tokens` 强制设为 `1` 来掩盖超长输入，而是保留原请求并由后端推理引擎返回上下文长度错误。
