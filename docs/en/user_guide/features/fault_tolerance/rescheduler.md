# Fault-Scenario Rescheduling

When the fault-scenario rescheduling function is enabled, in fault scenarios where the inference process is abnormally interrupted due to a failure on an inference node or an abnormal disconnection between the `Coordinator` and an inference node, the `Coordinator` reschedules the inference request to another healthy inference node to continue completing the inference task.

## Configuration Parameters

The fault-scenario rescheduling function uses the following configuration parameters in the [`user_config.json`](../../configuration/config_reference.md#motor_coordinator_config) configuration file:

- Fault-scenario rescheduling function switch: uses the `enable` configuration parameter in `reschedule_config`, which defaults to `false`;

  - `false`: disables the fault-scenario rescheduling function.

  - `true`: enables the fault-scenario rescheduling function.

- Number of rescheduling attempts: uses the `transport_max_retry` configuration parameter;

  - When the `transport_max_retry` configuration parameter is empty, the `max_retry` configuration parameter is used.

- Rescheduling interval: uses the `retry_delay` parameter, a floating-point value in seconds;

  - Rescheduling interval algorithm: for each inference task, the first fault waits `retry_delay` seconds before rescheduling, and each subsequent rescheduling interval is twice the previous rescheduling interval.

The following shows an example of the fault-scenario rescheduling function configuration:

```json
{
  "motor_coordinator_config": {
    "exception_config": {
      "reschedule_config": {
        "enable": false
      },
      "max_retry": 5,
      "transport_max_retry": null,
      "retry_delay": 0.2
    }
  }
}
```

## Memory Usage

When the fault-scenario rescheduling function is enabled, the `prompt_tokens` of **streaming requests** and the `tokens` of streaming responses are cached in the `Coordinator`, occupying the `Coordinator` memory during the inference task until the inference task ends and the memory is released.

The following uses a 10000-concurrency + 1M-context example to calculate the memory usage limit of the `Coordinator` when the fault-scenario rescheduling function is enabled.
(Based on industry experience, 1M tokens occupy approximately 3 to 6 MB of memory. Considering extreme cases, it is recommended to calculate using **factor 6**.)

In multi-concurrency scenarios:

- The formula for calculating the memory usage of the fault rescheduling function is as follows:

  - Memory usage limit of the fault rescheduling function ≈ concurrency × context length × factor 6

  - According to the above formula, in a 10000-concurrency + 1M-context application scenario, the memory usage limit of the fault-scenario rescheduling function is:

  - Memory usage limit of the fault rescheduling function ≈ 10000 × 1M × factor 6 ≈ 60G

- In addition, considering the basic capabilities, the request body cache requires memory. In extreme long-message scenarios, also calculated using **factor 6**, the formula for calculating the cached memory of the request body cache is as follows:

  - Memory usage limit of the request body cache ≈ concurrency × message length × factor 6

  - According to the preceding formula, in an application scenario with 10000 concurrent requests and 1M long messages, the memory usage limit of the request body is:

  - Memory usage limit of the request body cache ≈ 10000 × 1M × factor 6 ≈ 60G

Therefore, the memory usage limit of `Coordinator` is `> 60G + 60G = 120G`. Considering the basic memory overhead of `Coordinator` and the memory usage of other functions, it is recommended to set the actual memory limit of `Coordinator` to `128G`.

When the fault-scenario rescheduling function is enabled, it is recommended to calculate the memory usage limit according to the preceding formula based on the `maximum number of concurrent requests` and `context length`, and modify the memory configuration of `Coordinator`.

- For the maximum number of concurrent requests, refer to the `max_requests` configuration parameter in the [`user_config.json`](../../configuration/config_reference.md#motor_coordinator_config) configuration file;

- To set the memory usage limit of `Coordinator`, modify the `resources.limits.memory` configuration item in the `coordinator` container in the `yaml` file;

  - When deploying in CRD mode, refer to [`examples/deployer/yaml_template/infer_service_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/deployer/yaml_template/infer_service_template.yaml) for the `yaml` file;

  - When deploying in Multi mode, refer to [`examples/deployer/yaml_template/coordinator_template.yaml`](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/deployer/yaml_template/coordinator_template.yaml) for the `yaml` file.

The following is a reference example:

```yaml
containers:
  name: mindie-motor-coordinator
  ...
  resources:
    requests:
      memory: "4Gi"
      cpu: "16"
    limits:
      memory: "128Gi"
      cpu: "64"
```
