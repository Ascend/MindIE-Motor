# 部署方式说明

MindIE Motor支持以下部署方式，可根据环境自行选择。

## K8s部署

该部署方式适用于已有Kubernetes集群的场景，通过deployer工具一键生成并apply资源文件，支持PD分离、PD混部等多种部署形态，具备完整的服务发现、负载均衡与自愈能力。

- PD混部服务部署：
    - 部署文档请参考[PD混部服务部署](./k8s/pd_aggregation_deployment.md)。
- PD分离服务部署：
    - 基于vLLM推理引擎部署文档请参考[PD分离服务部署（基于vLLM推理引擎）](./k8s/pd_disaggregation_deployment.md)。
    - 基于SGLang推理引擎部署文档请参考[PD分离服务部署（基于SGLang推理引擎）](./k8s/pd_disaggregation_sglang.md)。

>[!NOTE] 说明
>需要在集群内通过Job执行deployer时，可使用[云原生部署](../../../../examples/cloud_native_deploy/README.zh.md)。社区Chart支持直接`helm install/upgrade`。

## Docker部署

该部署方式仅依赖docker组件，可实现快速敏捷的服务部署。针对不同使用场景，提供如下部署方案：

| 方案 | 场景 |  服务器数量 |
| :--- | :--- | :--- |
| [单容器部署方案](docker/single_container.md) | 小模型（权重＜100GB）、且为单机；在一个容器内部署推理PD分离或者PD混部服务，适用于小模型验证。 | 1台 |
| [宿主机部署方案](docker/host/pd_disaggregation_deployment.md) | 普通场景，适用所有模型，服务器仅安装Docker，用户在宿主机执行一条指令创建容器并部署服务，但容器的生命周期需要用户手动管理。 |  不限 |
| [容器内部署方案](docker/container/pd_disaggregation_deployment.md)| 云平台场景，适用所有模型，服务器由云化平台管理（例如：ModelArts），容器的创建和回收由平台纳管，用户在容器内执行一条指令部署服务。 |  不限 |

## Slurm部署

该部署方式适用于HPC/Slurm+Apptainer集群，部署脚本所在路径为：examples/deployer/slurm_deploy.py。

- Slurm环境准备请参考[Slurm集群环境准备](slurm/environment_preparation.md) 。
- Slurm服务部署请参考[Slurm服务部署](slurm/service_deployment.md)。

## Coordinator独立部署

该部署方式适用于已有原生vLLM Prefill/Decode，且只需单独拉起调度面的场景，不需要部署Controller与Node Manager。

Coordinator独立部署文档请参考[Coordinator独立部署](standalone.md)。
