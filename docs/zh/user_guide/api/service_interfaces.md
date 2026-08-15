# 业务接口

## OpenAI Chat Completion 接口

**接口功能**

提供与 OpenAI `v1/chat/completions` 兼容的对话生成入口，用于多轮对话、角色设定与上下文续写。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{IP}:{Port}/v1/chat/completions`

IP与端口参见[业务接口的IP/端口与配置](./README.md#业务接口的ip端口与配置)

**请求参数**

| 参数名 | 类型 | 说明 |
|---|---|---|
| model | string | 必选；模型名称。 |
| messages | array | 必选；对话消息列表。 |
| stream | boolean | 可选；是否流式输出，默认为false。<ul><li>true：流式;</li><li>false：非流式。</li></ul> |

`messages`参数中的子参数解释如下：

| 参数名 | 类型 | 说明 |
|---|---|---|
| role | string | 必选；角色如下：<ul><li>`system`：系统/规则提示；</li><li>`user`：用户输入；</li><li>`assistant`：助手输出。</li></ul>对话中通常以 `system` 作为最高优先级约束，`user` 为问题内容，`assistant` 为历史回答。 |
| content | string | 必选；消息内容。 |

其余OpenAI兼容字段（如`max_tokens`、`temperature`等）将透传给后端推理引擎。常用参数说明如下：

| 参数 | 类型 | 说明 |
|---|---|---|
| max_tokens | integer | 最大生成token数上限。 |
| temperature | number | 采样温度，温度越大输出结果越随机（常用 0~1），温度过大可能导致输出不稳定。 |
| top_p | number | nucleus采样阈值（0~1），与`temperature` 参数同时使用时通常取其一。 |
| presence_penalty | number | 话题惩罚项，鼓励引入新内容；取值区间依后端实现。 |
| frequency_penalty | number | 频率惩罚项，降低重复内容；取值区间依后端实现。 |
| stop | string/array | 停止词，匹配后提前结束生成。 |

>[!NOTE]说明
>未被后端推理引擎支持的字段会被忽略或降级处理，具体能力以实际模型与后端版本为准。

**使用样例**

- 流式使用样例：

  ```bash
  curl -N -X POST "http://{IP}:{Port}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer {API_KEY}" \
    -d '{
      "model": "qwen3",
      "messages": [
        { "role": "user", "content": "Hello there!" }
      ],
      "stream": true
    }'
  ```

- 非流式使用样例：

  ```bash
  curl -X POST "http://{IP}:{Port}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer {API_KEY}" \
    -d '{
      "model": "qwen3",
      "messages": [
        { "role": "system", "content": "You are a helpful assistant." },
        { "role": "user", "content": "Hi!" }
      ],
      "temperature": 0.7
    }'
  ```

**响应样例**

- 流式响应样例：

  ```text
  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"Hey there! "},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"Great to see you. "},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"How can I help today?"},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

  data: [DONE]
  ```

- 非流式响应示例：

  ```JSON
  {
    "id": "chatcmpl-xxx",
    "object": "chat.completion",
    "created": 1765856304,
    "model": "qwen3",
    "choices": [
      {
        "index": 0,
        "message": { "role": "assistant", "content": "Hey there! Great to see you—how can I help today?" },
        "finish_reason": "stop"
      }
    ]
  }
  ```

**输出说明**

- 流式响应参数说明：

  流式响应为SSE事件流，每行以`data:`开头，`data: [DONE]`表示结束。`data:`后的JSON结构如下：

  | 参数名 | 类型 | 说明 |
  |---|---|---|
  | id | string | 本次请求ID。 |
  | object | string | 返回对象类型：`chat.completion.chunk`。 |
  | created | integer | 创建时间戳（秒）。 |
  | model | string | 模型名称。 |
  | choices | array | 增量结果列表。 |
  | choices[].index | integer | 序号。 |
  | choices[].delta | object | 增量内容。 |
  | choices[].delta.role | string | 仅首包可能返回`assistant`。 |
  | choices[].delta.content | string | 生成增量文本。 |
  | choices[].finish_reason | string/null | 结束原因，未结束时为`null`。 |

- 非流式响应参数说明：

  | 参数名 | 类型 | 说明 |
  |---|---|---|
  | id | string | 本次请求ID。 |
  | object | string | 返回对象类型：`chat.completion`。 |
  | created | integer | 创建时间戳（秒）。 |
  | model | string | 模型名称。 |
  | choices | array | 生成结果列表。 |
  | choices[].index | integer | 序号。 |
  | choices[].message | object | 生成消息。 |
  | choices[].message.role | string | 角色，固定为`assistant`。 |
  | choices[].message.content | string | 生成内容。 |
  | choices[].finish_reason | string/null | 结束原因，如`stop`、`length`等。 |

## OpenAI Completion 接口

**接口功能**

OpenAI Completion 兼容接口，支持文本补全与结果采样。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{IP}:{Port}/v1/completions`

IP与端口参见[业务接口的IP/端口与配置](./README.md#业务接口的ip端口与配置)

**请求参数**

| 参数名 | 类型 | 说明 |
|---|---|---|
| model | string | 必选；模型名称。 |
| prompt | string/array | 必选；提示词。 |
| stream | boolean | 可选；是否流式输出，默认为false。<ul><li>true：流式;</li><li>false：非流式。</li></ul> |

其余OpenAI兼容字段（如`max_tokens`、`temperature`等）将透传给后端推理引擎。常用字段说明同上，若后端不支持部分字段将被忽略或降级处理。

**使用样例**

- 流式使用样例：

  ```bash
  curl -N -X POST "http://{IP}:{Port}/v1/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer {API_KEY}" \
    -d '{
      "model": "qwen3",
      "prompt": "Hello!",
      "stream": true
    }'
  ```

- 非流式使用样例：

  ```bash
  curl -X POST "http://{IP}:{Port}/v1/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer {API_KEY}" \
    -d '{
      "model": "qwen3",
      "prompt": "Hi!",
      "max_tokens": 64,
      "temperature": 0.7
    }'
  ```

**响应示例**

- 流式响应样例：

  ```text
  data: {"id":"cmpl-xxx-0","object":"text_completion","created":1765856304,"model":"qwen3","choices":[{"index":0,"text":"Hey there! ","finish_reason":null}]}

  data: {"id":"cmpl-xxx-0","object":"text_completion","created":1765856304,"model":"qwen3","choices":[{"index":0,"text":"Lovely to hear from you—","finish_reason":null}]}

  data: {"id":"cmpl-xxx-0","object":"text_completion","created":1765856304,"model":"qwen3","choices":[{"index":0,"text":"what would you like to do today?","finish_reason":null}]}

  data: {"id":"cmpl-xxx-0","object":"text_completion","created":1765856304,"model":"qwen3","choices":[{"index":0,"text":"","finish_reason":"stop"}]}

  data: [DONE]
  ```

- 非流式响应样例（非流式）：

  ```JSON
  {
    "id": "cmpl-xxx-0",
    "object": "text_completion",
    "created": 1765856304,
    "model": "qwen3",
    "choices": [
      { "index": 0, "text": "Hey there! Lovely to hear from you—what would you like to do today?", "finish_reason": "stop" }
    ]
  }
  ```

**输出说明**

- 流式响应参数说明：

  流式响应为SSE事件流，每行以`data:` 开头，`data: [DONE]`表示结束。`data:`后的JSON结构如下：

  | 参数名 | 类型 | 说明 |
  |---|---|---|
  | id | string | 本次请求ID。 |
  | object | string | 返回对象类型：`text_completion`。 |
  | created | integer | 创建时间戳（秒）。 |
  | model | string | 模型名称。 |
  | choices | array | 增量结果列表。 |
  | choices[].index | integer | 序号。 |
  | choices[].text | string | 生成增量文本。 |
  | choices[].finish_reason | string/null | 结束原因，未结束时为`null`。 |

- 非流式响应参数说明：

  | 参数名 | 类型 | 说明 |
  |---|---|---|
  | id | string | 本次请求ID。 |
  | object | string | 返回对象类型：`text_completion`。 |
  | created | integer | 创建时间戳（秒）。 |
  | model | string | 模型名称。 |
  | choices | array | 生成结果列表。 |
  | choices[].index | integer | 序号。 |
  | choices[].text | string | 生成文本。 |
  | choices[].finish_reason | string/null | 结束原因，如`stop`、`length`等。 |

## Anthropic Messages 接口

**接口功能**

提供与 Anthropic Messages API 兼容的对话生成入口，支持多模态图片输入、工具调用、思考链（thinking）内容块。请求由 Coordinator 进行协议感知转发：实例 business_port 上的 engine_server 原生提供 `/v1/messages` 路由（基于 vLLM 的 Anthropic serving 实现），完整的 Anthropic 协议端到端保留，PD 调度、故障重试等能力按 Anthropic 协议形状适配（详见文末"Anthropic 接口特性支持矩阵"）。仅 vLLM 后端模式可用。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{IP}:{Port}/v1/messages`

IP与端口参见[业务接口的IP/端口与配置](./README.md#业务接口的ip端口与配置)

**请求参数**

| 参数名 | 类型 | 说明 |
|---|---|---|
| model | string | 必选；模型名称。 |
| messages | array | 必选；对话消息列表，每条消息包含 `role`（`user` 或 `assistant`）和 `content`（字符串或内容块数组）。 |
| max_tokens | integer | 必选；最大生成 token 数。 |
| stream | boolean | 可选；是否流式输出（SSE），默认为 `false`。 |
| system | string/array | 可选；系统提示词，支持字符串或内容块数组格式。 |
| stop_sequences | array | 可选；停止序列列表。 |
| temperature | number | 可选；采样温度（0~1）。 |
| top_p | number | 可选；nucleus 采样阈值。 |
| top_k | integer | 可选；top-k 采样参数。 |
| tools | array | 可选；工具定义列表，每条工具包含 `name`、`description`、`input_schema`。 |
| tool_choice | object | 可选；工具选择策略，`type` 取值：`auto`、`any`、`tool`、`none`。 |

**使用样例**

- 基本文本对话（非流式）：

  ```bash
  curl -X POST "http://{IP}:{Port}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: {API_KEY}" \
    -d '{
      "model": "qwen3-8b",
      "messages": [
        { "role": "user", "content": "Hello!" }
      ],
      "max_tokens": 100
    }'
  ```

- 流式对话：

  ```bash
  curl -N -X POST "http://{IP}:{Port}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: {API_KEY}" \
    -d '{
      "model": "qwen3-8b",
      "messages": [
        { "role": "user", "content": "Explain quantum computing in simple terms." }
      ],
      "max_tokens": 200,
      "stream": true
    }'
  ```

- 带系统提示词：

  ```bash
  curl -X POST "http://{IP}:{Port}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: {API_KEY}" \
    -d '{
      "model": "qwen3-8b",
      "messages": [
        { "role": "user", "content": "What is the weather?" }
      ],
      "max_tokens": 100,
      "system": "You are a helpful weather assistant."
    }'
  ```

- 带工具调用：

  ```bash
  curl -X POST "http://{IP}:{Port}/v1/messages" \
    -H "Content-Type: application/json" \
    -H "x-api-key: {API_KEY}" \
    -d '{
      "model": "qwen3-8b",
      "messages": [
        { "role": "user", "content": "What is the weather in Beijing?" }
      ],
      "max_tokens": 100,
      "tools": [
        { "name": "get_weather", "description": "Get current weather", "input_schema": { "type": "object" } }
      ],
      "tool_choice": { "type": "auto" }
    }'
  ```

**响应样例**

- 非流式响应样例：

  ```JSON
  {
    "id": "msg_xxx",
    "type": "message",
    "role": "assistant",
    "content": [
      { "type": "text", "text": "Hello! How can I help you today?" }
    ],
    "model": "qwen3-8b",
    "stop_reason": "end_turn",
    "usage": {
      "input_tokens": 10,
      "output_tokens": 8
    }
  }
  ```

- 流式响应样例（SSE 事件流）：

  ```text
  event: message_start
  data: {"type":"message_start","message":{"id":"msg_xxx","type":"message","role":"assistant","content":[],"model":"qwen3-8b","usage":{"input_tokens":10}}}

  event: content_block_start
  data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

  event: content_block_delta
  data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello! "}}

  event: content_block_delta
  data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"How can I help?"}}

  event: content_block_stop
  data: {"type":"content_block_stop","index":0}

  event: message_delta
  data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":8}}

  event: message_stop
  data: {"type":"message_stop"}

  data: [DONE]
  ```

**输出说明**

- 非流式响应参数说明：

  | 参数名 | 类型 | 说明 |
  |---|---|---|
  | id | string | 本次请求 ID。 |
  | type | string | 返回对象类型：`message`。 |
  | role | string | 角色，固定为 `assistant`。 |
  | content | array | 内容块列表。 |
  | content[].type | string | 内容块类型：`text`、`tool_use`、`thinking` 等。 |
  | content[].text | string | 文本内容（当 type 为 `text` 时）。 |
  | model | string | 模型名称。 |
  | stop_reason | string | 结束原因：`end_turn`（正常结束）、`max_tokens`（达到上限）、`tool_use`（工具调用）。 |
  | usage | object | Token 统计，包含 `input_tokens`、`output_tokens`。 |

- 流式响应事件类型说明：

  | 事件类型 | 说明 |
  |---|---|
  | `message_start` | 消息开始，包含元数据和初始 usage。 |
  | `content_block_start` | 内容块开始，标记一个新的 text / tool_use / thinking 块。 |
  | `content_block_delta` | 内容块增量，携带 `text_delta`、`input_json_delta`、`thinking_delta` 等。 |
  | `content_block_stop` | 内容块结束。 |
  | `message_delta` | 消息增量，包含 `stop_reason` 和最终 usage。 |
  | `message_stop` | 消息结束。 |

## Anthropic Count Tokens 接口

**接口功能**

提供与 Anthropic `messages/count_tokens` 兼容的 Token 计数入口，用于预估输入消息的 Token 消耗。请求由 Coordinator 以单实例轻量调度转发至 engine_server 的 `/v1/messages/count_tokens` 路由：不经过 PD 分离调度、不改写任何生成参数、不触发 prefill。仅 vLLM 后端模式可用。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{IP}:{Port}/v1/messages/count_tokens`

IP与端口参见[业务接口的IP/端口与配置](./README.md#业务接口的ip端口与配置)

**请求参数**

| 参数名 | 类型 | 说明 |
|---|---|---|
| model | string | 必选；模型名称。 |
| messages | array | 必选；消息列表，格式同 `/v1/messages`。 |
| system | string/array | 可选；系统提示词。 |
| tools | array | 可选；工具定义。 |
| tool_choice | object | 可选；工具选择策略。 |

**使用样例**

```bash
curl -X POST "http://{IP}:{Port}/v1/messages/count_tokens" \
  -H "Content-Type: application/json" \
  -H "x-api-key: {API_KEY}" \
  -d '{
    "model": "qwen3-8b",
    "messages": [
      { "role": "user", "content": "Hello!" }
    ]
  }'
```

**响应样例**

```JSON
{
  "input_tokens": 10,
  "context_management": {
    "original_input_tokens": 10
  }
}
```

**输出说明**

| 参数名 | 类型 | 说明 |
|---|---|---|
| input_tokens | integer | 输入 Token 数量。 |
| context_management | object | 上下文管理信息。 |
| context_management.original_input_tokens | integer | 原始输入 Token 数量。 |

---

## Anthropic 接口特性支持矩阵

`/v1/messages` 与 `/v1/messages/count_tokens` 与 PyMotor 各部署特性叠加时的行为如下。未列出的行为与 Anthropic 官方协议一致。

| 特性 | `/v1/messages` | `/v1/messages/count_tokens` | 说明 |
|---|---|---|---|
| 流式输出（SSE） | 支持 | 不涉及 | `event:`/`data:` 帧端到端保留，Coordinator 流处理不丢弃、不改写任何帧（与 `reschedule_enabled` 配置无关）。 |
| PD 分离（并发模式） | 支持 | 不涉及 | 发往 Prefill 实例的请求按 `stream=false`、`max_tokens=1` 改写（字段为 Anthropic 原生字段），Decode 实例通过 `kv_transfer_params` 完成 KV 传输。 |
| PD 分离（交接/CPCD 模式） | 支持 | 不涉及 | Prefill 实例短路返回 `PrefillResult`，Decode 实例经 `_motor_prefill_result` 获得 KV bootstrap。 |
| PDHybrid 混合部署与回退 | 支持 | 支持 | decode 池不可用时可回退单实例完整响应，协议形状保持不变。 |
| 故障重调度（`reschedule_enabled`，默认开启） | 降级支持 | 不涉及 | 提交前失败：以原始请求体重试（可能重复一次 prefill）；提交后中流失败：流终止并返回 Anthropic 错误事件。**不支持断流续传**（不注入 `return_token_ids`，不做 token 回放）。 |
| 精度采样（`precision_check_enabled`） | 不覆盖 | 不涉及 | Anthropic 流量不注入 `logprobs`/`return_token_ids` 等采样字段（请求级 debug 日志记录跳过），精度检测对 Anthropic 流量不生效。 |
| EPD 多模态部署 | 支持 | 不涉及 | `messages[].content[]` 中 `type=image` 的内容块会正确触发 Encode 实例调度。 |
| 引擎侧重计算（recompute） | 支持 | 不涉及 | 引擎内部重计算（如 KV 池不一致、KV 拉取失败）对客户端呈现为 `stop_reason=end_turn`，重计算事件记录于引擎日志，不会透出非法的 `stop_reason` 值或 `null`。 |
| 错误响应格式 | Anthropic 错误信封 | Anthropic 错误信封 | HTTP 错误为 `{"type": "error", "error": {"type": ..., "message": ...}}`；已提交流的中流错误为单个 `event: error` 帧（无 `[DONE]`）。引擎返回的 Anthropic 错误体原样透传。 |
| 调度方式 | 按拓扑调度（PD/混合） | 单实例轻量调度 | count_tokens 不经过 PD 分离调度、不附加 `_motor_dispatch`、不注入任何生成参数、不触发 prefill。 |
| 缓存命中明细（`usage.cache_read_input_tokens` / `cache_creation_input_tokens`） | 支持 | 不涉及 | 需引擎开启 `enable_prompt_tokens_details`。非流式响应与流式最终 `message_delta` 携带缓存字段：`cache_read_input_tokens` 为缓存命中 token 数，`input_tokens` 为新计算 token 数，`cache_creation_input_tokens` 恒为 0（vLLM 无 cache creation 概念）。并发调度模式下 `message_start` 的 usage 由 Coordinator 合并 Prefill 实例的缓存信息；trigger/metaserver 模式下 `message_start` 省略该可选字段。引擎未输出缓存明细时字段整体省略（不为 `null`）。 |

---

## Anthropic 接口字段支持明细

以下对照 Anthropic 官方 Messages API 逐项说明 `/v1/messages` 的字段支持情况。协议未声明的字段按 pydantic `extra=ignore` 规则**静默忽略**（不报错、不生效）。

### 请求字段

| 字段 | 支持情况 | 说明 |
|---|---|---|
| model | 支持（必选） | |
| messages | 支持（必选） | `role` 为 `user`/`assistant`；`content` 为字符串或内容块数组。 |
| messages[].content `text` 块 | 支持 | |
| messages[].content `image` 块 | 支持 | `source` 支持 `base64` 与 `url` 两种形式，内部转换为 OpenAI `image_url`；EPD 部署下正确触发 Encode 实例调度。 |
| messages[].content `tool_use` 块 | 支持 | assistant 消息回传的工具调用，内部转换为 OpenAI `tool_calls`。 |
| messages[].content `tool_result` 块 | 支持 | 转换为 `tool` 角色消息，内容支持文本与图片。 |
| messages[].content `thinking` 块 | 支持 | 回传的思考内容并入推理上下文。 |
| messages[].content `redacted_thinking` 块 | 容忍 | 内容不透明（base64），接收但忽略，保证全量回传时不出校验错。 |
| max_tokens | 支持（必选） | 必须为正整数。 |
| system | 支持 | 字符串或内容块数组；注入 prompt，PD 部署下 Prefill/Decode 实例渲染一致。 |
| stop_sequences | 支持（有降级） | 停止功能生效；但命中时 `stop_reason` 恒为 `end_turn`（见响应字段说明）。 |
| stream | 支持 | |
| temperature / top_p / top_k | 支持 | |
| tools | 支持（需引擎参数） | 需引擎启动参数 `--enable-auto-tool-choice` 与 `--tool-call-parser`（如 `hermes`），否则返回 400（`invalid_request_error`）。 |
| tool_choice | 支持（需引擎参数） | 映射：`auto`→`auto`、`any`→`required`、`none`→`none`、`tool`→指定函数；前提同 tools。 |
| metadata | 忽略 | 协议接受，但不使用、不在响应中回传。 |
| thinking（请求级思考配置） | 不支持（静默忽略） | vLLM 无按请求的思考预算概念；思考链输出取决于模型能力与引擎 `--reasoning-parser` 配置，与该字段无关。 |
| cache_control（内容块/system/tools 上） | 不支持（静默忽略） | vLLM 前缀缓存自动生效，不支持显式缓存断点控制；缓存命中情况通过响应 `usage.cache_read_input_tokens` 观察。 |
| service_tier / container / mcp_servers / inference_geo 等 | 不支持（静默忽略） | vLLM 无对应能力。 |

### 响应字段

| 字段 | 支持情况 | 说明 |
|---|---|---|
| id | 支持（格式差异） | 形如 `chatcmpl-*`（vLLM 请求 id），非 Anthropic 的 `msg_*` 格式；SDK 可正常解析。 |
| type / role | 支持 | 恒为 `message` / `assistant`。 |
| content `text` 块 | 支持 | |
| content `thinking` 块（含 signature） | 条件支持 | 仅当引擎配置 `--reasoning-parser` 且模型以约定标签输出思考内容时产生；`signature` 为占位 UUID 而非 Anthropic 真实签名（回传可被正常接受）。 |
| content `tool_use` 块 | 支持（需引擎参数） | 前提同请求 tools；流式以 `input_json_delta` 增量输出。 |
| stop_reason | 支持（有降级） | `end_turn`/`max_tokens`/`tool_use` 正确映射；**`stop_sequence` 永不产生**——vLLM 的 finish_reason 不区分停止序列命中，归一映射为 `end_turn`，且 `stop_sequence` 字段恒为 `null`。引擎内部重计算（recompute）映射为 `end_turn` 并记录引擎日志。 |
| usage.input_tokens / output_tokens | 支持 | |
| usage.cache_read_input_tokens / cache_creation_input_tokens | 支持（需 `enable_prompt_tokens_details`） | 见"Anthropic 接口特性支持矩阵"。 |
| container / server_tool_use / web_search_tool_result 等新版块 | 不支持 | vLLM 无对应能力，不会产生。 |

### 流式事件

`message_start`、`content_block_start`、`content_block_delta`（`text_delta`/`thinking_delta`/`signature_delta`/`input_json_delta`）、`content_block_stop`、`message_delta`、`message_stop`、`error` 均支持；`ping` 为可选事件，当前不产生。`message_stop` 之后附加一行 `data: [DONE]`（vLLM 惯例的非标准帧，主流 Anthropic SDK 会忽略）。`message_start` 的 `usage` 在 trigger/metaserver 调度模式下省略缓存字段（可选字段），最终 `message_delta` 携带完整值。

### `/v1/messages/count_tokens` 字段

请求支持 `model`（必选）、`messages`（必选）、`system`、`tools`、`tool_choice`；`max_tokens` 不需要。响应为 `{"input_tokens": <int>, "context_management": {"original_input_tokens": <int>}}`。

## 模型列表查询接口

**接口功能**

返回当前AIGW模型配置与实例数量信息。

**接口格式**

请求类型：**GET**
> URL：`http(s)://{IP}:{Port}/v1/models`

IP与端口参见[业务接口的IP/端口与配置](./README.md#业务接口的ip端口与配置)

**请求参数**

无

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/v1/models"
```

**响应示例**

```json
{
  "object": "list",
  "data": [
    {
      "id": "qwen3",
      "object": "model",
      "owned_by": "motor",
      "p_max_seqlen": 8192,
      "d_max_seqlen": 8192,
      "slo_ttft": 1000,
      "slo_tpot": 50,
      "p_instances_num": 2,
      "d_instances_num": 2,
      "created": 1765856000
    }
  ]
}
```

**输出说明**

| 参数名 | 类型 | 说明 |
|---|---|---|
| object | string | 返回对象类型：`list`。 |
| data | array | 模型列表。 |
| data[].id | string | 模型名称 |
| data[].object | string | 返回对象类型。 |
| data[].owned_by | string | 模型归属。 |
| data[].p_max_seqlen | integer | P实例最大序列长度。 |
| data[].d_max_seqlen | integer | D实例最大序列长度。 |
| data[].slo_ttft | integer | SLO首token时延（TTFT，单位毫秒）。 |
| data[].slo_tpot | integer | SLO每token时延（TPOT，单位毫秒）。 |
| data[].p_instances_num | integer | P实例数量。 |
| data[].d_instances_num | integer | D实例数量。 |
| data[].created | integer | 创建时间戳（秒）。 |
