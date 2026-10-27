# Tracing Feature

## Feature Introduction

The MindIE Motor Tracing capability is based on the capabilities of the third-party component opentelemetry. For opentelemetry documentation, see [Documentation|OpenTelemetry](https://opentelemetry.io/docs/).

MindIE Motor records request processing and routing spans in the Coordinator, and propagates the request context through W3C `traceparent` / `tracestate`. If the engine supports context propagation and OTLP span export, the processing of the Coordinator, Prefill, and Decode can be correlated in the same trace, which helps locate request latency and cross-component calls. The following uses vLLM configuration items and a Jaeger receiver as an example. The number and attributes of engine spans depend on the engine implementation and configuration.

### Constraints and Limitations

| Constraint | Requirement |
|----------|------|
| Hardware | No special requirements. Tracing collects and exports data in the Coordinator and engine processes, and the inference hardware requirements are determined by the selected engine. |
| Deployment scenario | Both the Coordinator and the engines to be traced must be configured with a reachable OTLP receiver. In the production environment, use TLS. For the configuration method, see the example. |
| Engine | Tracing on the Motor side is independent of the engine type. A complete trace requires the engine to support W3C context propagation and OTLP span export. If only the Coordinator is configured, only its own spans can be collected. The following uses vLLM as an example to demonstrate the writing of configuration items such as `otlp-traces-endpoint`. |
| Feature mutual exclusion | No known mutually exclusive features. |
| Software dependency | The corresponding OpenTelemetry SDK/exporter must be installed in the Coordinator and engine environments. The receiver must support the selected OTLP protocol. Jaeger is only an example in this document, and other OTLP-compatible backends can also be used. |
| Other restrictions | Ensure that the OTLP receiving ports (4318/4317) and the Jaeger page port (16686) are not occupied, and that the deployment environment can communicate with the Jaeger service over the network. |

## Using the Feature

### Environment Preparation

- Use the env.json and user_config.json examples in [MindIE Motor Quick Start](../quick_start.md) as the configuration baseline.

- Prepare the Jaeger executable file, or deploy it as a Docker container (see the [Jaeger official website](https://www.jaegertracing.io/download/) and the documentation of the version in use). Other OTLP-compatible trace backends can also be used. Start the receiver first according to the receiver startup method described below, and then deploy the inference service.

- Ensure that the deployment environment can access the Jaeger OTLP receiving ports (4318/4317) and the Jaeger page port (16686).

### Usage Example

To enable the Tracing capability of MindIE Motor, modify the env.json configuration file and the user_config.json configuration file, and then deploy the service by using the deploy.py script. The specific process is as follows.

**Procedure**

1. Configure the env.json configuration file. Using env.json in [MindIE Motor Quick Start](../quick_start.md) as the reference baseline, add the three environment variables `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_TRACES_INSECURE`, and `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` under the three configuration items `motor_coordinator_env`, `motor_engine_prefill_env`, and `motor_engine_decode_env`.

   Example:

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

   **Table 1** Environment variable description

   | Configuration Item | Type | Value Range | Mandatory/Optional | Default Value | Description |
   |--------|------|----------|-----------|--------|------|
   | OTEL_SERVICE_NAME | string | Non-empty service name | Explicit configuration recommended | Not set by Motor, determined by the SDK | The service name is used for retrieval on the backend. It is recommended to name the Coordinator, P, and D separately. |
   | OTEL_EXPORTER_OTLP_TRACES_INSECURE | string | "true" / "false" | Optional | "false" (OTel gRPC default) | Configuration for gRPC transport. The security of HTTP is determined by the http/https scheme of the endpoint. |
   | OTEL_EXPORTER_OTLP_TRACES_PROTOCOL | string | grpc / http/protobuf | Explicit configuration recommended | grpc for the Coordinator | The protocol must match the receiver. Set it explicitly in the engine environment as well to avoid different default values across versions. |

   Environment variable values in `env.json` must be strings. The example is an unencrypted test link. In the production environment, use an `https://` endpoint, enable TLS on the receiver, and configure `OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE` and certificate mounting as required. Changing only `OTEL_EXPORTER_OTLP_TRACES_INSECURE` to `"false"` does not turn an `http://` OTLP/HTTP request into HTTPS. For the protocol and certificate semantics, see [OTel exporter configuration](https://opentelemetry.io/docs/specs/otel/protocol/exporter/).

2. Configure the user_config.json configuration file. Using user_config.json in [MindIE Motor Quick Start](../quick_start.md) as the reference baseline, configure the following parameters.

   - Add a `tracer_config` subfield under the `motor_coordinator_config` field. The `endpoint` under `tracer_config` is mandatory for enabling the Tracing capability, and its value is configured according to the value of the `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` environment variable in the env.json configuration file.

   - Add the `otlp-traces-endpoint` configuration under `engine_config` of `motor_engine_prefill_config` and `motor_engine_decode_config`, and its value is configured according to the value of the `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` environment variable in the env.json configuration file.

   Example:

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

   **Table 2** Configuration parameter description

   | Configuration Item | Type | Value Range | Mandatory/Optional | Default Value | Description |
   |--------|------|----------|-----------|--------|------|
   | motor_coordinator_config.tracer_config.endpoint | string | Reachable URL that matches the protocol | Mandatory when Coordinator Tracing is enabled | Empty string | <ul><li>HTTP example: `http://<receiver>:4318/v1/traces`;</li><li>gRPC example: `http://<receiver>:4317`;</li></ul>Use HTTPS for TLS |
   | motor_coordinator_config.tracer_config.root_sampling_rate | number | (0, 1] | Optional | 1.0 | Sampling ratio when there is no parent span |
   | motor_coordinator_config.tracer_config.remote_parent_sampled | number | (0, 1] | Optional | 1.0 | Sampling ratio when the remote parent span is sampled |
   | motor_coordinator_config.tracer_config.remote_parent_not_sampled | number | (0, 1] | Optional | 1.0 | Sampling ratio when the remote parent span is not sampled |
   | motor_coordinator_config.tracer_config.local_parent_sampled | number | (0, 1] | Optional | 1.0 | Sampling ratio when the local parent span is sampled |
   | motor_coordinator_config.tracer_config.local_parent_not_sampled | number | (0, 1] | Optional | 1.0 | Sampling ratio when the local parent span is not sampled |
   | motor_engine_prefill_config.engine_config.otlp-traces-endpoint | string | Reachable URL that matches the protocol | Mandatory when Tracing is enabled for this engine | Not configured | Reporting address of the prefill engine. Fill it in according to the HTTP/gRPC protocol. |
   | motor_engine_decode_config.engine_config.otlp-traces-endpoint | string | Reachable URL that matches the protocol | Mandatory when Tracing is enabled for this engine | Not configured | Reporting address of the decode engine. Fill it in according to the HTTP/gRPC protocol. |

   The current Motor configuration validation requires all five sampling ratios to be greater than 0, and the OTel sampler requires them not to exceed 1. Therefore, the available range is `(0, 1]`. To disable Coordinator Tracing, set `endpoint` to an empty string instead of setting the sampling ratio to 0. The default value 1.0 is used for full sampling, including the case where the parent span is not sampled. In the production environment, evaluate the export and storage overhead before adjusting the sampling ratios, and use them together with a TLS endpoint (see the preceding table and the env.json description). Motor Tracing has no additional independent tuning parameters, and the main configuration items are the endpoint and the five sampling ratios listed in Table 2.

3. Start the Jaeger receiver.

   Obtain the executable file or image that matches the deployment environment from the [Jaeger official website](https://www.jaegertracing.io/download/), and enable OTLP receiving according to the documentation of the version in use. The following uses the all-in-one executable file as an example (HTTP 4318, gRPC 4317, UI 16686). The startup parameters may differ across versions, and the official documentation prevails.

   You can also deploy it as a Docker container:

   ```bash
   ./jaeger --set receivers.otlp.protocols.http.endpoint=0.0.0.0:4318 --set receivers.otlp.protocols.grpc.endpoint=0.0.0.0:4317 &
   ```

   After startup, open the web page at port 16686 of the corresponding IP in a browser to view the service call chain. The effect is as follows:

   ![Snipaste_2026-03-31_20-59-16.jpg](https://raw.gitcode.com/user-images/assets/9428015/e2338b5c-646f-4349-b62b-1dae9c95b217/Snipaste_2026-03-31_20-59-16.jpg 'Snipaste_2026-03-31_20-59-16.jpg')

   ![Snipaste_2026-03-31_20-59-24.jpg](https://raw.gitcode.com/user-images/assets/9428015/85f73899-a6cd-4667-837d-300b432d2e2a/Snipaste_2026-03-31_20-59-24.jpg 'Snipaste_2026-03-31_20-59-24.jpg')

4. Deploy the service.

   Deploy the service by using the deploy.py script in the examples/deployer directory. You can specify a configuration directory or specify configuration files separately:

   ```bash
   cd examples/deployer
   # Method 1: Specify the configuration directory (recommended)
   python deploy.py --config_dir ../../model_configs/vllm

   # Method 2: Specify configuration files separately
   python deploy.py --user_config_path ../../model_configs/vllm/user_config.json --env_config_path ../../model_configs/vllm/env.json
   ```

   The Coordinator log should contain the following fragment (the endpoint varies with the configuration; the following is a format example):

   ```text
   TracerManager init.(enable:True,endpoint:http://<receiver>:4318/v1/traces,protocol:http/protobuf)
   ```

   This log only indicates that the Coordinator has enabled the exporter, and does not prove that the receiver has received data. Service readiness and end-to-end traces still need to be checked according to [Verification](#verification).

### Verification

1. Send an inference request to the deployed inference service.

   For example:

   ```bash
   curl -X POST "http://<service IP>:<service port>/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d '{"model":"qwen3-8B","messages":[{"role":"user","content":"hello"}]}'
   ```

2. After the batch export is complete, open the page `http://<Jaeger-IP>:16686` on the host where Jaeger is located, and query by service name and the time range in which the request occurred. The Jaeger host is not necessarily the same as the inference service host.

3. Confirm the Coordinator and the P/D engine spans that the request actually went through under the same Trace ID. Seeing only three service names does not mean that the trace is already correlated. First accept the full-sampling configuration in the example. When data is missing, check the endpoint, protocol, context propagation, and exporter error logs of each process.

## FAQs

### No Trace Data Is Displayed on the Jaeger Page

**Symptom**

After the service is deployed, no trace data corresponding to the request can be queried on the Jaeger page (port 16686).

**Cause Analysis**

The addresses configured in `tracer_config.endpoint` or `otlp-traces-endpoint` are inconsistent with the Jaeger OTLP receiving ports (4318/4317), or the corresponding ports are occupied or the network is unreachable.

**Solution**

Check that the protocol and address of `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` in env.json are consistent with those of `endpoint` and `otlp-traces-endpoint` in user_config.json, confirm that `receivers.otlp.protocols.http.endpoint=0.0.0.0:4318` and `receivers.otlp.protocols.grpc.endpoint=0.0.0.0:4317` in the Jaeger startup parameters are consistent with the reporting address, and then run deploy.py again to deploy the service.

### Port Already in Use When Jaeger Starts

**Symptom**

When Jaeger is started, a message is displayed indicating that the OTLP receiving ports (4318/4317) or the page port (16686) are already in use, and the service cannot start.

**Cause Analysis**

The related ports are occupied by other processes.

**Solution**

Locate the listening process on the Linux host where Jaeger runs:

```bash
ss -ltnp | grep -E ':(4317|4318|16686)[[:space:]]'
```

First confirm the process ownership. If a Jaeger instance is already available, reuse its endpoints. If the ports belong to other services, select unoccupied ports for Jaeger and update the endpoints of the Coordinator and the engines accordingly. If an existing service must be stopped, use its original service management method, and do not forcibly terminate unknown processes.
