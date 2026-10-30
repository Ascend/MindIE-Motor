# 部署

MindIE Motor 支持以下部署方式，可根据自身环境选择：

## K8s 部署

适用于已有 Kubernetes 集群的场景，通过 deployer 工具一键生成并 apply 资源文件，支持 PD 分离、PD 聚合等多种部署形态，具备完整的服务发现、负载均衡与自愈能力。

→ 从 [部署模式说明](k8s/README.md) 开始；SGLang 引擎见 [SGLang PD 分离部署](k8s/pd_disaggregation_sglang.md)

需要在集群内通过 Job 执行 deployer 时，可使用[云原生部署与 Helm Chart](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/cloud_native_deploy/README.zh.md)。社区 Chart 支持直接 `helm install/upgrade`。

## Docker 部署

仅依赖 Docker，入口为 `examples/deployer/docker_deploy.py`。针对不同使用场景：

| 方案 | 设计场景 | 服务器数量 |
| :--- | :--- | :--- |
| [单容器部署方案](docker/single_container.md) | 小模型（权重＜100GB）、单机：在一个容器内部署 PD 分离或 PD 混部，适用于小模型验证。 | 1台 |
| [宿主机部署方案](docker/host/pd_disaggregation_deployment.md) | 普通场景：适用所有模型，服务器只安装 Docker。用户在宿主机执行一条指令创建容器并部署服务，容器生命周期需要用户手动管理。 | 不限 |
| [容器内部署方案](docker/container/pd_disaggregation_deployment.md) | 云平台场景：适用所有模型，容器由平台创建和回收（例如 ModelArts）。用户在容器内执行一条指令部署服务。 | 不限 |

## Slurm 部署

适用于 HPC / Slurm + Apptainer 集群，通过 `examples/slurm_deployer` 提交作业拉起服务。

→ 从 [Slurm 部署概述](../slurm/README.md) 开始，或直接查看 [环境准备](../slurm/environment_preparation.md) 与 [服务部署](../slurm/service_deployment.md)

## Coordinator 独立部署

适用于已有原生 vLLM Prefill/Decode、只需单独拉起调度面的场景，不部署 Controller 与 Node Manager。

→ 见 [Coordinator 独立部署](standalone.md)
