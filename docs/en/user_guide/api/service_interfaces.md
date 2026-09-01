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
  | choices[].delta.content | string | Generate incremental text. |
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

- Non-streaming response example (non-streaming):

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
  | choices[].text | string | Generate incremental text. |
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
