# tracing能力部署

## 特性介绍

pyMotor tracing能力基于三方件`opentelemetry`能力，`opentelemetry`文档资料可参考[文档|OpenTelemetry](https://opentelemetry.io/zh/docs/)。

通过修改env.json配置文件和user_config.json配置文件后即可通过deploy.py脚本完成服务部署。

## 部署流程

pyMotor开启tracing能力需修改env.json配置文件和user_config.json配置文件后，通过deploy.py脚本即可完成服务部署，具体流程如下。

### 配置env.json

以[pyMotor快速开始](../quick_start.md)中实例env.json为参考基线，适配打开tracing能力后的配置文件示例如下：

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
  "motor_kv_cache_pool_env": {
  }
}
```

需要在`motor_coordinator_env`、`motor_engine_prefill_env`、`motor_engine_decode_env`三个配置项下新增`OTEL_SERVICE_NAME`、`OTEL_EXPORTER_OTLP_TRACES_INSECURE`、`OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`三个环境变量。
环境变量含义：

- `OTEL_SERVICE_NAME`上报数据的服务名称，根据模块名称定义，建议参考样例。
- `OTEL_EXPORTER_OTLP_TRACES_INSECURE`是否开启非安全协议，生产环境建议设置为`false`。
- `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`上报数据协议，可选`grpc`和``http/protobuf``。根据实际开发习惯设置。

### 配置user_config.json

以[PyMotor快速开始](../quick_start.md)中实例user_config.json为参考基线，适配打开tracing能力后的配置文件示例如下：

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
       "kv_connector": "MooncakeLayerwiseConnector",
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
       "kv_connector": "MooncakeLayerwiseConnector",
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

- 需要在`motor_coordinator_config`下新增`tracer_config`，`tracer_config`下的`endpoint`配置为开启tracing能力必填，填写内容根据env.json中的 `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`配置，可选： `http://xx.xx.xx.xx:4318/v1/traces`或`grpc://xx.xx.xx.xx:4317`
- `motor_engine_prefill_config`、`motor_engine_decode_config`的`engine_config`下新增`otlp-traces-endpoint`配置，填写方法同`endpoint`

### 部署服务

在 `examples/deployer` 目录下通过 deploy.py 脚本部署服务。支持指定配置目录或单独指定配置文件：

```bash
cd examples/deployer
# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

### 部署jaeger

参考[jaeger文档](https://www.jaegertracing.io/docs/2.14/)
下载好可执行文件后，在服务器上执行以下命令即可。也可采用docker容器方式，具体参考[jaeger官网](https://www.jaegertracing.io/download/)

```bash
./jaeger --set receivers.otlp.protocols.http.endpoint=0.0.0.0:4318 --set receivers.otlp.protocols.grpc.endpoint=0.0.0.0:4317 &
```

运行后通过浏览器打开对应IP的16686端口网页，效果如下：

![Snipaste_2026-03-31_20-59-16.jpg](https://raw.gitcode.com/user-images/assets/9428015/e2338b5c-646f-4349-b62b-1dae9c95b217/Snipaste_2026-03-31_20-59-16.jpg 'Snipaste_2026-03-31_20-59-16.jpg')

---

![Snipaste_2026-03-31_20-59-24.jpg](https://raw.gitcode.com/user-images/assets/9428015/85f73899-a6cd-4667-837d-300b432d2e2a/Snipaste_2026-03-31_20-59-24.jpg 'Snipaste_2026-03-31_20-59-24.jpg')

---
