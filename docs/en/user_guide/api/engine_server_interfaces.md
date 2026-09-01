# Engine Server Internal Interface

>[!NOTE]NOTE
>
> The Engine Server internal interface is mounted on the Engine Server InferEndpoint and does **not** provide services on the Coordinator inference interface.

## Engine Server Snapshot Interface

The Engine Server snapshot interface is used for the inference engine to save device-side snapshots, unlock devices, and recover device-side snapshots in container snapshot scenarios. The interface is mounted on the Engine Server InferEndpoint and shares the service port specified by the `--port` startup parameter of `engine_server` with inference interfaces such as `/v1/chat/completions` and `/health`.

The typical call sequence is: `suspend` → (optional) `device_unlock` → `resume`. The specific timing is determined by the deployer.

The Engine Server snapshot interface uses the inference port:

- Base address: `http(s)://{EngineIP}:{inference port}`

- Security protocol: `https` is used when `infer_tls_config.enable_tls` is `true`; otherwise, `http` is used.

For the IP and port, see [IP/Port of Internal Interfaces](./README.md#ipport-of-the-internal-interface)

### Device-side Snapshot Save Interface

**Interface Function**

Notifies the inference engine to flush the model runtime weights to the specified path, locks the device, and saves the device-side snapshot.

**Interface Format**

Request type: **POST**
> URL: `http(s)://{EngineIP}:{inference port}/suspend?model_save_path={model flush path}`

For the IP and port, see [IP/Port of Internal Interfaces](./README.md#ipport-of-the-internal-interface)

**Request Parameters**

| Parameter Name | Type | Description |
| --- | --- | --- |
| `model_save_path` | string | Required; query parameter. The flush directory for data such as model weights. |

**Usage Example**

```bash
curl -X POST "http://{EngineIP}:{inference port}/suspend?model_save_path=/snapshot/weight"
```

**Response example**

- Success: HTTP `200`, with an empty response body.

- Failure: `400` is returned when the required parameter `model_save_path` is missing; `501` is returned when the current engine does not implement `suspend` / `resume`.

### Device Unlock Interface

**Interface Function**

After the device-side snapshot save interface is called, the device enters a locked state. This interface is used to notify the inference engine to unlock the device.

**Interface Format**

Request type: **POST**
> URL: `http(s)://{EngineIP}:{inference port}/device_unlock`

For the IP and port, see [IP/Port of Internal Interfaces](./README.md#ipport-of-the-internal-interface)

**Request Parameters**

None

**Usage Example**

```bash
curl -X POST "http://{EngineIP}:{inference port}/device_unlock"
```

**Response example**

- Success: HTTP `200`, with an empty response body.

- Failure: returns `501` when the current engine has not implemented `device_unlock`.

### Device-side Snapshot Resume Interface

**Interface Function**

Notify the inference engine to resume the saved device-side snapshot, reload the runtime model weights from the specified path, and rebuild runtime states such as the communication domain.

**Interface Format**

Request type: **POST**
> URL: `http(s)://{EngineIP}:{inference port}/resume?data_parallel_master_ip={DP master node IP}&model_path={model path}`

For the IP and port, see [Internal Interface IP/Port](./README.md#ipport-of-the-internal-interface)

**Request Parameter**

| Parameter Name | Type | Description |
| --- | --- | --- |
| `data_parallel_master_ip` | string | Required; query parameter. Data parallel (DP) master node IP. |
| `model_path` | string | Required; query parameter. Model loading path. |

**Usage Example**

```bash
curl -X POST "http://{EngineIP}:{inference port}/resume?data_parallel_master_ip=10.0.0.1&model_path=/snapshot/weight"
```

**Response example**

- Success: HTTP `200`, with an empty response body.

- Failure: `400` is returned when the required parameters `data_parallel_master_ip` or `model_path` are missing; `501` is returned when the current engine does not implement `suspend` / `resume`.

## MetaServer Forwarding Interface

**Interface Function**

Used only in PD/CDP detach scenarios, for a D node to forward requests to a P node.

**Interface Format**

Request type: **POST**
> URL: `http(s)://{EngineIP}:{inference port}/v1/metaserver`

For the IP and port, see [IP/Port of the Internal Interface](./README.md#ipport-of-the-internal-interface)

**Request Parameters**

| Parameter | Type | Description |
| --- | --- | --- |
| `model` | string | Required; model name, passed through to the target node. |
| `messages` | array | Mutually exclusive with `prompt`; Chat input. |
| `prompt` | string | Mutually exclusive with `messages`; Completion input. |
| `stream` | boolean | Optional; whether to return in streaming mode, passed through to the target node. |
| `kv_transfer_params` | object | Required; forwarding control parameters. |
| `kv_transfer_params.request_id` | string | Required; request identifier, used for cross-node tracking and association. |
| `kv_transfer_params.do_remote_decode` | boolean | Optional; whether to perform Decode on the target node. |
| `kv_transfer_params.do_remote_prefill` | boolean | Optional; whether to perform Prefill on the target node. |
| `kv_transfer_params.remote_engine_id` | string | Required; engine ID of the target node. |
| `kv_transfer_params.remote_host` | string | Required; address of the target node (IP or domain name). |
| `kv_transfer_params.remote_port` | string | Required; port of the target node. |

**Usage Example**

- CDP detach scenario, where a D node triggers Prefill on a P node:

  ```json
  curl -X POST "http://{EngineIP}:{inference port}/v1/metaserver" \
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

- PD disaggregation scenario, where the P node triggers Decode on the D node:

  ```json
  curl -X POST "http://{EngineIP}:{inference port}/v1/metaserver" \
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

- CDP separation scenario, transparently passing the response content from the P node:

  ```json
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

- PD disaggregation scenario, transparently passing the response content from the D node:

  ```json
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
This example describes the output of a non-streaming `chat.completion`:

| Parameter | Type | Description |
| --- | --- | --- |
| `id` | string | Response ID. |
| `object` | string | Response object type, `chat.completion` in this example. |
| `created` | integer | Response creation time (Unix timestamp). |
| `model` | string | Name of the model actually used. |
| `choices` | array | List of generated results. |
| `choices[].index` | integer | Result sequence number. |
| `choices[].message.role` | string | Role, `assistant` in this example. |
| `choices[].message.content` | string | Generated content. |
| `choices[].finish_reason` | string | Reason for completion, such as `stop` or `length`. |
| `usage` | object | Token statistics. |
| `usage.prompt_tokens` | integer | Number of input tokens. |
| `usage.completion_tokens` | integer | Number of output tokens. |
| `usage.total_tokens` | integer | Total number of tokens. |
