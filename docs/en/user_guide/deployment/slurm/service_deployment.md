# Slurm Service Deployment

This document describes how to use `examples/deployer/slurm_deploy.py` to deploy
MindIE Motor through Slurm and Apptainer. Before deployment, complete [Environment Preparation](./environment_preparation.md) first.

## Deployment Entry

Run the following in the `examples/deployer` directory:

```bash
python3 slurm_deploy.py start \
  --config_dir /path/to/model/config \
  --partition <partition-name> \
  --coordinator-service <coordinator-address> \
  --controller-service <controller-address>
```

`--config_dir` points to the directory that contains `user_config.json` and `env.json`. You can also specify the two files separately:

```bash
python3 slurm_deploy.py start \
  --config /path/to/user_config.json \
  --env /path/to/env.json \
  --partition <partition-name> \
  --coordinator-service <coordinator-address> \
  --controller-service <controller-address>
```

## Parameter Configuration

The current Apptainer startup mode shares the host IPC and `/dev/shm` by default, so no additional IPC parameters are required.
`MOTOR_ENABLE_IPC_HOST` in `env.json` is used for Docker and Kubernetes deployment and does not affect the Slurm container startup parameters.

Command-line parameters take precedence over environment variables, and environment variables take precedence over the default values in `slurm_deploy.py`.

| Command-Line Parameter | Environment Variable | Default Value | Description |
|------------|----------|--------|------|
| `--partition` | `PARTITION` | `<partition>` | Slurm partition |
| `--device` | `DEVICE` | `npu` | GRES device name used by Engine jobs |
| `--coordinator-cpus` | `COORDINATOR_CPUS` | `64` | Number of CPUs requested by the Coordinator task |
| `--controller-cpus` | `CONTROLLER_CPUS` | `8` | Number of CPUs requested by the Controller task |
| `--kv-store-cpus` | `KV_STORE_CPUS` | `8` | Number of CPUs requested by the KV Store task |
| `--kv-conductor-cpus` | `KV_CONDUCTOR_CPUS` | `8` | Number of CPUs requested by the KV Conductor task |
| `--mf-store-cpus` | `MF_STORE_CPUS` | `8` | Number of CPUs requested by the MF Store task |
| `--encode-cpus` | `ENCODE_CPUS` | `16` | Number of CPUs requested by each Encode task |
| `--prefill-cpus` | `PREFILL_CPUS` | `16` | Number of CPUs requested by each Prefill task |
| `--decode-cpus` | `DECODE_CPUS` | `16` | Number of CPUs requested by each Decode task |
| `--union-cpus` | `UNION_CPUS` | `16` | Number of CPUs requested by each Union task |
| `--distribution-path` | `SLURM_DISTRIBUTION_PATH` | `/tmp` | Distribution root directory on compute nodes |
| `--log-path` | `SLURM_LOG_PATH` | `./slurm_workspace` | Log root directory on compute nodes |
| `--coordinator-service` | `COORDINATOR_SERVICE` | `<coordinator-ip>` | Coordinator address |
| `--controller-service` | `CONTROLLER_SERVICE` | `<controller-ip>` | Controller address |
| `--kvs-master-service` | `KVS_MASTER_SERVICE` | `<kvs-master-ip>` | KV Store address |
| `--kv-conductor-service` | `KV_CONDUCTOR_SERVICE` | `<kv-conductor-ip>` | KV Conductor address |
| `--mf-store-service` | `MF_STORE_SERVICE` | `<mf-store-ip>` | MF Store address |
| `--ascend-mf-store-port` | `ASCEND_MF_STORE_PORT` | `50089` | MF Store listening port |

For example, you can configure site information with environment variables:

```bash
export PARTITION=<partition-name>
export DEVICE=npu
export COORDINATOR_CPUS=64
export CONTROLLER_CPUS=8
export KV_STORE_CPUS=8
export KV_CONDUCTOR_CPUS=8
export MF_STORE_CPUS=8
export ENCODE_CPUS=16
export PREFILL_CPUS=16
export DECODE_CPUS=16
export UNION_CPUS=16
export SLURM_DISTRIBUTION_PATH=/tmp
export SLURM_LOG_PATH=./slurm_workspace
export COORDINATOR_SERVICE=<coordinator-address>
export CONTROLLER_SERVICE=<controller-address>

python3 slurm_deploy.py start --config_dir /path/to/model/config
```

Relative distribution and log paths are converted to absolute paths based on the current directory when the command is executed. Compute nodes must be able to
create these two paths. The ConfigMap uses the same absolute path on compute nodes and inside containers. The log path is configured independently; changing
`SLURM_DISTRIBUTION_PATH` does not change where logs are written.

A Slurm job always uses the local `/tmp` of the compute node as the initial working directory, and then `slurm_job.sh` creates the
distribution directory and the log directory on each node. Therefore, job startup does not depend on the current directory of the submission node existing on the compute nodes.

## Node Configuration

Coordinator, Controller, KV Store, KV Conductor, and MF Store need to run on the
fixed nodes that correspond to the service addresses. The deployment script first uses `getent hosts` to look up the node name, and then reads `scontrol show node -o`.

If the nodes cannot be found automatically, you can set the following environment variables:

- `COORDINATOR_NODE`
- `CONTROLLER_NODE`
- `KVS_MASTER_NODE`
- `KV_CONDUCTOR_NODE`
- `MF_STORE_NODE`

When `COORDINATOR_INFER_SERVICE` and `COORDINATOR_OBS_SERVICE` are not set, Motor automatically uses
`COORDINATOR_SERVICE`, and Slurm no longer passes these two variables again.

## Startup Process

After `start` is executed, the deployment script works in the following order:

1. Read and validate `user_config.json` and `env.json`.
2. Determine the Engine type and whether KV Store, KV Conductor, and MF Store are enabled.
3. Prepare the ConfigMap once on the submission node and generate a unified `service_id`.
4. Pack the ConfigMap and write it into the generated `slurm_job.sh`.
5. Submit the Coordinator, Controller, optional storage services, and Engine jobs respectively.
6. Each job creates the deployment directory on the node it is allocated to.
7. `sbcast` sends the generated script to all nodes used by the job.
8. Each node unpacks the ConfigMap and starts the corresponding role with Apptainer.

The container does not prepare the ConfigMap again, so all roles of the same startup use the same `service_id`.

## Files on the Submission Node

When `start` is executed, the following are created in the current directory:

```text
./slurm_workspace/
├── configmap/
├── slurm_deployment.json
└── slurm_job.sh
```

- `configmap/`: the configuration and scripts prepared for this startup.
- `slurm_deployment.json`: records the Job IDs of all services and the deployment parameters, used to stop services and for subsequent scaling.
- `slurm_job.sh`: the job script that contains the complete ConfigMap data.

Running a normal `start` again regenerates these contents. Running `stop` does not delete or rewrite them.

After the ConfigMap data is written into the job script, the script size must not exceed the `MaxScriptSize` of Slurm. The deployment script
checks this in advance against the default 4 MiB limit.

## Files on Compute Nodes

`SLURM_DEPLOYMENT_ID` consists of the `job_id` in `user_config.json`, the startup time, and a random suffix.
The file structure on each compute node is as follows:

```text
<distribution-path>/<deployment-id>/
├── mindie_motor_<slurm-job-id>.sh
└── mindie_motor_<slurm-job-id>_<task-id>/
    └── configmap/

<log-path>/<deployment-id>/
└── <role>_<slurm-job-id>_task<task-id>_<node-name>.log
```

The Job ID and Task ID are used to isolate the running directories of different jobs and different nodes. The ConfigMap is mounted read-only
to the same absolute path in the Apptainer container. Standard output and standard error are written to `.log` files in a separate log directory.

When a task exits, the directory of this task and the distribution script of the current job are deleted; after the last task on a node exits, the
deployment directory, which is then empty, is deleted as well. Log files are retained, and you must log in to the corresponding node to view them. To retain logs for a long time, place
the log path on reliable local storage.

## Engine Jobs

Slurm does not read `deploy_mode`; instead, it submits Engine jobs according to the following fields:

| Role | Number of Instances | Nodes per Instance | NPUs per Node |
|------|--------|--------------|----------------|
| Encode | `e_instances_num` | `single_e_instance_pod_num` | `e_pod_npu_num` |
| Prefill | `p_instances_num` | `single_p_instance_pod_num` | `p_pod_npu_num` |
| Decode | `d_instances_num` | `single_d_instance_pod_num` | `d_pod_npu_num` |
| Union | `hybrid_instances_num` | `single_hybrid_instance_pod_num` | `hybrid_pod_npu_num` |

If the number of nodes per instance or the number of NPUs per node is less than or equal to 0, that role is not submitted, and the reason for skipping is printed in the terminal.

## Engine Scaling

After changing the number of Engine instances in the original `user_config.json`, run the following in the same directory used when the deployment was started:

```bash
python3 slurm_deploy.py start \
  --config_dir /path/to/model/config \
  --update_instance_num
```

Scale-out submits Engine jobs starting from the current maximum instance index, while scale-in cancels jobs starting from the maximum instance index.
Coordinator, Controller, and storage services are not restarted,
and `SLURM_DEPLOYMENT_ID` does not change either.

During scaling, only `e_instances_num`, `p_instances_num`, `d_instances_num`, or
`hybrid_instances_num` may be modified. If other content of the configuration changes, or if the locally recorded instances are inconsistent with the prepared configuration,
the command reports an error directly and does not infer or rebuild the state automatically.

## KV Store, KV Conductor, and MF Store

Whether KV Store, KV Conductor, and MF Store are started is determined by `user_config.json`. Enabling KV Conductor
does not automatically enable KV Store; `KVS_MASTER_SERVICE` can point to the KV Store of this deployment or to an existing
service.

Memcache uses the following two configuration files:

```text
kv_store_backends.memcache.mmc-local-inprocess.conf
kv_store_backends.memcache.mmc-local-standalone.conf
```

The source files are located in `examples/deployer/startup/roles/kv_store_backends/memcache/`. At startup they are copied to the
ConfigMap and sent to compute nodes together with the job script. After the container starts, the following are generated:

```text
/usr/local/Ascend/pyMotor/conf/mmc-local-inprocess.conf
/usr/local/Ascend/pyMotor/conf/mmc-local-standalone.conf
```

## Viewing and Stopping

To view Slurm jobs:

```bash
squeue -p <partition-name>
scontrol show job <job-id>
```

To stop this deployment:

```bash
python3 slurm_deploy.py stop
```

`stop` runs `scancel --quiet` on the Job IDs in `slurm_deployment.json`. Running it repeatedly does not fail because the jobs have already finished.
`stop` does not modify the workspace on the submission node. When tasks on compute nodes exit, they clean up their own task directories
and the distribution script; the last task deletes the empty deployment directory and keeps the `.log` files.
