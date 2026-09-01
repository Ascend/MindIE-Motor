# Manual Scaling

## Scope

This manual applies to the manual scaling process of MindIE Motor. Scaling is completed by modifying the instance number in `user_config.json` and executing the corresponding commands.

## Prerequisites

- At least one full deployment has been completed successfully (a ConfigMap `motor-config` exists in the cluster, containing the currently deployed `user_config` as the baseline).

- You have the `kubectl` permission.

## Configuration Description

During scaling, only the following fields are allowed to be modified:

- `motor_deploy_config.p_instances_num` (PD disaggregation)

- `motor_deploy_config.d_instances_num` (PD disaggregation)

- `motor_deploy_config.hybrid_instances_num` (PD co-location)

The instance numbers above must be **greater than 0 and not exceed 16**; otherwise, an error occurs during deployment or scaling.

## Procedure

### Initial Deployment

Run the full deployment in the `examples/deployer` directory:

```bash
cd examples/deployer
# PD disaggregation: (recommended) method 1, specifying the configuration directory
python3 deploy.py --config_dir ../infer_engines/vllm

# PD co-location: (recommended) method 1, specifying the configuration directory 
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid

# PD disaggregation: method 2, specifying the configuration files separately
python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json

# PD co-location: method 2, specifying the configuration files separately
python3 deploy.py --user_config_path ../infer_engines/vllm/pd_hybrid/user_config.json --env_config_path ../infer_engines/vllm/pd_hybrid/env.json
```

After completion:

- The ConfigMap `motor-config` is created or updated in the cluster (its content comes from the current `user_config.json` input), serving as the baseline for subsequent scaling and refresh operations.

- The YAML files for each service are generated under `output/deployment/`.

### Scaling

1. Modify the instance numbers in `user_config.json`:

   - PD disaggregation: `p_instances_num`, `d_instances_num`

   - PD co-location (CRD default): `hybrid_instances_num`

2. Run the scaling command in the `examples/deployer` directory:

```bash
cd examples/deployer
# PD disaggregation
python3 deploy.py --config_dir ../infer_engines/vllm --update_instance_num

# PD co-location
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid --update_instance_num
```

If the deployment was performed by specifying a separate configuration file, you must also specify `--user_config_path` and `--env_config_path` during scaling.

Note:

- The baseline comes from the cluster ConfigMap (`motor-config`). Compared with the current input, only instance number changes are allowed.

- Scale up: run `kubectl apply` only for the newly added instance indexes. Running instances are not re-pulled.

- Scale down: delete instances in descending order **starting from the instance with the largest index**, and synchronously delete the corresponding engine YAML files under `output/deployment/`.

- After success, the ConfigMap is updated to the current input `user_config.json`.

## FAQs

### Error: ConfigMap `motor-config` Not Found or Has No `user_config` in Cluster

This indicates that a full deployment has not been performed yet, or that there is no `motor-config` in the corresponding namespace. Perform a full deployment in the `examples/deployer` directory first, for example:

```bash
cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/vllm
```

### Error: `user_config` Changes Detected Beyond Instance Numbers

Configurations other than the instance number have been modified. Modify only `p_instances_num`/`d_instances_num`/`hybrid_instances_num`.

For the complete PD co-location deployment process and configuration instructions, see [PD Co-location Deployment](../deployment/k8s/pd_aggregation_deployment.md).

## Precautions

- Scaling affects only engine instances; controller/coordinator are not updated during the scaling process.

- To modify non-instance-number configurations such as images and mount paths, redeploy the service.

- Scaling down deletes instances starting from the highest index and removes the corresponding YAML files under output.

- The baseline of the deployed configuration is the `user_config` in the ConfigMap (`motor-config`) within the cluster.

- The Prefix Cache feature is enabled by default. This feature reuses the computed KV Cache to improve inference performance. Newly scaled-up instances have no KV Cache, so their inference performance may degrade slightly and recover after a period of time.
