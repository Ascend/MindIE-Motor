# Tracing Capability Deployment

## Feature Description

The pyMotor tracing capability is based on the third-party component OpenTelemetry. For details about the OpenTelemetry documentation, see [OpenTelemetry](https://opentelemetry.io/docs/).

After modifying the `env.json` and `user_config.json` configuration files, you can deploy the service using the `deploy.py` script.

## Deployment Process

To enable tracing in pyMotor, modify the `env.json` and `user_config.json` configuration files, then deploy the service using the `deploy.py` script. The detailed process is as follows:

### Configuring `env.json`

Using the `env.json` example from [pyMotor Quick Start](./quick_start.md) as a baseline, the following configuration sample enables tracing capabilities:

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

Under each of the three configuration items—`motor_coordinator_env`, `motor_engine_prefill_env`, and `motor_engine_decode_env`—add the following three environment variables: `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_TRACES_INSECURE`, and `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`.
Environment variables:

- `OTEL_SERVICE_NAME`: The service name for the reported data. Set this according to the module name; refer to the provided examples.
- `OTEL_EXPORTER_OTLP_TRACES_INSECURE`: Whether to enable the insecure protocol. For production environments, it is advised to set this to `false`.
- `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`: The protocol used for reporting data. Options are `grpc` and `http/protobuf`. Set this based on your development practices.

### Configuring·`user_config.json`

Using the `user_config.json` example from the [PyMotor Quick Start](../user_guide/quick_start.md) as a baseline, the following configuration file demonstrates how to enable tracing:

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
    "model_config": {
      "model_name": "qwen3-8B",
      "model_path": "/mnt/weight/qwen3_8B",
      "npu_mem_utils": 0.9,
      "prefill_parallel_config": {
        "dp_size": 2,
        "tp_size": 2,
        "pp_size": 1,
        "enable_ep": false,
        "dp_rpc_port": 9000,
        "world_size": 4
      }
    },
    "engine_config": {
      "otlp-traces-endpoint": "http://xx.xx.xx.xx:4318/v1/traces",
      "kv_transfer_config": {
       "kv_connector": "MooncakeLayerwiseConnector",
       "kv_buffer_device": "npu",
       "kv_role": "kv_consumer",
       "kv_connector_module_path": "vllm_ascend.distributed.mooncake_layerwise_connector",
       "kv_connector_extra_config": {
         "use_ascend_direct": true
        }
      }
    }
  },
  "motor_engine_decode_config": {
    "engine_type": "vllm",
    "model_config": {
      "model_name": "qwen3-8B",
      "model_path": "/mnt/weight/qwen3_8B",
      "npu_mem_utils": 0.9,
      "prefill_parallel_config": {
        "dp_size": 2,
        "tp_size": 2,
        "pp_size": 1,
        "enable_ep": false,
        "dp_rpc_port": 9000,
        "world_size": 4
      }
    }
  },
  "engine_config": {
    "otlp-traces-endpoint": "http://xx.xx.xx.xx:4318/v1/traces",
    "kv_transfer_config": {
     "kv_connector": "MooncakeLayerwiseConnector",
     "kv_buffer_device": "npu",
     "kv_role": "kv_consumer",
     "kv_connector_module_path": "vllm_ascend.distributed.mooncake_layerwise_connector",
     "kv_connector_extra_config": {
       "use_ascend_direct": true,
      }
    }
  }
}
```

- Under `motor_coordinator_config`, add a new `tracer_config` item. The `endpoint` field under `tracer_config` is required to enable tracing. Its value depends on the `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` setting in `env.json` and can be either `http://xx.xx.xx.xx:4318/v1/traces` or `grpc://xx.xx.xx.xx:4317`.
- Under `motor_engine_prefill_env` and `motor_engine_decode_env`, add the `otlp-traces-endpoint` configuration. Populate it using the same method as the `endpoint` field.

### Deploying a Service

Deploy the service using the `deploy.py` script located in the `examples/deployer` directory. You can specify a configuration directory or a configuration file separately.

```bash
cd examples/deployer
# (Recommended) Method 1: Specify a configuration directory
python deploy.py --config_dir ../infer_engines/vllm

# Method 2: Specify the configuration file separately.
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

### Deploying Jaeger

For details, see the [Jaeger documentation](https://www.jaegertracing.io/docs/2.14/).
After downloading the executable file, run the following command on the server: You can also use the Docker container. For details, see the [Jaeger official website](https://www.jaegertracing.io/download/).

```bash
./jaeger --set receivers.otlp.protocols.http.endpoint=0.0.0.0:4318 --set receivers.otlp.protocols.grpc.endpoint=0.0.0.0:4317 &
```

After running, open `http://<IP>:16686` in a browser. The resulting page should appear as shown below:

![Snipaste_2026-03-31_20-59-16.jpg](https://raw.gitcode.com/user-images/assets/9428015/e2338b5c-646f-4349-b62b-1dae9c95b217/Snipaste_2026-03-31_20-59-16.jpg 'Snipaste_2026-03-31_20-59-16.jpg')

---

![Snipaste_2026-03-31_20-59-24.jpg](https://raw.gitcode.com/user-images/assets/9428015/85f73899-a6cd-4667-837d-300b432d2e2a/Snipaste_2026-03-31_20-59-24.jpg 'Snipaste_2026-03-31_20-59-24.jpg')

---
