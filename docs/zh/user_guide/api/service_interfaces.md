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

---

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

---

## Anthropic Messages 接口

**接口功能**

提供与 Anthropic Messages API 兼容的对话生成入口，支持多模态图片输入、工具调用、思考链（thinking）内容块。请求由 Coordinator 透明转发至 vLLM 后端引擎，由 vLLM 处理完整的 Anthropic 协议。仅 vLLM 后端模式可用。

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

---

## Anthropic Count Tokens 接口

**接口功能**

提供与 Anthropic `messages/count_tokens` 兼容的 Token 计数入口，用于预估输入消息的 Token 消耗。请求由 Coordinator 透明转发至 vLLM 后端引擎。仅 vLLM 后端模式可用。

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
