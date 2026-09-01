# Deployment Mode Configuration Description

MindIE Motor supports two business topology structures (**affecting functionality**) and three service deployment modes (**not affecting functionality, requiring no special attention**). This document describes the related content to avoid user confusion.

Brief Description

| Category | Option | Description |
|------|------|--------------|
| Business topology | PD disaggregation, PD colocation | Determines whether Prefill/Decode are deployed separately, **affecting inference performance**. |
| Service deployment mode | `infer_service_set`, `multi_deployment`, `single_container` | Only affects how service resources are created. The deployment mode **has no impact on service functionality. This document only introduces the working principle, and generally you do not need to pay attention to the fields**. When not explicitly configured, `infer_service_set` is used. |

## Business Topology

The business topology determines the service functions and runtime form, and must be selected according to the scenario.

### PD Disaggregation

Prefill and Decode belong to different instances, which is suitable for scenarios that require independent planning of P/D resources and pursue higher throughput. For detailed steps, see [PD Disaggregation Deployment](./pd_disaggregation_deployment.md).

### PD Co-location

Prefill and Decode are hosted by the same type of instance (union), which is suitable for quick verification and small- to medium-scale deployment. For detailed steps, see [PD Co-location Deployment](./pd_aggregation_deployment.md).

## Deployment Mode

The service deployment mode only **affects how service resources are created**, and can be specified by modifying the `user_config.json` file, as shown below. If not configured, the default is `infer_service_set`.

```json
{
  "motor_deploy_config": {
    "deploy_mode": "multi_deployment",  // Indicates that the service is deployed through the multi_deployment mode.
    ...
  }
}
```

The configuration options are described as follows:

| Value | Description |
|------|------|
| `infer_service_set` | Default mode. Generates a single `infer_service.yaml`, and the CRD controller uniformly starts pods such as controller, coordinator, prefill, decode (PD disaggregation), or union (PD co-location). |
| `multi_deployment` | Generates multiple independent YAML files such as controller, coordinator, engine, and kv_pool, and creates pods separately. |
| `single_container` | Single-container mode. Merges P/D into a single container for running, suitable for small-scale or test scenarios. |

For other configuration fields in the user_config.json file, see [Full Configuration Reference](../../configuration/config_reference.md).
