# Tracing特性

## 特性介绍

MindIE Motor Tracing能力基于第三方组件opentelemetry的能力，opentelemetry文档资料可参考[文档|OpenTelemetry](https://opentelemetry.io/zh/docs/)。

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认**
MindIE Motor Tracing能力通过opentelemetry机制实现模型服务推理链路的链路追踪功能，可对协调器、Prefill引擎、Decode引擎等模块的调用链进行端到端追踪。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 约束维度 | 要求 |
|----------|------|
| 硬件 | Atlas 800I A2 推理服务器 |
| 部署场景 | 生产环境建议将`OTEL_EXPORTER_OTLP_TRACES_INSECURE`设置为`false` |
| 引擎 | 仅支持vLLM引擎 |
| 特性互斥 | **内容缺失，需要人工补齐。** |
| 软件依赖 | 依赖第三方组件opentelemetry，链路可视化依赖Jaeger |
| 其他限制 | 需确保OTLP接收端口（4318/4317）与Jaeger页面端口（16686）未被占用，且部署环境与Jaeger服务之间网络互通 |

## 特性使用

### 环境准备

**依据原文上下文内容重组，请进行人工校验。**

- 以[MindIE Motor快速开始](../quick_start.md)中的env.json与user_config.json示例为配置基线。
- 准备Jaeger可执行文件，或采用Docker容器方式部署Jaeger（参考[Jaeger官网](https://www.jaegertracing.io/download/)）。
- 确保部署环境可访问Jaeger的OTLP接收端口（4318/4317）及Jaeger页面端口（16686）。

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

MindIE Motor开启Tracing能力需修改env.json配置文件和user_config.json配置文件后，通过deploy.py脚本即可完成服务部署，具体流程如下。

**操作步骤**

1. 配置env.json配置文件，以[MindIE Motor快速开始](../quick_start.md)中env.json为参考基线，在`motor_coordinator_env`、`motor_engine_prefill_env`、`motor_engine_decode_env`三个配置项下新增`OTEL_SERVICE_NAME`、`OTEL_EXPORTER_OTLP_TRACES_INSECURE`、`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`三个环境变量。
示例如下：

    ```json
    {
      "version": "2.0.0",
      "motor_common_env": {
      },
      "motor_controller_env": {
      },
      "motor_coordinator_env": {
        "OTEL_SERVICE_NAME": "mindie-motor",
        "OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf"
      },
      "motor_engine_prefill_env": {
        "OTEL_SERVICE_NAME": "vllm-server-p",
        "OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf"
      },
      "motor_engine_decode_env": {
        "OTEL_SERVICE_NAME": "vllm-server-d",
        "OTEL_EXPORTER_OTLP_TRACES_INSECURE": "true",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf"
      },
      "motor_kv_cache_store_env": {
      }
    }
    ```

    **依据原文上下文内容重组，请进行人工校验。**

    **表 1** 环境变量说明

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    |--------|------|----------|-----------|--------|------|
    | OTEL_SERVICE_NAME | string | 合法服务名称 | 必填 | 无 | 上报数据的服务名称，根据模块名称定义。 |
    | OTEL_EXPORTER_OTLP_TRACES_INSECURE | bool | true/false | 必填 | 无 | 是否开启非安全协议，生产环境建议设置为false。 |
    | OTEL_EXPORTER_OTLP_TRACES_PROTOCOL | string | grpc/http/protobuf | 必填 | 无 | 上报数据协议。 |

2. 配置user_config.json配置文件，以[MindIE Motor快速开始](../quick_start.md)中user_config.json为参考基线，配置以下参数。

    - 需要在`motor_coordinator_config`字段下新增`tracer_config`子字段，其中`tracer_config`下的`endpoint`为开启Tracing能力必填，其值根据env.json配置文件中的`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`环境变量值配置。
    - `motor_engine_prefill_config`、`motor_engine_decode_config`的`engine_config`下新增`otlp-traces-endpoint`配置，其值根据env.json配置文件中的`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`环境变量值配置。

    示例如下：

    ```json
    {
      "version": "v2.0",
      "motor_deploy_config": {
        "p_instances_num": 1,
        "d_instances_num": 1,
        "single_p_instance_pod_num": 1,
        "single_d_instance_pod_num": 1,
        "p_pod_npu_num": 4,
        "d_pod_npu_num": 4,
        "image_name": "",
        "job_id": "mindie-motor",
        "hardware_type": "800I_A2",
        "weight_mount_path": "/mnt/weight/"
      },
      "motor_controller_config": {
      },
      "motor_coordinator_config": {
        "tracer_config": {
          "endpoint": "http://xx.xx.xx.xx:4318/v1/traces",
          "root_sampling_rate": 1,
          "remote_parent_sampled": 1,
          "remote_parent_not_sampled": 1,
          "local_parent_sampled": 1,
          "local_parent_not_sampled": 1
        }
      },
      "motor_nodemanger_config": {
      },
      "motor_engine_prefill_config": {
        "engine_type": "vllm",
        "engine_config": {
          "served_model_name": "qwen3-8B",
          "model": "/mnt/weight/qwen3_8B",
          "gpu_memory_utilization": 0.9,
          "data_parallel_size": 2,
          "tensor_parallel_size": 2,
          "pipeline_parallel_size": 1,
          "enable_expert_parallel": false,
          "data_parallel_rpc_port": 9000,
          "otlp-traces-endpoint": "http://xx.xx.xx.xx:4318/v1/traces",
          "kv_transfer_config": {
          "kv_connector": "MooncakeConnectorV1",
          "kv_buffer_device": "npu",
          "kv_role": "kv_producer",
          "kv_connector_extra_config": {
            "use_ascend_direct": true
            }
          }
        }
      },
      "motor_engine_decode_config": {
        "engine_type": "vllm",
        "engine_config": {
          "served_model_name": "qwen3-8B",
          "model": "/mnt/weight/qwen3_8B",
          "gpu_memory_utilization": 0.9,
          "data_parallel_size": 2,
          "tensor_parallel_size": 2,
          "pipeline_parallel_size": 1,
          "enable_expert_parallel": false,
          "data_parallel_rpc_port": 9000,
          "otlp-traces-endpoint": "http://xx.xx.xx.xx:4318/v1/traces",
          "kv_transfer_config": {
          "kv_connector": "MooncakeConnectorV1",
          "kv_buffer_device": "npu",
          "kv_role": "kv_consumer",
          "kv_connector_extra_config": {
            "use_ascend_direct": true
            }
          }
        }
      }
    }
    ```

    **依据原文上下文内容重组，请进行人工校验。**

    **表 2** 配置参数说明

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    |--------|------|----------|-----------|--------|------|
    | motor_coordinator_config.tracer_config.endpoint | string | <ul><li>`http://xx.xx.xx.xx:4318/v1/traces`</li><li>`grpc://xx.xx.xx.xx:4317`</li></ul>| 必填 | 无 | 数据上报地址，根据env.json中`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`选择 |
    | motor_coordinator_config.tracer_config.root_sampling_rate | **内容缺失，需要人工补齐** |**内容缺失，需要人工补齐** | **内容缺失，需要人工补齐** | 1 | 根Span采样率。 |
    | motor_coordinator_config.tracer_config.remote_parent_sampled | **内容缺失，需要人工补齐** | **内容缺失，需要人工补齐** | **内容缺失，需要人工补齐** | 1 | 远程父Span已采样时的采样策略。 |
    | motor_coordinator_config.tracer_config.remote_parent_not_sampled | **内容缺失，需要人工补齐** | **内容缺失，需要人工补齐** | **内容缺失，需要人工补齐** | 1 | 远程父Span未采样时的采样策略。 |
    | motor_coordinator_config.tracer_config.local_parent_sampled | **内容缺失，需要人工补齐** | **内容缺失，需要人工补齐** | **内容缺失，需要人工补齐** | 1 | 本地父Span已采样时的采样策略。 |
    | motor_coordinator_config.tracer_config.local_parent_not_sampled | **内容缺失，需要人工补齐** | **内容缺失，需要人工补齐** | **内容缺失，需要人工补齐** | 1 | 本地父Span未采样时的采样策略。 |
    | motor_engine_prefill_config.engine_config.otlp-traces-endpoint | string | <ul><li>`http://xx.xx.xx.xx:4318/v1/traces`</li><li>`grpc://xx.xx.xx.xx:4317`</li></ul> | 必填 | 无 | Prefill引擎数据上报地址。 |
    | motor_engine_decode_config.engine_config.otlp-traces-endpoint | string | <ul><li>`http://xx.xx.xx.xx:4318/v1/traces`</li><li>`grpc://xx.xx.xx.xx:4317`</li></ul> | 必填 | 无 | Decode引擎数据上报地址。 |

3. 部署服务。

    在examples/deployer目录下通过deploy.py脚本部署服务。支持指定配置目录或单独指定配置文件：

      ```bash
      cd examples/deployer
      # 方式一：指定配置目录（推荐）
      python deploy.py --config_dir ../infer_engines/vllm

      # 方式二：单独指定配置文件
      python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
      ```

    **内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

    服务部署成功，coordinator、engine等模块正常启动，启动日志中出现Tracing上报相关配置加载成功的信息。（**补充表示部署成功的回显信息**）

4. 部署Jaeger。

    参考[Jaeger文档](https://www.jaegertracing.io/docs/2.14/)下载好可执行文件后，在服务器上执行以下命令即可。也可采用Docker容器方式，具体参考[Jaeger官网](https://www.jaegertracing.io/download/)：

    ```bash
    ./jaeger --set receivers.otlp.protocols.http.endpoint=0.0.0.0:4318 --set receivers.otlp.protocols.grpc.endpoint=0.0.0.0:4317 &
    ```

    启动后通过浏览器打开对应IP的16686端口网页，可查看服务调用链效果，效果如下：

    ![Snipaste_2026-03-31_20-59-16.jpg](https://raw.gitcode.com/user-images/assets/9428015/e2338b5c-646f-4349-b62b-1dae9c95b217/Snipaste_2026-03-31_20-59-16.jpg 'Snipaste_2026-03-31_20-59-16.jpg')

    ![Snipaste_2026-03-31_20-59-24.jpg](https://raw.gitcode.com/user-images/assets/9428015/85f73899-a6cd-4667-837d-300b432d2e2a/Snipaste_2026-03-31_20-59-24.jpg 'Snipaste_2026-03-31_20-59-24.jpg')

### 验证特性

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

1. 向已部署的推理服务发起一次推理请求，例如：

   ```bash
   curl -X POST http://<服务IP>:<服务端口>/v1/chat/completions \
        -H "Content-Type: application/json" \
        -d '{"model":"qwen3-8B","messages":[{"role":"user","content":"hello"}]}'
   ```

2. 打开Jaeger页面（http://<服务IP>:16686），在服务列表中选择mindie-motor、vllm-server-p或vllm-server-d，查看本次请求对应的调用链。

3. Jaeger页面出现本次请求对应的Trace及Span数据，Span覆盖协调器、Prefill引擎、Decode引擎等模块，说明Tracing能力已生效。

## 常见问题

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

### Jaeger页面未出现Trace数据

**问题描述**

服务部署完成后，在Jaeger页面（16686端口）查询不到请求对应的Trace数据。

**原因分析**

`tracer_config.endpoint`或`otlp-traces-endpoint`配置的地址与Jaeger的OTLP接收端口（4318/4317）不一致，或对应端口被占用、网络不通。

**解决步骤**

核对env.json中`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`与user_config.json中`endpoint`、`otlp-traces-endpoint`的协议及地址一致，确认Jaeger启动参数中的`receivers.otlp.protocols.http.endpoint=0.0.0.0:4318`、`receivers.otlp.protocols.grpc.endpoint=0.0.0.0:4317`与上报地址一致后，重新执行deploy.py部署服务。

### Jaeger启动时报端口被占用

**问题描述**

启动Jaeger时提示OTLP接收端口（4318/4317）或页面端口（16686）被占用，服务无法启动。

**原因分析**

相关端口已被其他进程占用。

**解决步骤**

通过以下命令查找并结束占用进程后重新启动：

```bash
netstat -ano | findstr 4318
taskkill /PID <PID> /F
```
