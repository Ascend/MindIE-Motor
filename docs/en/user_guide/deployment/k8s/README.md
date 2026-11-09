# Business Topologies and Service Deployment Methods

MindIE Motor supports two business topologies and three service deployment methods. This document describes these concepts to prevent confusion.

**Table 1** Business topologies and service deployment methods

| Category | Option | Description |
|------|------|--------------|
| Business topology | <ul><li>PD disaggregation</li><li>PD aggregation</li></ul> | Determines whether Prefill/Decode are deployed separately, and affects inference performance to some extent. |
| Service deployment method | <ul><li>`infer_service_set`</li><li>`multi_deployment`</li><li>`single_container`</li></ul> | Only affects how service resources are created. The deployment method has no impact on service functionality. This document describes only the working principles, and generally you do not need to pay attention to this field. If it is not explicitly configured, the `infer_service_set` deployment method is used by default. |

## Business Topology

The business topology determines the service functionality and runtime form, and must be selected based on the actual scenario.

### PD Disaggregation

Prefill and Decode belong to different instances. This topology is suitable for scenarios that require independent planning of P/D resources and higher throughput.

- vLLM inference engine: For details, see [PD Disaggregation Service Deployment (Based on the vLLM Inference Engine)](./pd_disaggregation_deployment.md).
- SGLang inference engine: For details, see [PD Disaggregation Service Deployment (Based on the SGLang Inference Engine)](./pd_disaggregation_sglang.md).

### PD Aggregation

Prefill and Decode are hosted by the same type of instance (`union`). This topology is suitable for quick verification and small- and medium-scale deployment. For details, see [PD Aggregation Service Deployment](./pd_aggregation_deployment.md).

## Deployment Method

The service deployment method only affects how service resources are created. You can specify it by modifying the `user_config.json` file. If it is not configured, the default is `infer_service_set`. The following is a configuration example.

```json
{
  "motor_deploy_config": {
    "deploy_mode": "multi_deployment",  // Indicates that the service is deployed through the multi_deployment method.
    ...
  }
}
```

**Table 2** Configuration option parameters

| Value | Description |
|------|------|
| `infer_service_set` | Default method. Generates a single `infer_service.yaml` file, and the CRD controller uniformly starts pods such as controller, coordinator, prefill, decode (PD disaggregation), or union (PD aggregation). |
| `multi_deployment` | Generates multiple independent YAML files such as controller, coordinator, engine, and kv_pool, and creates pods separately. |
| `single_container` | Single-container method. Merges P/D into a single container for running, which is suitable for small-scale or test scenarios. |

For other configuration fields in the `user_config.json` file, see [Full Configuration Reference](../../configuration/config_reference.md).
