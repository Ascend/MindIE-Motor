# 启动状态查询接口


**接口功能**

查询服务启动状态。

**接口格式**

操作类型：**GET**

```
URL：https://{ip}:{port}/v1/startup
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令]参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令]参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

无

**使用样例**

请求样例：

```
GET https://{ip}:{port}/v1/startup
```

响应样例：

服务已启动时无内容。

**输出说明**

-   状态码200，表示服务已启动，消息体没有内容。
-   无响应，表示服务未启动。

# 健康状态查询接口

**接口功能**

查询服务状态是否正常。

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   该接口建议每隔五秒发送一次。
>-   该接口支持PD分离场景。

**接口格式**

操作类型：**GET**

```
URL：https://{ip}:{port}/v1/health
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

无

**使用样例**

请求样例：

```
GET https://{ip}:{port}/v1/health
```

响应样例：

服务状态正常时无内容。

**输出说明**

-   状态码200，表示服务状态正常，消息体没有内容。
-   无响应，表示服务异常。

# 就绪状态查询接口

**接口功能**

查询服务就绪状态。

**接口格式**

操作类型：**GET**

```
URL：https://{ip}:{port}/v1/readiness
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

无

**使用样例**

请求样例：

```
GET https://{ip}:{port}/v1/readiness
```

响应样例：

服务准备就绪时无内容。

**输出说明**

-   状态码200，表示服务已就绪，消息体没有内容。
-   状态码503，表示服务未就绪，消息体没有内容。

# Triton健康检查接口

**接口功能**

检查Triton健康状态。

>![](public_sys-resources/icon-note.gif) **说明：** 
>该接口建议每隔五秒发送一次。

**接口格式**

操作类型：**GET**

```
URL：https://{ip}:{port}/v2/health/live
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

无

**使用样例**

请求样例：

```
GET https://{ip}:{port}/v2/health/live
```

响应样例：

服务正常运行时无内容。

**输出说明**

-   状态码200，表示服务正常运行，消息体没有内容。
-   无响应，表示服务异常。

# Triton就绪状态检查接口

**接口功能**

检查Triton就绪状态。

**接口格式**

操作类型：**GET**

```
URL：https://{ip}:{port}/v2/health/ready
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

无

**使用样例**

请求样例：

```
GET https://{ip}:{port}/v2/health/ready
```

响应样例：

服务准备就绪时无内容。

**输出说明**

-   状态码200，表示服务已就绪，消息体没有内容。
-   状态码503，表示服务未就绪，消息体没有内容。

# Triton模型就绪状态检查接口

**接口功能**

检查Triton模型就绪状态。

**接口格式**

操作类型：**GET**

```
URL：https://{ip}:{port}/v2/models/{MODEL_NAME}/versions/${MODEL_VERSION}/ready
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。
>-   ${MODEL_NAME}字段指定需要查询的模型名称。
>-   /versions/${MODEL_VERSION}字段暂不支持，不传递。

**请求参数**

无

**使用样例**

请求样例：

```
GET https://{ip}:{port}/v2/models/llama3-70b/ready
```

响应样例：

Triton模型已就绪时无内容。

**输出说明**

-   状态码200，表示Triton模型已就绪，消息体没有内容。
-   状态码503，表示Triton模型未就绪，消息体没有内容。

# 集群节点同步接口

**接口功能**

同步集群节点。

>![](public_sys-resources/icon-note.gif) **说明：** 
>该接口不需要用户调用，属于Coordinator和Controller之间的通信接口。

**接口格式**

操作类型：**POST**

```

URL：https://\{ip\}:\{port\}/v1/instances/refresh
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

<br>

**请求参数**

|参数|类型|说明|
|--|--|--|
|ids|uint64_t[]|必填。<br>同步节点的ID列表。|
|instances|object[]|必填。<br>同步节点的具体信息。|
|id|uint64_t|必填。<br>节点ID。|
|ip|string|必填。<br>节点IP。|
|port|string|必填，取值范围：["1024", "65535"]<br>节点数据端口。|
|metric_port|string|必填，取值范围：["1024", "65535"]<br>节点metrics端口。|
|model_name|string|必填。<br>节点加载的模型名。|
|static_info|object|必填。<br>节点静态信息。|
|group_id|uint64_t|必填。<br>节点所属的组ID。|
|max_seq_len|uint32_t|必填。取值大于0。<br>节点最大队列长度。|
|max_output_len|uint32_t|必填，取值范围[1, max_seq_len - 1]。<br>节点最大推理输出长度。|
|total_slots_num|uint32_t|必填，取值范围[1, 5000]。<br>节点最大推理任务数。|
|total_block_num|uint32_t|必填，取值大于0。<br>节点最大内存块个数。|
|block_size|uint32_t|必填，取值范围[1, 128]。<br>内存块大小。|
|label|uint32_t|必填。只在PD分离场景有用。节点标签；取值如下所示：<li>2：Prefill节点<li>3：Decode节点|
|role|uint32_t|必填。节点身份；取值如下所示：<li>80：Prefill节点<li>68：Decode节点<li>85：Undef节点|
|dynamic_info|object|必填。<br>节点动态信息。|
|avail_slots_num|uint32_t|必填。取值范围[0, total_slots_num]。<br>节点剩余可用推理任务数。|
|avail_block_num|uint32_t|必填。取值范围[0, total_block_num]。<br>节点剩余可用内存块个数。|
|peers|uint64_t[]|Decode节点连接的所有Prefill节点ID。<li>当节点为Prefill时，不需要该字段；<li>当节点为Decode时，必填。|

<br>

**使用样例**

请求样例：

```
POST https://{ip}:{port}/v1/instances/refresh
```

请求消息体：

```
{
    "ids": [
        0,1
    ],
    "instances": [
        {
            "id": 0,
            "ip": "0.0.0.0",
            "port": "1025",
            "metric_port": "1026",
            "model_name": "your_model_name",
            "static_info": {
                "group_id": 0,
                "max_seq_len": 2048,
                "max_output_len": 512,
                "total_slots_num": 200,
                "total_block_num": 1024,
                "block_size": 128,
                "label": 2,
                "role": 80
            },
            "dynamic_info": {
                "avail_slots_num": 200,
                "avail_block_num": 1024
            }
        },
        {
            "id": 1,
            "ip": "0.0.0.0",
            "port": "1025",
            "metric_port": "1026",
            "model_name": "your_model_name",
            "static_info": {
                "group_id": 0,
                "max_seq_len": 2048,
                "max_output_len": 512,
                "total_slots_num": 200,
                "total_block_num": 1024,
                "block_size": 128,
                "label": 3,
                "role": 68
            },
            "dynamic_info": {
                "avail_slots_num": 200,
                "avail_block_num": 1024,
                "peers": [
                    0
                ]
            }
        }
    ]
}
```

响应样例：

正常时无响应消息体。

<br>

**输出说明**

当请求的消息体json格式和字段正确时，返回http200状态码；否则返回http400状态码。


# 集群节点停止服务接口

**接口功能**

停止集群节点服务。

>![](public_sys-resources/icon-note.gif) **说明：** 
>该接口不需要用户调用，属于Coordinator和Controller之间的通信接口。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/v1/instances/offline
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

<br>

**请求参数**

|参数|类型|说明|
|--|--|--|
|ids|uint64_t[]|必填。<br>停止服务的节点ID。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/v1/instances/offline
```

请求消息体：

```
{
    "ids": [
        0,1
    ]
}
```

响应样例：

正常时无响应消息体。

**输出说明**

-   请求的消息体json格式和字段正确时，返回http200状态码。
-   当Coordinator未就绪时，返回http503状态码。
-   当请求消息体格式错误时，返回http400状态码。

# 集群节点恢复服务接口

**接口功能**

恢复集群节点服务。

>![](public_sys-resources/icon-note.gif) **说明：** 
>该接口不需要用户调用，属于Coordinator和Controller之间的通信接口。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/v1/instances/online
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

|参数|类型|说明|
|--|--|--|
|ids|uint64_t[]|必填。<br>恢复服务的节点ID。|

<br>

**使用样例**

请求样例：

```
POST https://{ip}:{port}/v1/instances/online
```

请求消息体：

```
{
    "ids": [
        0,1
    ]
}
```

响应样例：

正常时无响应消息体。

**输出说明**

-   请求的消息体json格式和字段正确时，返回http200状态码。
-   当Coordinator未就绪时，返回http503状态码。
-   当请求消息体格式错误时，返回http400状态码。

# PD节点之间任务状态查询接口

**接口功能**

查询PD节点之间的任务状态。

>![](public_sys-resources/icon-note.gif) **说明：** 
>该接口不需要用户调用，属于Coordinator和Controller之间的通信接口。

**接口格式**

操作类型：**POST**

```
URL：https:/{ip\}:{port}/v1/instances/query_tasks
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

|参数|类型|说明|
|--|--|--|
|p_id|uint64_t|必填。<br>P节点ID。|
|d_id|uint64_t|必填。<br>D节点ID。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/v1/instances/query_tasks
```

请求消息体：

```
{
    "p_id": 0,
    "d_id": 1
}
```

响应样例：

-   PD之间没有任务

    ```
    {
        "is_end": true
    }
    ```

-   PD之间有任务

    ```
    {
        "is_end": false
    }
    ```

**输出说明**

|参数|类型|说明|
|--|--|--|
|is_end|bool|PD之间任务是否结束。|


# 管控指标查询接口

>![](public_sys-resources/icon-note.gif) **说明：** 
>如需使用该接口，请确保在启动服务前开启服务化管控开关。开启服务化管控功能的命令如下：
>```
>export MIES_SERVICE_MONITOR_MODE=1
>```

**接口功能**

查询集群的服务化管控指标，返回格式为Prometheus metrics。

**接口格式**

操作类型：**GET**

```
URL：https://{ip}:{port}/metrics
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

无

**使用样例**

请求样例：

```
GET https://{ip}:{port}/metrics
```

响应样例：

```
# HELP request_received_total Number of requests received so far.
# TYPE request_received_total counter
request_received_total{model_name="llama3-8b"} 3188
# HELP request_success_total Number of requests proceed successfully so far.
# TYPE request_success_total counter
request_success_total{model_name="llama3-8b"} 2267
# HELP request_failed_total Number of requests failed so far.
# TYPE request_failed_total counter
request_failed_total{model_name="llama3-8b"} 0
# HELP num_preemptions_total Cumulative number of preemption from the engine.
# TYPE num_preemptions_total counter
num_preemptions_total{model_name="llama3-8b"} 637
# HELP num_requests_running Number of requests currently running on NPU.
# TYPE num_requests_running gauge
num_requests_running{model_name="llama3-8b"} 0
# HELP num_requests_waiting Number of requests waiting to be processed.
# TYPE num_requests_waiting gauge
num_requests_waiting{model_name="llama3-8b"} 0
# HELP num_requests_swapped Number of requests swapped to CPU.
# TYPE num_requests_swapped gauge
num_requests_swapped{model_name="llama3-8b"} 0
# HELP prompt_tokens_total Number of prefill tokens processed.
# TYPE prompt_tokens_total counter
prompt_tokens_total{model_name="llama3-8b"} 9564
# HELP generation_tokens_total Number of generation tokens processed.
# TYPE generation_tokens_total counter
generation_tokens_total{model_name="llama3-8b"} 84425
# HELP avg_prompt_throughput_toks_per_s Average prefill throughput in tokens/s.
# TYPE avg_prompt_throughput_toks_per_s gauge
avg_prompt_throughput_toks_per_s{model_name="llama3-8b"} 0.586739718914032
# HELP avg_generation_throughput_toks_per_s Average generation throughput in tokens/s.
# TYPE avg_generation_throughput_toks_per_s gauge
avg_generation_throughput_toks_per_s{model_name="llama3-8b"} 2.375296831130981
# HELP failed_request_perc Requests failure rate. 1 means 100 percent usage.
# TYPE failed_request_perc gauge
failed_request_perc{model_name="llama3-8b"} 0
# HELP npu_cache_usage_perc NPU KV-cache usage. 1 means 100 percent usage.
# TYPE npu_cache_usage_perc gauge
npu_cache_usage_perc{model_name="llama3-8b"} 1
# HELP cpu_cache_usage_perc CPU KV-cache usage. 1 means 100 percent usage.
# TYPE cpu_cache_usage_perc gauge
cpu_cache_usage_perc{model_name="llama3-8b"} 0
# HELP npu_prefix_cache_hit_rate NPU prefix cache block hit rate.
# TYPE npu_prefix_cache_hit_rate gauge
npu_prefix_cache_hit_rate{model_name="llama3-8b"} 0.5
# HELP time_to_first_token_seconds Histogram of time to first token in seconds.
# TYPE time_to_first_token_seconds histogram
time_to_first_token_seconds_count{model_name="llama3-8b"} 2523
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.001"} 0
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.005"} 0
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.01"} 0
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.02"} 0
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.04"} 0
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.06"} 10
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.08"} 54
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.1"} 104
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.25"} 256
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.5"} 256
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="0.75"} 276
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="1"} 321
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="2.5"} 628
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="5"} 1148
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="7.5"} 2523
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="10"} 2523
time_to_first_token_seconds_bucket{model_name="llama3-8b",le="+Inf"} 2523
# HELP time_per_output_token_seconds Histogram of time per output token in seconds.
# TYPE time_per_output_token_seconds histogram
time_per_output_token_seconds_count{model_name="llama3-8b"} 85800
time_per_output_token_seconds_sum{model_name="llama3-8b"} 4445.857012826018
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.01"} 0
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.025"} 0
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.05"} 3
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.075"} 12
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.1"} 40283
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.15"} 83145
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.2"} 83339
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.3"} 83339
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.4"} 83539
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.5"} 85139
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="0.75"} 85740
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="1"} 85800
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="2.5"} 85800
time_per_output_token_seconds_bucket{model_name="llama3-8b",le="+Inf"} 85800
# HELP e2e_request_latency_seconds Histogram of end to end request latency in seconds.
# TYPE e2e_request_latency_seconds histogram
e2e_request_latency_seconds_count{model_name="llama3-8b"} 2267
e2e_request_latency_seconds_sum{model_name="llama3-8b"} 12684.5319980979
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="1"} 27
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="2.5"} 268
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="5"} 712
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="10"} 2267
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="15"} 2267
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="20"} 2267
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="30"} 2267
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="40"} 2267
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="50"} 2267
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="60"} 2267
e2e_request_latency_seconds_bucket{model_name="llama3-8b",le="+Inf"} 2267
# HELP request_prompt_tokens Number of prefill tokens processed.
# TYPE request_prompt_tokens histogram
request_prompt_tokens_count{model_name="llama3-8b"} 3188
request_prompt_tokens_sum{model_name="llama3-8b"} 9564
request_prompt_tokens_bucket{model_name="llama3-8b",le="10"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="50"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="100"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="200"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="500"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="1000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="2000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="5000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="10000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="16000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="20000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="32000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="50000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="64000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="128000"} 3188
request_prompt_tokens_bucket{model_name="llama3-8b",le="+Inf"} 3188
# HELP request_generation_tokens Number of generation tokens processed.
# TYPE request_generation_tokens histogram
request_generation_tokens_count{model_name="llama3-8b"} 2267
request_generation_tokens_sum{model_name="llama3-8b"} 84425
request_generation_tokens_bucket{model_name="llama3-8b",le="10"} 0
request_generation_tokens_bucket{model_name="llama3-8b",le="50"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="100"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="200"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="500"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="1000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="2000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="5000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="10000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="16000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="20000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="32000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="50000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="64000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="128000"} 2267
request_generation_tokens_bucket{model_name="llama3-8b",le="+Inf"} 2267
```
<br>

**输出说明**

-   状态码200，查询成功，消息主体为Prometheus Metrics格式的集群管控指标汇总。
-   状态码503，查询失败。

指标种类和内容由Server决定，Coordinator只负责汇总集群下各节点的指标。

|参数|类型|说明|
|--|--|--|
|request_received_total|Counter|服务端截至目前为止接收到的请求个数。<br>model_name：使用的模型名称。string类型，如果有多个模型，请使用"&"进行拼接。响应样例中的所有model_name都为此含义。|
|request_success_total|Counter|服务端截至目前为止执行成功的请求个数。|
|request_failed_total|Counter|服务端到目前为止推理失败的请求个数。|
|num_requests_running|Gauge|服务端当前正在执行的请求个数。|
|num_requests_waiting|Gauge|服务端当前等待调度执行的请求个数。|
|num_requests_swapped|Gauge|服务端当前被交换到CPU上的请求个数。|
|num_preemptions_total|Counter|服务端截至目前为止，累计执行请求抢占次数**。**|
|prompt_tokens_total|Counter|已经处理的prefill tokens数量。|
|generation_tokens_total|Counter|已经处理的generation tokens数量。|
|avg_prompt_throughput_toks_per_s|Gauge|截止上一个完成的请求为止，最新的平均prefill吞吐，单位为tokens/s。|
|avg_generation_throughput_toks_per_s|Gauge|截止上一个生成的token为止，最新的平均generation吞吐，单位为tokens/s。|
|failed_request_perc|Gauge|服务端截至目前为止执行失败的请求率，1代表100%。|
|npu_cache_usage_perc|Gauge|当前KV Cache的NPU显存利用率，1代表100%。|
|cpu_cache_usage_perc|Gauge|当前KV Cache的CPU内存利用率，1代表100%。|
|npu_prefix_cache_hit_rate|Gauge|NPU卡上prefix cache命中率，1代表100%。|
|time_to_first_token_seconds|Histogram|首token时延，代表请求生成首个推理token消耗的时间，单位为秒。该首token时延为纯推理的时间，不包含HTTP通信、tokenizer和PD分离下传输KV Cache的时间。<li>time_to_first_token_seconds_count：截止目前为止，完成并统计首token时延的请求个数。<li>time_to_first_token_seconds_sum：截止目前为止，完成并统计首token时延的所有请求的首token时延的加和。<li>time_to_first_token_seconds_bucket：截止目前为止，直方图分桶统计的请求的首token时延数据。<br>le：less than or equal to，是直方图分桶的界限。<br>**响应样例中的所有le都为此含义。**|
|time_per_output_token_seconds|Histogram|token生成时延，代表连续两个token生成之间的时间间隔，单位为秒。该token生成时延为纯推理的时间，不包含HTTP通信和PD分离下传输KV Cache的时间。<li>time_per_output_token_seconds_count：截止目前为止，完成并统计token生成时延的token个数。<li>time_per_output_token_seconds_sum：截止目前为止，完成并统计token生成时延的所有token的token生成时延的加和。<li>time_per_output_token_seconds_bucket：截止目前为止，直方图分桶统计的token生成时延数据。|
|e2e_request_latency_seconds|Histogram|端到端时延，代表请求从接收到执行完成消耗的时间，单位为秒。<li>e2e_request_latency_seconds_count：截止目前为止，完成并统计端到端时延的请求个数。<li>e2e_request_latency_seconds_sum：截止目前为止，完成并统计端到端时延的所有请求的端到端时延的加和。<li>e2e_request_latency_seconds_bucket：截止目前为止，直方图分桶统计的请求的端到端时延数据。|
|request_prompt_tokens|Histogram|请求输入的token数量，代表请求输入的prompt经过tokenizer之后得到的token个数。<li>request_prompt_tokens_count：截止目前为止，完成并统计当前指标的请求个数。<li>request_prompt_tokens_sum：截止目前为止，完成并统计当前指标的所有请求的输入的token数量的加和。<li>request_prompt_tokens_bucket：截止目前为止，直方图分桶统计的请求的输入的token数量数据。|
|request_generation_tokens|Histogram|请求输出的token数量，代表请求经过模型推理之后得到的token个数。<li>request_generation_tokens_count：截止目前为止，完成并统计当前指标的请求个数。<li>request_generation_tokens_sum：截止目前为止，完成并统计当前指标的所有请求的输出的token数量的加和。<li>request_generation_tokens_bucket：截止目前为止，直方图分桶统计的请求的输出的token数量数据。|

# Controller从Coordinator处获取主备信息与请求数量

**接口功能**

查询Coordinator的主备信息与这段时间内Coordinator接收到的请求数量。

>![](public_sys-resources/icon-note.gif) **说明：** 
>该接口不需要用户调用，属于Coordinator和Controller之间的通信接口。

**接口格式**

操作类型：**GET**

```
URL：https://{ip}:{port}/recvs_info
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

无

**使用样例**

请求样例：

```
GET https://{ip}:{port}/recvs_info
```

响应样例：

```
{"is_master":false,"recv_flow":0}
```

**输出说明**

|参数|类型|说明|
|--|--|--|
|is_master|bool|必填。<br>查询的Coordinator是否为主Coordinator。|
|recv_flow|bool|必填。<br>从上次询问到本次询问期间Coordinator接收到的请求数量。|

# Controller向Coordinator同步主备状态与异常状态

**接口功能**

Controller向Coordinator同步主备状态与异常状态。

**接口格式**

操作类型：**POST**

```
URL：https://{ip}:{port}/backup_info
```

>![](public_sys-resources/icon-note.gif) **说明：** 
>-   \{ip\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_ip\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_ip"参数。
>-   \{port\}优先取[启动命令](启动调度器.md#section733210894016)参数中的\{manage\_port\}；如果没有配置该命令行参数，则取配置文件ms\_coordinator.json的"manage\_port"参数。

**请求参数**

|参数|类型|说明|
|--|--|--|
|is_master|bool|必填。<br>Controller判断该Coordinator是否为主coordinator。|
|is_abnormal|bool|必填。<br>Controller判断现在是否处于两个Coordinator都收到请求的异常情况。|


**使用样例**

请求样例：

```
POST https://{ip}:{port}/backup_info
```

请求消息体：

```
{"is_master":false,"is_abnormal":false}
```

响应样例：

```
{"update_successfully": true}
```

**输出说明**

无



