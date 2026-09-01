# Tracing Capability Deployment

## Feature Introduction

The MindIE Motor Tracing capability is based on the third-party component `opentelemetry`. For the `opentelemetry` documentation, see [Documentation|OpenTelemetry](https://opentelemetry.io/docs/).

After modifying the `env.json` configuration file and the `user_config.json` configuration file, you can complete service deployment by running the `deploy.py` script.

## Deployment Process

To enable the Tracing capability of MindIE Motor, the `env.json` configuration file and the `user_config.json` configuration file must be modified, after which the service can be deployed by using the `deploy.py` script. The specific process is as follows.

### Configuring `env.json`

Using the `env.json` instance in [MindIE Motor Quick Start](../quick_start.md) as the reference baseline, the configuration file example after enabling the Tracing capability is as follows.

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

You need to add the following three environment variables under the three configuration items `motor_coordinator_env`, `motor_engine_prefill_env`, and `motor_engine_decode_env`:

- `OTEL_SERVICE_NAME`: the service name for reporting data, defined based on the module name. It is recommended to refer to the sample.

- `OTEL_EXPORTER_OTLP_TRACES_INSECURE`: whether to enable the insecure protocol. It is recommended to set it to `false` in the production environment.

- `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`: the protocol for reporting data. The options are `grpc` and `http/protobuf`. Set it according to your actual development habits.

### Configuring `user_config.json`

Using the `user_config.json` instance in [MindIE Motor Quick Start](../quick_start.md) as the reference baseline, the configuration file example after enabling the Tracing capability is as follows.

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

- You need to add `tracer_config` under `motor_coordinator_config`. The `endpoint` under `tracer_config` is mandatory for enabling the Tracing capability. The value is configured according to `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` in `env.json`, and can be `http://xx.xx.xx.xx:4318/v1/traces` or `grpc://xx.xx.xx.xx:4317`.

- Add the `otlp-traces-endpoint` configuration under `engine_config` of `motor_engine_prefill_config` and `motor_engine_decode_config`. The value is configured in the same way as `endpoint`.

### Deploying the Service

Deploy the service by using the `deploy.py` script in the `examples/deployer` directory. You can specify a configuration directory or specify configuration files separately.

```bash
cd examples/deployer
# (Recommended) Method 1: Specify the configuration directory
python deploy.py --config_dir ../infer_engines/vllm

# Method 2: Specify configuration files separately
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

### Deploying jaeger

Refer to the [jaeger documentation](https://www.jaegertracing.io/docs/2.14/). After downloading the executable file, run the following command on the server. You can also use the Docker container method. For details, see the [jaeger official website](https://www.jaegertracing.io/download/).

```bash
./jaeger --set receivers.otlp.protocols.http.endpoint=0.0.0.0:4318 --set receivers.otlp.protocols.grpc.endpoint=0.0.0.0:4317 &
```

After running, open the page at port 16686 of the corresponding IP in a browser.

![Snipaste_2026-03-31_20-59-16.jpg](https://raw.gitcode.com/user-images/assets/9428015/e2338b5c-646f-4349-b62b-1dae9c95b217/Snipaste_2026-03-31_20-59-16.jpg 'Snipaste_2026-03-31_20-59-16.jpg')

![Snipaste_2026-03-31_20-59-24.jpg](https://raw.gitcode.com/user-images/assets/9428015/85f73899-a6cd-4667-837d-300b432d2e2a/Snipaste_2026-03-31_20-59-24.jpg 'Snipaste_2026-03-31_20-59-24.jpg')
