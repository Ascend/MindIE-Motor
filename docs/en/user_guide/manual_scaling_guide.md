# Manual Scaling User Guide (MindIE PyMotor)

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:05:21.181Z pushedAt=2026-07-01T09:07:26.655Z -->

## Scope

This guide applies to the manual scaling process of MindIE PyMotor. Scaling is achieved by modifying the number of instances in `user_config.json` and executing the corresponding commands.

## Prerequisites

- At least one full deployment has been successfully completed (the ConfigMap `motor-config` exists in the cluster, containing the currently deployed `user_config` as a baseline).

- Ensure you have `kubectl` permissions.

## Configuration Description

Only the following fields can be modified during scaling:

- `motor_deploy_config.p_instances_num`

- `motor_deploy_config.d_instances_num`

The above instance count must be **greater than 0 and not exceed 16**; otherwise, an error will be reported during deployment or scaling.

## Procedure

### Initial Deployment

Execute full deployment in the `examples/deployer` directory:

```bash
cd examples/deployer
# Method 1: specify the configuration directory (recommended)
python3 deploy.py --config_dir ../infer_engines/vllm

# Method 2: Specify configuration files individually
python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

After completion:

- The ConfigMap `motor-config` will be created/updated in the cluster (with content from the current input `user_config.json`), serving as the baseline for subsequent scaling and refresh.

- Service YAML files will be generated under `output/deployment/`.

### Scaling

1. Modify the number of instances in `user_config.json`:

   - `p_instances_num`

   - `d_instances_num`

2. Run the scaling commands in the `examples/deployer` directory:

```bash
cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/vllm --update_instance_num
```

To use the method of specifying configuration files individually, you must also specify `--user_config_path` and `--env_config_path` during scaling.

NOTE

- The baseline comes from the cluster ConfigMap (`motor-config`). Compared with the current input, only changes to the number of instances are allowed.

- Scale-up: `kubectl apply` is executed only for the newly added instance indexes. Running instances will not be re-pulled.

- Scale-down: Instances are deleted sequentially **starting from the instance with the highest index**, and the corresponding engine YAML files under `output/deployment/` are deleted synchronously.

- Upon success, the ConfigMap is updated to the current input `user_config.json`.

## FAQs

### Error: ConfigMap motor-config not found or has no user_config in cluster

This indicates that a full deployment has not been performed yet, or `motor-config` does not exist in the corresponding namespace. Therefore, perform a full deployment in the `examples/deployer` directory, for example:

```bash
cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/vllm
```

### Error: user_config changes detected beyond instance numbers

This indicates that configurations other than the instance count have been modified. Modify `p_instances_num`/`d_instances_num` only.

## Notes

- Scaling only affects engine instances; Controller/Coordinator are not updated during the scaling process.

- To modify non-instance-count configurations such as images or mount paths, perform a redeployment.

- Scale-in deletes instances starting from the highest index and removes the corresponding YAML files under the `output` directory.

- The baseline for deployed configurations is the `user_config` in the cluster's ConfigMap (`motor-config`).

- The Prefix Cache feature is enabled by default. This feature reuses pre-computed KV caches to improve inference performance. Newly scaled-out instances do not have KV cache data, so their inference performance may degrade slightly and recover after a period of time.
