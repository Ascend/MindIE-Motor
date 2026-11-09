# Deployment Methods

MindIE Motor supports the following deployment methods, which can be selected based on your environment.

## K8s Deployment

This deployment method is suitable for scenarios where a Kubernetes cluster already exists. Resource files are generated and applied with one click through the deployer tool, supporting multiple deployment forms such as PD disaggregation and PD aggregation, and providing complete service discovery, load balancing, and self-healing capabilities.

- PD aggregation service deployment:
    - For the deployment document, see [PD Aggregation Service Deployment](./k8s/pd_aggregation_deployment.md).
- PD disaggregation service deployment:
    - For the deployment document based on the vLLM inference engine, see [PD Disaggregation Service Deployment (Based on the vLLM Inference Engine)](./k8s/pd_disaggregation_deployment.md).
    - For the deployment document based on the SGLang inference engine, see [PD Disaggregation Service Deployment (Based on the SGLang Inference Engine)](./k8s/pd_disaggregation_sglang.md).

>[!NOTE] NOTE
>When the deployer needs to be executed in the cluster through a Job, you can use [Cloud-Native Deployment](../../../../examples/cloud_native_deploy/README.md). The community Chart supports direct `helm install/upgrade`.

## Docker Deployment

This deployment method depends only on the Docker component and enables fast and agile service deployment. The following deployment solutions are provided for different use cases:

| Solution | Scenario | Number of Servers |
| :--- | :--- | :--- |
| [Single-container deployment solution](docker/single_container.md) | Small models (weights less than 100 GB) on a single machine; the PD disaggregated or PD aggregated inference service is deployed within one container, which is suitable for small model verification. | 1 server |
| [Host deployment solution](docker/host/pd_disaggregation_deployment.md) | General scenarios, applicable to all models. Only Docker is installed on the server. The user runs a single command on the host to create a container and deploy the service, but the container lifecycle must be managed manually. | Unlimited |
| [In-container deployment solution](docker/container/pd_disaggregation_deployment.md)| Cloud platform scenarios, applicable to all models. Servers are managed by a cloud platform (for example, ModelArts), and container creation and reclamation are managed by the platform. The user runs a single command inside the container to deploy the service. | Unlimited |

## Slurm Deployment

This deployment method is suitable for HPC/Slurm + Apptainer clusters. The deployment script is located at: `examples/deployer/slurm_deploy.py`.

- For Slurm environment preparation, see [Slurm Cluster Environment Preparation](slurm/environment_preparation.md).
- For Slurm service deployment, see [Slurm Service Deployment](slurm/service_deployment.md).

## Standalone Coordinator Deployment

This deployment method is suitable for scenarios where native vLLM Prefill/Decode already exists and only the scheduling plane needs to be started separately, without deploying the Controller and Node Manager.

For details, see the [Standalone Coordinator Deployment](standalone.md) document.
