# MindIE-Motor

> [English](./OVERVIEW.md) | 中文

## 快速参考

- MindIE-Motor 由 [MindIE community](https://www.hiascend.com/cn/developer/software/mindie) 维护

- 从哪里获取帮助

    - [AscendHub 镜像仓库](https://www.hiascend.com/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4)
    - [MindIE-Motor 文档](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/docs/zh/index.md)
    - [昇腾开发者社区](https://www.hiascend.com/developer)
    - [问题反馈](https://gitcode.com/Ascend/MindIE-PyMotor/issues)

---

## MindIE-Motor

提供一键式 PD 分离部署，基于云原生插件化架构灵活适配多种推理引擎（vLLM、SGLang），结合高性能调度与负载均衡能力，构建高可用、可扩展的大规模推理服务。

---

## 支持的 Tags 及 Dockerfile 链接

官方发布镜像名称为 `mindie-motor`，仓库地址为 [AscendHub mindie-motor](https://www.hiascend.com/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4)。每个 Tag 均为多架构镜像（`arm64` / `x86_64`）。

### Tag 规范

当前发布镜像 Tag 遵循以下格式：

```text
<Motor版本>-<推理引擎版本>-<芯片系列>-<操作系统>-<python版本>
```

| 字段 | 示例值 | 说明 |
|---|---|---|
| `Motor版本` | `3.1.0`、`3.1.0b1` | MindIE-Motor 版本号 |
| `引擎版本` | `0.23.0`、`0.23.0rc1` | 配套 vllm-ascend 版本 |
| `芯片系列` | `a2`、`a3`、`a5` | 目标昇腾芯片系列 |
| `操作系统` | `ubuntu22.04`、`openeuler24.03` | 基础操作系统 |
| `python版本` | `py3.12` | Python 版本 |

3.0.x 历史 Tag 使用另一套命名，见 [Supported Tags](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/supported_tags.md)。

### 最新版本 MindIE-Motor 3.1.0

如下所示是 MindIE-Motor 在 AscendHub 最新发布的 3.1.0 版本的所有镜像（2026/08/18），历史版本所有的 Tag 请参考 [Supported Tags](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/supported_tags.md)

| Tag | Dockerfile | 架构 | 镜像内容 |
|---|---|---|---|
| `3.1.0-vllm_ascend0.23.0-a2-ubuntu22.04-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a2-ubuntu22.04-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a2-openeuler24.03-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a2-openeuler24.03-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a3-ubuntu22.04-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a3-ubuntu22.04-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a3-openeuler24.03-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a3-openeuler24.03-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a5-ubuntu22.04-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a5-ubuntu22.04-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |
| `3.1.0-vllm_ascend0.23.0-a5-openeuler24.03-py3.12` | [Dockerfile](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docker/mindie-motor-vllm/3.1.0-vllm_ascend0.23.0-a5-openeuler24.03-py3.12/Dockerfile) | arm64 / x86_64 | motor / vllm-ascend 0.23.0 |

---

## 快速开始

### 前置要求（可选）

- 宿主机上已经安装好固件与驱动，具体可参考[安装驱动和固件](https://www.hiascend.com/document/detail/zh/mindie/100/envdeployment/instg/mindie_instg_0006.html)。
- 宿主机上已经安装好 Docker 和 k8s。

### 使用 Motor

参考[快速入门](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docs/zh/user_guide/quick_start_motor.md)

### 如何本地构建

每个 Dockerfile 会在构建时自动 clone 指定分支与 commit 的源码，并在镜像内执行 `build.sh` 安装 `motor` / `ccae_reporter`，**无需本地源码或构建上下文**。将 `<tag>` 替换为目标组合后，在项目根目录执行：

```bash
TAG="3.1.0-vllm_ascend0.23.0-a2-ubuntu22.04-py3.12"

docker build --network=host \
    --platform=linux/arm64 \
    -t "mindie-motor:${TAG}" \
    -f "docker/mindie-motor-vllm/${TAG}/Dockerfile" \
    .
```

各 Dockerfile 头部注释中已写明对应的 `--platform`、源码仓库信息与完整 `docker build` 命令，可直接复制使用。

---

## 许可证

查看这些镜像中包含的 Motor 的[许可证信息](https://gitcode.com/Ascend/MindIE-Motor/blob/master/LICENSE.md)。

与所有容器镜像一样，预装软件包（Python、系统库等）可能受其自身许可证约束。
