# Slurm Deployment

This document describes how to deploy MindIE Motor on a Slurm cluster. The complete process includes preparing the cluster software, configuring NPU resources, verifying the Slurm cluster, and starting the Motor service using `examples/deployer/slurm_deploy.py`.

## Deployment Process

```text
Environment preparation → Slurm/NPU configuration → Cluster verification → Motor configuration → Job submission → Inference verification
```

| Phase | Document | Main Content |
|------|------|----------|
| 1 | [Environment Preparation](./environment_preparation.md) | Install Slurm and Apptainer, and configure Munge, NPU GRES, and cgroup |
| 2 | [Service Deployment](./service_deployment.md) | Submit, view, and stop service jobs using `examples/deployer/slurm_deploy.py` |

## Node Roles

| Node | Slurm Service | Main Responsibility |
|------|------------|----------|
| Master node (controller) | `slurmctld` | Manages the cluster state and schedules jobs |
| Compute node (compute) | `slurmd` | Executes jobs and provides NPU resources |

When the master node also participates in computing, it must additionally run `slurmd` and be configured as a compute node in `slurm.conf`.

## Prerequisites

- The operating system is EulerOS, openEuler, or a RHEL-based distribution (aarch64).

- All nodes can access each other through host name or DNS resolution.

- All compute nodes can access the `.sif` image, model weights, and deployment directory under the same path.

- Root permission is available on the nodes, and configuration files and the Munge key can be distributed between the nodes.
