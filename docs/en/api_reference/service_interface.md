# Service APIs

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:07:44.825Z pushedAt=2026-06-27T07:29:21.970Z -->

## OpenAI Chat Completion API

**API Function**

Provides a conversation generation entry point compatible with OpenAI `v1/chat/completions`, used for multi-turn conversations, role settings, and context continuation.

**API Format**

Request Type: **POST**  
URL: `http(s)://{CoordinatorIP}:{Inference Port}/v1/chat/completions`

  >[!NOTE]NOTE
  >
  > - `{CoordinatorIP}`: The IP or domain name of the machine where the Coordinator service is deployed. The value comes from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual node IP at runtime.
  > - `{Inference Port}`: The internal port value comes from the configuration item `api_config.coordinator_api_infer_port` (default `1025`). The external binding port is specified by `spec.ports[].nodePort` in the deployment configuration `deployer/deployment/coordinator_init.yaml` (default `31015`).

Request Header:

- Required: `Content-Type: application/json`

- Optional: `{api_key_config.header_name}: {api_key_config.key_prefix}{API_KEY}` (default `Authorization: Bearer {API_KEY}`)

**Request Parameters**

| Parameter | Type | Description |
|---|---|---|
| model | string | (Required) Model name. |
| messages | array | (Required) List of conversation messages. |
| stream | boolean | (Optional) Whether to enable streaming output. Defaults to `false`.<ul><li>`true`: Streaming;</li><li>`false`: Non-streaming.</li></ul> |

The sub-parameters within the `messages` parameter are explained as follows:

| Parameter | Type | Description |
|---|---|---|
| role | string | (Required) The roles are as follows:<ul><li>`system`: System/rule prompt;</li><li>`user`: User input;</li><li>`assistant`: Assistant output.</li></ul>In a conversation, `system` typically serves as the highest priority constraint, `user` contains the question content, and `assistant` contains the historical responses. |
| content | string | (Required) Message content. |

Other OpenAI-compatible fields (such as `max_tokens`, `temperature`, etc.) will be passed through to the backend inference engine. Common parameters are described as follows:

| Parameter | Type | Description |
|---|---|---|
| max_tokens | integer | The maximum number of tokens to generate. |
| temperature | number | Sampling temperature. Higher values produce more random output (commonly 0–1). Excessively high values may cause unstable output. |
| top_p | number | Nucleus sampling threshold (0–1). When used alongside the `temperature` parameter, typically only one of them is applied. |
| presence_penalty | number | Topic penalty to encourage introducing new content. The value range depends on the backend implementation. |
| frequency_penalty | number | Frequency penalty to reduce repetitive content. The value range depends on the backend implementation. |
| stop | string/array | Stop word. Generation stops early when matched. |

>[!NOTE]Note
>Fields not supported by the backend inference engine will be ignored or degraded. The specific capabilities are subject to the actual model and backend version.

**Usage Example**

- Streaming usage example:

  ```bash
  curl -N -X POST "http://{CoordinatorIP}:{Inference Port}/v1/chat/completions" \
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
  curl -X POST "http://{CoordinatorIP}:{Inference Port}/v1/chat/completions" \
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

**Response Example**

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

**Output Description**

- Streaming response parameter description:

  The streaming response is an SSE event stream, where each line starts with `data:`, and concludes with `data: [DONE]`. The JSON structure after `data:` is as follows:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | ID of the current request. |
  | object | string | Return object type: `chat.completion.chunk`. |
  | created | integer | Creation timestamp (seconds). |
  | model | string | Model name. |
  | choices | array | Incremental result list. |
  | choices[].index | integer | Sequence number. |
  | choices[].delta | object | Incremental content. |
  | choices[].delta.role | string | `assistant` maybe returned only for the first packet. |
  | choices[].delta.content | string | Generated incremental text. |
  | choices[].finish_reason | string/null | Finish reason; `null` when not finished. |

- Non-streaming response parameter description:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | ID of the current request. |
  | object | string | Return object type: `chat.completion`. |
  | created | integer | Creation timestamp (in seconds). |
  | model | string | Model name. |
  | choices | array | List of generated results. |
  | choices[].index | integer | Sequence number. |
  | choices[].message | object | Generated message. |
  | choices[].message.role | string | Role, fixed as `assistant`. |
  | choices[].message.content | string | Generated content. |
  | choices[].finish_reason | string/null | Finish reason, such as `stop`, `length`, etc. |

## OpenAI Completion API

**API Function**

OpenAI Completion compatible API, supporting text completion and result sampling.

**API Format**

Request Type: **POST**
URL: `http(s)://{CoordinatorIP}:{Inference Port}/v1/completions`

  >[!NOTE]Note
  >
  > - `{CoordinatorIP}`: The IP or domain name of the machine where the Coordinator service is deployed. The value comes from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual node IP at runtime.
  > - `{Inference Port}`: The internal port value comes from the configuration item `api_config.coordinator_api_infer_port` (default `1025`). The external binding port is specified by `spec.ports[].nodePort` in the deployment configuration `deployer/deployment/coordinator_init.yaml` (default `31015`).

Request Header:

- Required: `Content-Type: application/json`

- Optional: `{api_key_config.header_name}: {api_key_config.key_prefix}{API_KEY}` (default `Authorization: Bearer {API_KEY}`)

**Request Parameters**

| Parameter | Type | Description |
|---|---|---|
| model | string | (Required) Model name. |
| prompt | string/array | (Required) Prompt. |
| stream | boolean | (Optional) Whether to enable streaming output. Defaults to `false`.<ul><li>`true`: Streaming</li><li>`false`: Non-streaming</li></ul> |

Other OpenAI-compatible fields (such as `max_tokens`, `temperature`, etc.) will be passed through to the backend inference engine. The descriptions of common fields are the same as above. If the backend does not support a field, it will be ignored or downgraded.

**Usage Example**

- Streaming usage example:

  ```bash
  curl -N -X POST "http://{CoordinatorIP}:{Inference Port}/v1/completions" \
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
  curl -X POST "http://{CoordinatorIP}:{Inference Port}/v1/completions" \
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

  The streaming response is an SSE event stream. Each line starts with `data:`, and concludes with `data: [DONE]`. The JSON structure after `data:` is as follows:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | ID of the current request. |
  | object | string | Return object type: `text_completion`. |
  | created | integer | Creation timestamp (in seconds). |
  | model | string | Model name. |
  | choices | array | Incremental result list. |
  | choices[].index | integer | Index. |
  | choices[].text | string | Generated incremental text. |
  | choices[].finish_reason | string/null | Finish reason. `null` when not finished. |

- Non-streaming response parameter description:

  | Parameter | Type | Description |
  |---|---|---|
  | id | string | ID of the current request. |
  | object | string | Return object type: `text_completion`. |
  | created | integer | Creation timestamp (in seconds). |
  | model | string | Model name. |
  | choices | array | List of generated results. |
  | choices[].index | integer | Index. |
  | choices[].text | string | Generated text. |
  | choices[].finish_reason | string/null | Finish reason, such as `stop`, `length`, etc. |

## MetaServer Forwarding API (Internal API)

**API function**

Used only in PD/CDP disaggregation deployment scenarios, for the D node to forward requests to the P node.

**API Format**

Request Type: **POST**
URL: `http(s)://{CoordinatorIP}:{Inference Port}/v1/metaserver`

  >[!NOTE]NOTE
  >
  > - `{CoordinatorIP}`: The IP or domain name of the machine where the Coordinator service is deployed. The value comes from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual node IP at runtime.
  > - `{Inference Port}`: Configuration item `api_config.coordinator_api_mgmt_port` (default `1026`).

Request Header:

- Required: `Content-Type: application/json`

- Optional: None

**Request Parameters**

| Parameter | Type | Description |
| --- | --- | --- |
| `model` | string | (Required) Model name, transparently transmitted to the target node. |
| `messages` | array | Choose either this or `prompt`; Chat input. |
| `prompt` | string | Choose either this or `messages`; Completion input. |
| `stream` | boolean | (Optional) Whether to return in streaming mode, transparently transmitted to the target node. |
| `kv_transfer_params` | object | (Required) Forwarding control parameters. |
| `kv_transfer_params.request_id` | string | (Required) Request identifier for cross-node tracking and association. |
| `kv_transfer_params.do_remote_decode` | boolean | (Optional) Whether to execute Decode on the target node. |
| `kv_transfer_params.do_remote_prefill` | boolean | (Optional) Whether to execute Prefill on the target node. |
| `kv_transfer_params.remote_engine_id` | string | (Required) Target node engine ID. |
| `kv_transfer_params.remote_host` | string | (Required) Target node address (IP or domain name). |
| `kv_transfer_params.remote_port` | string | (Required) Target node port. |

**Usage Example**

- CDP disaggregation scenario, where D node triggers Prefill on P node:

  ```json
  curl -X POST "http://{CoordinatorIP}:{Inference Port}/v1/metaserver" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3",
    "messages": [
      { "role": "user", "content": "Hello!" }
    ],
    "stream": false,
    "kv_transfer_params": {
      "request_id": "req-id",
      "do_remote_decode": false,
      "do_remote_prefill": true,
      "remote_engine_id": "engine-p-0",
      "remote_host": "10.0.0.12",
      "remote_port": "1000"
    }
  }'
  ```

- PD disaggregation scenario, where the P node triggers Decode on D node:

  ```json
  curl -X POST "http://{CoordinatorIP}:{Inference Port}/v1/metaserver" \
    -H "Content-Type: application/json" \
    -d '{
      "model": "qwen3",
      "messages": [
        { "role": "user", "content": "Hello!" }
      ],
      "stream": false,
      "kv_transfer_params": {
        "request_id": "req-id",
        "do_remote_decode": true,
        "do_remote_prefill": false,
        "remote_engine_id": "engine-d-0",
        "remote_host": "10.0.0.21",
        "remote_port": "1001"
      }
    }'
  ```

**Response Example**

- CDP disaggregation scenario, transparently transmits the P node response content:

  ```JSON
  {
    "id": "chatcmpl-xxx12",
    "object": "chat.completion",
    "created": 1738828800,
    "model": "qwen3",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "Hello! How can I help you?"
        },
        "finish_reason": "stop"
      }
    ],
    "usage": {
      "prompt_tokens": 6,
      "completion_tokens": 7,
      "total_tokens": 13
    }
  }
  ```

- PD disaggregation scenario, transparently transmits the D node response content:

  ```JSON
  {
    "id": "chatcmpl-xxx",
    "object": "chat.completion",
    "created": 1738828800,
    "model": "qwen3",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "Hello! How can I help you?"
        },
        "finish_reason": "stop"
      }
    ],
    "usage": {
      "prompt_tokens": 8,
      "completion_tokens": 9,
      "total_tokens": 17
    }
  }
  ```

**Output Description**  
This example is the output description for a non-streaming `chat.completion`:

| Parameter | Type | Description |
| --- | --- | --- |
| `id` | string | Response ID. |
| `object` | string | Response object type, for example, `chat.completion`. |
| `created` | integer | Response creation time (Unix timestamp). |
| `model` | string | The model name actually used. |
| `choices` | array | Generated result list. |
| `choices[].index` | integer | Result index. |
| `choices[].message.role` | string | Role, for example, `assistant`. |
| `choices[].message.content` | string | Generated content. |
| `choices[].finish_reason` | string | Finish reason, such as `stop` or `length`. |
| `usage` | object | Token statistics. |
| `usage.prompt_tokens` | integer | Number of input tokens. |
| `usage.completion_tokens` | integer | Number of output tokens. |
| `usage.total_tokens` | integer | Total number of tokens. |

## Model List Query API

**API Function**

Returns the current AIGW model configuration and instance count information.

**API Format**

Request Type: **GET**  
URL: `http(s)://{CoordinatorIP}:{Inference Port}/v1/models`

  >[!NOTE]NOTE
  >
  > - `{CoordinatorIP}`: The IP or domain name of the machine where the Coordinator service is deployed. The value comes from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual node IP at runtime.
  > - `{Inference Port}`: The internal port value comes from the configuration item `api_config.coordinator_api_infer_port` (default `1025`). The external binding port is specified by `spec.ports[].nodePort` in the deployment configuration `deployer/deployment/coordinator_init.yaml` (default `31015`).

**Request Parameters**

None

**Usage Example**

```bash
curl -X GET "http://{CoordinatorIP}:{Inference Port}/v1/models"
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

**Output Description**

| Parameter | Type | Description |
|---|---|---|
| object | string | Return object type: `list`. |
| data | array | Model list. |
| data[].id | string | Model name. |
| data[].object | string | Return object type. |
| data[].owned_by | string | Model ownership. |
| data[].p_max_seqlen | integer | Maximum sequence length of the P instance. |
| data[].d_max_seqlen | integer | Maximum sequence length of the D instance. |
| data[].slo_ttft | integer | SLO time to first token (TTFT, in milliseconds). |
| data[].slo_tpot | integer | SLO time per output token (TPOT, in milliseconds). |
| data[].p_instances_num | integer | Number of P instances. |
| data[].d_instances_num | integer | Number of D instances. |
| data[].created | integer | Creation timestamp (in seconds). |
