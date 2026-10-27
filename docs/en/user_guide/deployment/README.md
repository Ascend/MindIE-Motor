# Deployment

MindIE Motor supports the following deployment methods, which can be selected based on your environment.

## K8s Deployment

It is suitable for scenarios where a Kubernetes cluster already exists. Resource files are generated and applied with one click through the deployer tool, supporting multiple deployment forms such as PD disaggregation and PD aggregation, and providing complete service discovery, load balancing, and self-healing capabilities.

→ Start from [Deployment Mode Description](k8s/README.md); for the SGLang engine, see [SGLang PD Disaggregation Deployment](k8s/pd_disaggregation_sglang.md)

If the deployer needs to be executed in the cluster through a Job, use [Cloud-Native Deployment and Helm Chart](../../../../examples/cloud_native_deploy/README.md). The community Chart supports direct `helm install/upgrade`.

## Docker Deployment

It is suitable for a single machine or an environment without K8s. The entry point is `examples/deployer/docker_deploy.py`.

→ [Docker Single-Container Service Deployment Guide](docker/single_container.md)

→ [Docker Multi-Container PD Disaggregation Deployment](docker/multi_container.md)

## Slurm Deployment

It is suitable for HPC / Slurm + Apptainer clusters. Submit jobs through `examples/deployer/slurm_deploy.py` to start the service.

→ Start from [Slurm Deployment Overview](slurm/README.md), or directly see [Environment Preparation](slurm/environment_preparation.md) and [Service Deployment](slurm/service_deployment.md)

## Standalone Coordinator Deployment

It is suitable for scenarios where native vLLM Prefill/Decode already exists and only the scheduling plane needs to be started separately, without deploying the Controller and Node Manager.

→ See [Standalone Coordinator Deployment](standalone.md)
