# 部署

MindIE Motor 支持以下两种部署方式，均为最佳实践，可根据自身环境选择：

## K8s 部署

适用于已有 Kubernetes 集群的场景，通过 deployer 工具一键生成并 apply 资源文件，支持 PD 分离、PD 聚合等多种部署形态，具备完整的服务发现、负载均衡与自愈能力。

→ 从 [部署模式说明](k8s/README.md) 开始

## Docker 部署

适用于单机或无 K8s 环境的场景，仅需 Docker 容器 + 宿主机挂载配置即可拉起推理服务，轻量快速。

→ 查看 [单容器部署](docker/single_container.md) 或 [多容器部署](docker/multi_container.md)
