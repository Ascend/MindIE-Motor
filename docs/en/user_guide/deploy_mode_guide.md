# Deployment Mode Configuration (deploy_mode)

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:03:44.229Z pushedAt=2026-06-30T08:45:10.384Z -->

## Overview

`motor_deploy_config.deploy_mode` selects the deployment mode for MindIE PyMotor, determining how `deploy.py` generates and applies Kubernetes resources.

**Default behavior**: When the default `user_config.json` does not contain the `deploy_mode` field, the CRD mode (`infer_service_set`) is used for deployment. To use the traditional multi-YAML mode, explicitly configure `"deploy_mode": "multi_deployment"` in `motor_deploy_config`.

## Configuration Items

| Value | Description |
|------|------|
| `infer_service_set` | Default mode. Generate a single `infer_service.yaml` (including RBAC + InferServiceSet). The CRD Controller uniformly starts pods such as Controller, Coordinator, prefill, and decode. The InferServiceSet CRD must be pre-installed in the cluster. |
| `multi_deployment` | Traditional mode. Generate multiple independent YAML files for Controller, Coordinator, engine_*, kv_pool, etc., which are applied separately. No CRD dependency. |
| `single_container` | Single-container mode. Merge P/D into a single container for execution, suitable for small-scale or testing scenarios. |

Defaults to `infer_service_set` when not configured.

## Configuration Example

Located in `motor_deploy_config` of `user_config.json`. When using the CRD mode, `deploy_mode` can be left unconfigured or explicitly set to `"deploy_mode": "infer_service_set"`.

When using multi_deployment, it must be explicitly added:

```json
{
  "motor_deploy_config": {
    "deploy_mode": "multi_deployment",
    ...
  }
}
```

## Important Constraints

- **Initial deployment**: Read `deploy_mode` from `user_config.json` and deploy according to the selected mode.

- **Scaling (`--update_instance_num`)**: Use the baseline saved in the cluster ConfigMap as the reference. Modifying `deploy_mode` in user_config is **not allowed**; otherwise, an error is reported.

- **Refreshing ConfigMap (`--update_config`)**: Use the baseline as the reference. Modifying `deploy_mode` is **not allowed**; otherwise, an error is reported.

To switch the deployment mode, delete the current deployment first, then modify `deploy_mode` and re-execute the full deployment.
