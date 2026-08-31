# Slurm 部署

本文档介绍如何在 Slurm 集群上部署 MindIE Motor。完整流程包括：准备集群软件、配置 NPU 资源、验证 Slurm 集群，以及使用 `examples/slurm_deployer` 启动 Motor 服务。

## 部署流程

```text
环境准备 → Slurm/NPU 配置 → 集群验证 → Motor 配置 → 提交作业 → 推理验证
```

| 阶段 | 文档 | 主要内容 |
|------|------|----------|
| 1 | [环境准备](./environment_preparation.md) | 安装 Slurm 和 Apptainer，配置 Munge、NPU GRES 与 cgroup |
| 2 | [服务部署](./service_deployment.md) | 配置 `examples/slurm_deployer`，提交、查看和停止服务作业 |

## 节点角色

| 节点 | Slurm 服务 | 主要职责 |
|------|------------|----------|
| 主节点（controller） | `slurmctld` | 管理集群状态并调度作业 |
| 计算节点（compute） | `slurmd` | 执行作业并提供 NPU 资源 |

主节点参与计算时，还需要同时运行 `slurmd`，并在 `slurm.conf` 中配置为计算节点。

## 使用前提

- 操作系统为 EulerOS、openEuler 或 RHEL 系（aarch64）。
- 所有节点之间可以通过主机名或 DNS 解析互相访问。
- 所有计算节点都能访问相同路径下的 `.sif` 镜像、模型权重和部署目录。
- 具备节点上的 root 权限，并能在节点之间分发配置文件和 Munge 密钥。
