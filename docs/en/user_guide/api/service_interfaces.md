# Business Interface

## OpenAI Chat Completion Interface

**Interface function**

Provides a conversation generation entry compatible with OpenAI `v1/chat/completions`, for multi-turn conversations, role setting, and context continuation.

**Interface format**

Request type: **POST**
> URL: `http(s)://{IP}:{Port}/v1/chat/completions`

For the IP and port, see [IP/Port and Configuration of Business Interfaces](./README.md#ipport-and-configuration-of-the-business-interface)

**Request parameter**

| Parameter | Type | Description |
|---|---|---|
| model | string | Required; model name. |
| messages | array | Required; conversation message list. |
| stream | boolean | Optional; whether to use streaming output. The default value is false.<ul><li>true: streaming;</li><li>false: non-streaming.</li></ul> |

The sub-parameters of the `messages` parameter are described as follows:

| Parameter | Type | Description |
|---|---|---|
| role | string | Required; the roles are as follows:<ul><li>`system`: system/rule prompt;</li><li>`user`: user input;</li><li>`assistant`: assistant output.</li></ul>In a conversation, `system` is usually the highest-priority constraint, `user` is the question content, and `assistant` is the historical answer. |
| content | string | Required; message content. |

Other OpenAI-compatible fields (such as `max_tokens` and `temperature`) are passed through to the backend inference engine. The common parameters are described as follows:

| Parameter | Type | Description |
|---|---|---|
| max_tokens | integer | Maximum number of generated tokens. |
| temperature | number | Sampling temperature. A higher temperature produces more random output (commonly 0 to 1). An excessively high temperature may cause unstable output. |
| top_p | number | Nucleus sampling threshold (0 to 1). When used together with the `temperature` parameter, usually only one of them is used. |
| presence_penalty | number | Topic penalty that encourages introducing new content. The value range depends on the backend implementation. |
| frequency_penalty | number | Frequency penalty that reduces repeated content. The value range depends on the backend implementation. |
| stop | string/array | Stop word. Generation ends early when the stop word is matched. |

>[!NOTE]NOTE
>Fields not supported by the backend inference engine are ignored or subject to degraded handling. The specific capabilities are subject to the actual model and backend version.

**Usage example**

- Streaming usage example:

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

- Non-streaming usage example:

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

**Response example**

- Streaming response example:

  ```text
  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"Hey there! "},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"Great to see you. "},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"How can I help today?"},"finish_reason":null}]}

  data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

  data: [DONE]
  ```

- Non-streaming response example:

  ```json
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

**Output description**

- Streaming response parameter description:

  The streaming response is an SSE event stream. Each line starts with `data:`, and `data: [DONE]` indicates the end. The JSON structure after `data:` is as follows:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | Request ID. |
  | object | string | Return object type: `chat.completion.chunk`. |
  | created | integer | Creation timestamp (seconds). |
  | model | string | Model name. |
  | choices | array | Incremental result list. |
  | choices[].index | integer | Sequence number. |
  | choices[].delta | object | Incremental content. |
  | choices[].delta.role | string | Only the first packet may return `assistant`. |
  | choices[].delta.content | string | Incremental text. |
  | choices[].finish_reason | string/null | End reason. It is `null` when not ended. |

- Non-streaming response parameter description:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | Request ID. |
  | object | string | Return object type: `chat.completion`. |
  | created | integer | Creation timestamp (seconds). |
  | model | string | Model name. |
  | choices | array | Generation result list. |
  | choices[].index | integer | Sequence number. |
  | choices[].message | object | Generated message. |
  | choices[].message.role | string | Role, fixed to `assistant`. |
  | choices[].message.content | string | Generated content. |
  | choices[].finish_reason | string/null | End reason, such as `stop` and `length`. |

## OpenAI Responses Interface

**Interface Function**

It provides a text generation entry point compatible with the OpenAI Responses API (`/v1/responses`) and is applicable to unified response-based generation scenarios.

**Interface Format**

Request type: **POST**
> URL: `http(s)://{IP}:{Port}/v1/responses`

For the IP and port, see [IP/Port and Configuration of Business Interfaces](./README.md#ipport-and-configuration-of-the-business-interface)

**Request Parameters**

| Parameter | Type | Description |
|---|---|---|
| model | string | Required; model name. |
| input | string/array | Required; text or an array of Responses input items. The item type depends on the deployed backend engine. |
| stream | boolean | Optional; whether to use streaming output. The default value is false.<ul><li>true: streaming;</li><li>false: non-streaming.</li></ul> |
| instructions | string | Optional; system instructions that the model needs to follow. |
| max_output_tokens | integer | Optional; maximum number of output tokens to generate. |

The request is forwarded by the Coordinator as-is to a backend engine that natively supports the Responses API. Currently, the Coordinator additionally supports extracting scheduling information from the plain text `input`, the text message array, and `instructions` for the existing PD, load, and KV affinity scheduling, and does not rewrite the original request into Chat Completions. Input items that are not of the message type are verified by the backend engine according to its native Responses schema.

> **Response format description**: As a transparent proxy, the Coordinator returns the response content directly from the backend inference engine without secondary wrapping.
> The actual response format and optional fields depend on the inference engine version in the deployment environment. Before going live, you must confirm that the engine natively provides `POST /v1/responses`.
> Currently, this interface only adds the "create and infer" capability. It does not include `GET /v1/responses/{id}`, `POST /v1/responses/{id}/cancel`, or background task state storage.
> Plain text requests can participate in KV affinity scheduling. Non-text inputs such as images and tool calls are still forwarded as-is, but the KV prefix extraction logic added by this interface is not used.
> The response example below is in the OpenAI standard format and is for reference only.

**Usage Example**

- Non-streaming usage example:

  ```bash
  curl -X POST "http://{IP}:{Port}/v1/responses" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer {API_KEY}" \
    -d '{
      "model": "qwen3",
      "input": "Hello there!"
    }'
  ```

- Streaming usage example:

  ```bash
  curl -N -X POST "http://{IP}:{Port}/v1/responses" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer {API_KEY}" \
    -d '{
      "model": "qwen3",
      "input": "Hello there!",
      "stream": true
    }'
  ```

**Response Example**

```json
{
  "id": "resp_xxx",
  "object": "response",
  "created_at": 1765856304,
  "status": "completed",
  "model": "qwen3",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "Hello there!"
        }
      ]
    }
  ]
}
```

**Output Description**

- Non-streaming response parameter description:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | Request ID. |
  | object | string | Return object type: `response`. |
  | created_at | integer | Creation timestamp (seconds). |
  | status | string | Response status, such as `completed`. |
  | model | string | Model name. |
  | output | array | List of output content. |
  | output[].type | string | Output type, such as `message`. |
  | output[].role | string | Output role, usually `assistant`. |
  | output[].content | array | List of output content fragments. |
  | output[].content[].type | string | Content type, such as `output_text`. |
  | output[].content[].text | string | Generated text. |

## OpenAI Completion Interface

**Interface Function**

OpenAI Completion-compatible interface that supports text completion and result sampling.

**Interface Format**

Request type: **POST**
> URL: `http(s)://{IP}:{Port}/v1/completions`

For the IP and port, see [IP/Port and Configuration of Business Interfaces](./README.md#ipport-and-configuration-of-the-business-interface)

**Request Parameters**

| Parameter | Type | Description |
|---|---|---|
| model | string | Required; model name. |
| prompt | string/array | Required; prompt. |
| stream | boolean | Optional; whether to use streaming output. The default value is false.<ul><li>true: streaming;</li><li>false: non-streaming.</li></ul> |

Other OpenAI-compatible fields (such as `max_tokens` and `temperature`) are passed through to the backend inference engine. For descriptions of common fields, see the preceding section. If the backend does not support some fields, they are ignored or subject to degraded handling.

**Usage Example**

- Streaming usage example:

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

- Non-streaming usage example:

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

**Response Example**

- Streaming response example:

  ```text
  data: {"id":"cmpl-xxx-0","object":"text_completion","created":1765856304,"model":"qwen3","choices":[{"index":0,"text":"Hey there! ","finish_reason":null}]}

  data: {"id":"cmpl-xxx-0","object":"text_completion","created":1765856304,"model":"qwen3","choices":[{"index":0,"text":"Lovely to hear from you—","finish_reason":null}]}

  data: {"id":"cmpl-xxx-0","object":"text_completion","created":1765856304,"model":"qwen3","choices":[{"index":0,"text":"what would you like to do today?","finish_reason":null}]}

  data: {"id":"cmpl-xxx-0","object":"text_completion","created":1765856304,"model":"qwen3","choices":[{"index":0,"text":"","finish_reason":"stop"}]}

  data: [DONE]
  ```

- Non-streaming response example:

  ```json
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

**Output Description**

- Streaming response parameter description:

  The streaming response is an SSE event stream. Each line starts with `data:`, and `data: [DONE]` indicates the end. The JSON structure after `data:` is as follows:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | Request ID. |
  | object | string | Return object type: `text_completion`. |
  | created | integer | Creation timestamp (seconds). |
  | model | string | Model name. |
  | choices | array | Incremental result list. |
  | choices[].index | integer | Sequence number. |
  | choices[].text | string | Incremental text. |
  | choices[].finish_reason | string/null | End reason. It is `null` when not finished. |

- Non-streaming response parameter description:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | Request ID. |
  | object | string | Return object type: `text_completion`. |
  | created | integer | Creation timestamp (seconds). |
  | model | string | Model name. |
  | choices | array | Generation result list. |
  | choices[].index | integer | Sequence number. |
  | choices[].text | string | Generated text. |
  | choices[].finish_reason | string/null | End reason, such as `stop` and `length`. |

## Anthropic Messages Interface

**Interface Function**

It provides a dialogue generation entry point compatible with the Anthropic Messages API and supports multimodal image input, tool calls, and thinking content blocks. The request is transparently forwarded by the Coordinator to the vLLM backend engine, and vLLM handles the complete Anthropic protocol. It is available only in vLLM backend mode.

**Interface Format**

Request type: **POST**
> URL: `http(s)://{IP}:{Port}/v1/messages`

For the IP and port, see [IP/Port and Configuration of Business Interfaces](./README.md#ipport-and-configuration-of-the-business-interface)

**Request Parameters**

| Parameter | Type | Description |
|---|---|---|
| model | string | Required; model name. |
| messages | array | Required; list of dialogue messages. Each message contains `role` (`user` or `assistant`) and `content` (a string or an array of content blocks). |
| max_tokens | integer | Required; maximum number of tokens to generate. |
| stream | boolean | Optional; whether to use streaming output (SSE). The default value is `false`. |
| system | string/array | Optional; system prompt. It supports the string format or the content block array format. |
| stop_sequences | array | Optional; list of stop sequences. |
| temperature | number | Optional; sampling temperature (0–1). |
| top_p | number | Optional; nucleus sampling threshold. |
| top_k | integer | Optional; top-k sampling parameter. |
| tools | array | Optional; list of tool definitions. Each tool contains `name`, `description`, and `input_schema`. |
| tool_choice | object | Optional; tool selection policy. Values of `type`: `auto`, `any`, `tool`, `none`. |

**Usage Example**

- Basic text dialogue (non-streaming):

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

- Streaming dialogue:

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

- With a system prompt:

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

- With tool calls:

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

**Response Example**

- Non-streaming response example:

  ```json
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

- Streaming response example (SSE event stream):

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

**Output Description**

- Non-streaming response parameter description:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | Request ID. |
  | type | string | Return object type: `message`. |
  | role | string | Role, fixed to `assistant`. |
  | content | array | List of content blocks. |
  | content[].type | string | Content block type: `text`, `tool_use`, `thinking`, etc. |
  | content[].text | string | Text content (when type is `text`). |
  | model | string | Model name. |
  | stop_reason | string | End reason: `end_turn` (normal end), `max_tokens` (upper limit reached), `tool_use` (tool call). |
  | usage | object | Token statistics, including `input_tokens` and `output_tokens`. |

- Streaming response event type description:

  | Event Type | Description |
  |---|---|
  | `message_start` | Message start, including metadata and the initial usage. |
  | `content_block_start` | Content block start, marking a new text / tool_use / thinking block. |
  | `content_block_delta` | Content block increment, carrying `text_delta`, `input_json_delta`, `thinking_delta`, etc. |
  | `content_block_stop` | Content block end. |
  | `message_delta` | Message increment, including `stop_reason` and the final usage. |
  | `message_stop` | Message end. |

## Anthropic Count Tokens Interface

**Interface Function**

It provides a token counting entry point compatible with Anthropic `messages/count_tokens`, used to estimate the token consumption of input messages. The request is transparently forwarded by the Coordinator to the vLLM backend engine. It is available only in vLLM backend mode.

**Interface Format**

Request type: **POST**
> URL: `http(s)://{IP}:{Port}/v1/messages/count_tokens`

For the IP and port, see [IP/Port and Configuration of Business Interfaces](./README.md#ipport-and-configuration-of-the-business-interface)

**Request Parameters**

| Parameter | Type | Description |
|---|---|---|
| model | string | Required; model name. |
| messages | array | Required; message list. The format is the same as that of `/v1/messages`. |
| system | string/array | Optional; system prompt. |
| tools | array | Optional; tool definitions. |
| tool_choice | object | Optional; tool selection policy. |

**Usage Example**

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

**Response Example**

```json
{
  "input_tokens": 10,
  "context_management": {
    "original_input_tokens": 10
  }
}
```

**Output Description**

| Parameter | Type | Description |
|---|---|---|
| input_tokens | integer | Number of input tokens. |
| context_management | object | Context management information. |
| context_management.original_input_tokens | integer | Number of original input tokens. |

## Model List Query Interface

**Interface Function**

Returns the current AIGW model configuration and instance quantity information.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/v1/models`

For the IP and port, see [IP/Port and Configuration of the Business Interface](./README.md#ipport-and-configuration-of-the-business-interface)

**Request Parameter**

None

**Usage Example**

```bash
curl -X GET "http://{IP}:{Port}/v1/models"
```

**Response Example**

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

**Output description**

| Parameter name | Type | Description |
|---|---|---|
| object | string | Return object type: `list`. |
| data | array | Model list. |
| data[].id | string | Model name |
| data[].object | string | Return object type. |
| data[].owned_by | string | Model ownership. |
| data[].p_max_seqlen | integer | Maximum sequence length of the P instance. |
| data[].d_max_seqlen | integer | Maximum sequence length of the D instance. |
| data[].slo_ttft | integer | SLO time to first token (TTFT, in milliseconds). |
| data[].slo_tpot | integer | SLO time per output token (TPOT, in milliseconds). |
| data[].p_instances_num | integer | Number of P instances. |
| data[].d_instances_num | integer | Number of D instances. |
| data[].created | integer | Creation timestamp (seconds). |
