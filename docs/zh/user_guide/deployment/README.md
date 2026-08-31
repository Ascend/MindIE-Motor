# 部署

MindIE Motor 支持以下部署方式，可根据自身环境选择：

## K8s 部署

适用于已有 Kubernetes 集群的场景，通过 deployer 工具一键生成并 apply 资源文件，支持 PD 分离、PD 聚合等多种部署形态，具备完整的服务发现、负载均衡与自愈能力。

→ 从 [部署模式说明](k8s/README.md) 开始

需要在集群内通过 Job 执行 deployer 时，可使用[云原生部署与 Helm Chart](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/cloud_native_deploy/README.zh.md)。社区 Chart 支持直接 `helm install/upgrade`。

## Docker 部署

适用于单机或无 K8s 环境的场景，仅需 Docker 容器 + 宿主机挂载配置即可拉起推理服务，轻量快速。

→ 查看 [单容器部署](docker/single_container.md) 或 [多容器部署](docker/multi_container.md)

## Slurm 部署

适用于 HPC / Slurm + Apptainer 集群，通过 `examples/slurm_deployer` 提交作业拉起服务。

→ 从 [Slurm 部署概述](../slurm/README.md) 开始，或直接查看 [环境准备](../slurm/environment_preparation.md) 与 [服务部署](../slurm/service_deployment.md)

## Coordinator 独立部署

适用于已有原生 vLLM Prefill/Decode、只需单独拉起调度面的场景，不部署 Controller 与 Node Manager。

→ 见 [Coordinator 独立部署](standalone.md)
