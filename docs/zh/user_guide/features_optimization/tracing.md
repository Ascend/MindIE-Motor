# Tracing特性

## 特性介绍

MindIE Motor Tracing能力基于第三方组件opentelemetry的能力，opentelemetry文档资料可参考[文档|OpenTelemetry](https://opentelemetry.io/zh/docs/)。

MindIE Motor 在 Coordinator 中记录请求处理与路由 Span，并通过 W3C `traceparent` / `tracestate` 传播请求上下文。若引擎支持上下文传播与 OTLP Span 导出，可在同一 Trace 中关联 Coordinator、Prefill 和 Decode 的处理过程，用于定位请求耗时与跨组件调用。下文以 vLLM 配置项与 Jaeger 接收端为例演示；引擎 Span 的数量和属性取决于引擎实现与配置。

### 约束与限制

| 约束维度 | 要求 |
|----------|------|
| 硬件 | 无特殊要求。Tracing 在 Coordinator 与引擎进程中采集并导出，推理硬件要求由所选引擎决定。 |
| 部署场景 | Coordinator 和需要追踪的引擎都应配置可达的 OTLP 接收端；生产环境使用 TLS，配置方法见下文。 |
| 引擎 | Motor 侧 Tracing 与引擎类型无关；完整链路要求引擎支持 W3C 上下文传播与 OTLP Span 导出。仅配置 Coordinator 时可采集其自身 Span。下文以 vLLM 为例演示 `otlp-traces-endpoint` 等配置项写法。 |
| 特性互斥 | 无已知互斥特性。 |
| 软件依赖 | Coordinator 和引擎环境需安装相应 OpenTelemetry SDK/exporter；接收端须支持所选 OTLP 协议。Jaeger 是本文展示示例，也可使用兼容 OTLP 的其他后端。 |
| 其他限制 | 需确保OTLP接收端口（4318/4317）与Jaeger页面端口（16686）未被占用，且部署环境与Jaeger服务之间网络互通。 |

## 特性使用

### 环境准备

- 以[MindIE Motor快速开始](../quick_start.md)中的env.json与user_config.json示例为配置基线。
- 准备 Jaeger 可执行文件，或采用 Docker 容器部署（参考 [Jaeger 官网](https://www.jaegertracing.io/download/)及所用版本的文档）。也可使用其他兼容 OTLP 的 Trace 后端。按下文的接收端启动方法先启动接收端，再部署推理服务。
- 确保部署环境可访问Jaeger的OTLP接收端口（4318/4317）及Jaeger页面端口（16686）。

### 使用样例

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

   **表 1** 环境变量说明

   | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
   |--------|------|----------|-----------|--------|------|
   | OTEL_SERVICE_NAME | string | 非空服务名称 | 建议显式配置 | Motor 未设置，由 SDK 决定 | 服务名称用于后端检索；Coordinator、P、D 建议分别命名 |
   | OTEL_EXPORTER_OTLP_TRACES_INSECURE | string | "true" / "false" | 选填 | "false"（OTel gRPC 默认） | gRPC 传输配置；HTTP 的安全性由 endpoint 的 http/https scheme 决定 |
   | OTEL_EXPORTER_OTLP_TRACES_PROTOCOL | string | grpc / http/protobuf | 建议显式配置 | Coordinator 为 grpc | 协议须与接收端匹配；引擎环境也显式设置，避免各版本默认值不同 |

   `env.json` 中环境变量值使用字符串。示例为不加密的测试链路；生产环境使用 `https://` endpoint，并在接收端启用 TLS，按需要配置 `OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE` 和证书挂载。仅将 `OTEL_EXPORTER_OTLP_TRACES_INSECURE` 改为 `"false"`，不会把 `http://` 的 OTLP/HTTP 请求变成 HTTPS。协议与证书语义见 [OTel exporter 配置](https://opentelemetry.io/docs/specs/otel/protocol/exporter/)。

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
          "data_parallel_size": 1,
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
          "data_parallel_size": 1,
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

   **表 2** 配置参数说明

   | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
   |--------|------|----------|-----------|--------|------|
   | motor_coordinator_config.tracer_config.endpoint | string | 与协议匹配的可达 URL | 开启 Coordinator Tracing 时必填 | 空字符串 | HTTP 示例 `http://<接收端>:4318/v1/traces`；gRPC 示例 `http://<接收端>:4317`；TLS 使用 https |
   | motor_coordinator_config.tracer_config.root_sampling_rate | number | (0, 1] | 选填 | 1.0 | 没有父 Span 时的采样比例 |
   | motor_coordinator_config.tracer_config.remote_parent_sampled | number | (0, 1] | 选填 | 1.0 | 远端父 Span 已采样时的采样比例 |
   | motor_coordinator_config.tracer_config.remote_parent_not_sampled | number | (0, 1] | 选填 | 1.0 | 远端父 Span 未采样时的采样比例 |
   | motor_coordinator_config.tracer_config.local_parent_sampled | number | (0, 1] | 选填 | 1.0 | 本地父 Span 已采样时的采样比例 |
   | motor_coordinator_config.tracer_config.local_parent_not_sampled | number | (0, 1] | 选填 | 1.0 | 本地父 Span 未采样时的采样比例 |
   | motor_engine_prefill_config.engine_config.otlp-traces-endpoint | string | 与协议匹配的可达 URL | 开启该引擎 Tracing 时必填 | 未配置 | prefill 引擎上报地址，按 HTTP/gRPC 协议分别填写 |
   | motor_engine_decode_config.engine_config.otlp-traces-endpoint | string | 与协议匹配的可达 URL | 开启该引擎 Tracing 时必填 | 未配置 | decode 引擎上报地址，按 HTTP/gRPC 协议分别填写 |

   当前 Motor 配置校验要求五个采样率均大于 0，OTel 采样器要求不超过 1，因此可用范围是 `(0, 1]`。关闭 Coordinator Tracing 时将 `endpoint` 置为空字符串，不要把采样率设为 0。默认值 1.0 用于全量采样，包括父 Span 未采样的情况；生产环境应评估导出和存储开销后调整采样率，并配合 TLS endpoint 使用（见上表及 env.json 说明）。Motor Tracing 无额外独立调优参数，主要配置项即表 2 所列 endpoint 与五个采样率。

3. 启动 Jaeger 接收端。

   从 [Jaeger 官网](https://www.jaegertracing.io/download/) 获取与部署环境匹配的可执行文件或镜像，按所用版本的文档启用 OTLP 接收。以下以 all-in-one 可执行文件为例（HTTP 4318、gRPC 4317、UI 16686）；不同版本的启动参数可能不同，以官方文档为准。也可采用 Docker 容器方式部署：

   ```bash
   ./jaeger --set receivers.otlp.protocols.http.endpoint=0.0.0.0:4318 --set receivers.otlp.protocols.grpc.endpoint=0.0.0.0:4317 &
   ```

   启动后通过浏览器打开对应IP的16686端口网页，可查看服务调用链效果，效果如下：

   ![Snipaste_2026-03-31_20-59-16.jpg](https://raw.gitcode.com/user-images/assets/9428015/e2338b5c-646f-4349-b62b-1dae9c95b217/Snipaste_2026-03-31_20-59-16.jpg 'Snipaste_2026-03-31_20-59-16.jpg')

   ![Snipaste_2026-03-31_20-59-24.jpg](https://raw.gitcode.com/user-images/assets/9428015/85f73899-a6cd-4667-837d-300b432d2e2a/Snipaste_2026-03-31_20-59-24.jpg 'Snipaste_2026-03-31_20-59-24.jpg')

4. 部署服务。

   在examples/deployer目录下通过deploy.py脚本部署服务。支持指定配置目录或单独指定配置文件：

   ```bash
   cd examples/deployer
   # 方式一：指定配置目录（推荐）
   python deploy.py --config_dir ../infer_engines/vllm

   # 方式二：单独指定配置文件
   python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
   ```

   Coordinator 日志应包含以下片段（endpoint 随配置变化；以下为格式示例）：

   ```text
   TracerManager init.(enable:True,endpoint:http://<接收端>:4318/v1/traces,protocol:http/protobuf)
   ```

   该日志仅表示 Coordinator 已启用 exporter，不证明接收端已收到数据。服务就绪和端到端 Trace 仍需按“验证特性”检查。

### 验证特性

1. 向已部署的推理服务发起一次推理请求，例如：

   ```bash
   curl -X POST "http://<服务IP>:<服务端口>/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"qwen3-8B","messages":[{"role":"user","content":"hello"}]}'
   ```

2. 等待批量导出后，打开 Jaeger 所在主机的页面 `http://<Jaeger-IP>:16686`，按服务名和请求发生的时间范围查询。Jaeger 主机不一定与推理服务主机相同。

3. 在同一个 Trace ID 下确认 Coordinator 及该请求实际经过的 P/D 引擎 Span。仅看到三个服务名不表示链路已经串联。先以示例的全量采样配置验收；出现数据缺失时，检查各进程的 endpoint、协议、上下文传播和 exporter 错误日志。

## 常见问题

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

在运行 Jaeger 的 Linux 主机上定位监听进程：

```bash
ss -ltnp | grep -E ':(4317|4318|16686)[[:space:]]'
```

先确认进程归属。若已有可用 Jaeger，复用其端点；若属于其他服务，可为 Jaeger 选择未占用端口，并同步修改 Coordinator 和引擎的 endpoint。确需停止已有服务时，使用其原有服务管理方式，不直接强制结束不明进程。
