# MindIE-Motor

> [English](./OVERVIEW.md) | 中文

## 快速参考

- MindIE-Motor 由 [MindIE community](https://www.hiascend.com/cn/developer/software/mindie) 维护

- 从哪里获取帮助

    - [MindIE 镜像仓库](https://www.hiascend.com/developer/ascendhub/detail/af85b724a7e5469ebd7ea13c3439d48f)
    - [MindIE-Motor 文档](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/docs/zh/index.md)
    - [昇腾开发者社区](https://www.hiascend.com/developer)
    - [问题反馈](https://gitcode.com/Ascend/MindIE-PyMotor/issues)

---

## MindIE-Motor

提供一键式 PD 分离部署，基于云原生插件化架构灵活适配多种推理引擎（vLLM、SGLang），结合高性能调度与负载均衡能力，构建高可用、可扩展的大规模推理服务。

---

## 支持的 Tags 及 Dockerfile 链接

### Tag 规范

官方预构建镜像 Tag 遵循以下格式：

```text
<MindIE版本>-<产品系列>-<python版本>-<操作系统>-lts-<架构>
```

| 字段 | 示例值 | 说明 |
|---|---|---|
| `MindIE版本` | `3.0.0`、 `3.0.0b1`、 `3.0.0b2`| MindIE-Motor 版本号 |
| `产品系列` | `800I-A2`、`800I-A3` | 目标昇腾产品系列 |
| `python版本` | `py3.11` | Python 版本 |
| `操作系统` | `Ubuntu24.04-lts`、`openEuler24.03-lts` | 基础操作系统及发行版标识 |
| `架构` | `aarch64`、`x86_64` | CPU 架构 |

### 3.0.0 版本 Dockerfile 目录

每个版本/硬件/系统/架构组合均有独立 Dockerfile

```text
docker/mindie-motor-vllm/<tag>/Dockerfile
```

| Tag | Dockerfile |
|---|---|
| `3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64/Dockerfile) |
| `3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-x86_64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-x86_64/Dockerfile) |
| `3.0.0-800I-A2-py3.11-openEuler24.03-lts-aarch64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A2-py3.11-openEuler24.03-lts-aarch64/Dockerfile) |
| `3.0.0-800I-A2-py3.11-openEuler24.03-lts-x86_64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A2-py3.11-openEuler24.03-lts-x86_64/Dockerfile) |
| `3.0.0-800I-A3-py3.11-Ubuntu24.04-lts-aarch64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A3-py3.11-Ubuntu24.04-lts-aarch64/Dockerfile) |
| `3.0.0-800I-A3-py3.11-Ubuntu24.04-lts-x86_64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A3-py3.11-Ubuntu24.04-lts-x86_64/Dockerfile) |
| `3.0.0-800I-A3-py3.11-openEuler24.03-lts-aarch64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A3-py3.11-openEuler24.03-lts-aarch64/Dockerfile) |
| `3.0.0-800I-A3-py3.11-openEuler24.03-lts-x86_64` | [Dockerfile](./mindie-motor-vllm/3.0.0-800I-A3-py3.11-openEuler24.03-lts-x86_64/Dockerfile) |

每个 Dockerfile 均已内置对应的基础镜像（vllm-ascend v0.18.0 系列）、目标平台、镜像 Tag、入口脚本与使用协议，无需额外脚本、外部 docker 文件或环境变量选择。

---

## 快速开始

### 前置要求（可选）

#### 安装驱动

- 宿主机上已经安装好固件与驱动，具体可参考[安装驱动和固件](https://www.hiascend.com/document/detail/zh/mindie/100/envdeployment/instg/mindie_instg_0006.html)。
- 宿主机上已经安装好 Docker。

---

### 构建 MindIE-Motor 镜像

每个 Dockerfile 会在构建时自动 clone 指定分支与 commit 的源码，**无需本地源码或构建上下文**。将 `<tag>` 替换为目标组合后执行：

```bash
TAG="3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64"

docker build --network=host \
    --platform=linux/arm64 \
    -t "mindie-motor-vllm:${TAG}" \
    -f "docker/mindie-motor-vllm/${TAG}/Dockerfile" \
    .
```

各 Dockerfile 头部注释中已写明对应的 `--platform`、源码仓库信息与完整 `docker build` 命令，可直接复制使用。

3.0.0 版本源码映射：

| 镜像版本 | 仓库 | 分支 | Commit |
|---|---|---|---|
| `3.0.0` | `https://gitcode.com/Ascend/MindIE-PyMotor.git` | `v3.0.0` | `383d1787ed3fc27aaad2db9cc5506d40c258c279` |

构建过程依次完成：

1. 拉取对应 vllm-ascend 基础镜像。
2. Clone 指定分支与 commit 的 MindIE-PyMotor 源码到 `/opt/MindIE-PyMotor`。
3. 安装依赖、编译并安装 `motor` wheel 包。
4. 编译并安装 `ccae_reporter` 可观测组件。
5. 在 Dockerfile 内联生成容器入口脚本与使用协议。

### 运行 MindIE-Motor 容器

运行前请确认宿主机已安装昇腾驱动，且 `/dev/davinci*` 等设备节点可用。

#### 最小验证命令

```bash
IMAGE_NAME="mindie-motor-vllm:3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64"

docker run --rm -it \
  --device=/dev/davinci_manager \
  --device=/dev/devmm_svm \
  --device=/dev/hisi_hdc \
  --device=/dev/davinci0 \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro \
  -v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/:ro \
  -v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi:ro \
  -v /var/log/npu/:/usr/slog \
  "${IMAGE_NAME}" \
  bash -c "npu-smi info && python -c 'import motor; print(\"motor ok\")'"
```

#### 启动推理服务

实际部署需提前准备 `boot.sh`、`user_config.json` 等配置文件，并挂载到容器内。完整端到端流程见 [docker-only 单容器部署指南](../docs/zh/user_guide/deployment/docker/single_container.md)。

```bash
CONFIGMAP_PATH="/path/to/configmap"
IMAGE_NAME="mindie-motor-vllm:3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64"

docker run -u root --rm --name mindie-motor \
  -e ASCEND_RUNTIME_OPTIONS=NODRV \
  -e CONFIGMAP_PATH="${CONFIGMAP_PATH}" \
  -e CONFIG_PATH=/usr/local/Ascend/pyMotor/conf \
  -e ROLE=SINGLE_CONTAINER \
  --device=/dev/davinci_manager \
  --device=/dev/devmm_svm \
  --device=/dev/hisi_hdc \
  --device=/dev/davinci0 \
  --device=/dev/davinci1 \
  -p 1025:1025 \
  -p 1026:1026 \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
  -v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
  -v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
  -v /usr/local/sbin:/usr/local/sbin \
  -v /var/log/npu/:/usr/slog \
  -v /mnt:/mnt \
  -v "${CONFIGMAP_PATH}:${CONFIGMAP_PATH}" \
  "${IMAGE_NAME}" \
  bash -c 'export POD_IP=$(grep $(hostname) /etc/hosts | cut -f1) && source ${CONFIGMAP_PATH}/boot.sh'
```

常用参数说明：

| 参数 / 环境变量 | 说明 |
|---|---|
| `--device=/dev/davinci{N}` | 映射 NPU 设备，按实际卡数追加 `davinci0`、`davinci1` 等 |
| `--device=/dev/davinci_manager` 等 | 昇腾管理设备，运行推理时通常必填 |
| `-v /usr/local/Ascend/driver:...` | 挂载宿主机昇腾驱动目录 |
| `-v ${CONFIGMAP_PATH}:...` | 挂载启动脚本与配置文件目录 |
| `-p <host>:<container>` | 暴露 API 端口，需与 `user_config.json` 中端口配置一致 |
| `ASCEND_RUNTIME_OPTIONS=NODRV` | 复用宿主机驱动，无需在容器内重复安装 |
| `CONFIGMAP_PATH` | 容器内启动脚本路径，需与挂载目录保持一致 |
| `CONFIG_PATH` | Motor 配置文件目录，默认 `/usr/local/Ascend/pyMotor/conf` |
| `ROLE` | 部署角色；单容器 PD 分离场景取 `SINGLE_CONTAINER` |

### 如何二次开发

```bash
FROM mindie-motor-vllm:3.0.0-800I-A2-py3.11-Ubuntu24.04-lts-aarch64

RUN apt update -y && \
    apt install gcc ...
```

---

## 支持的硬件

| 芯片系列 | 产品示例 | 架构 |
|---|---|---|
| 昇腾 910B | Atlas 800T A2、Atlas 900 A2 PoD | ARM64 / x86_64 |
| 昇腾 A3 | Atlas 800T A3 | ARM64 / x86_64 |

---

## 镜像版本说明

| 镜像版本 | 说明 | 备注 |
| - | - | - |
| 3.0.0 | MindIE 3.0.0 Release 版本 | 2026/5/6：首次发布 |

## 许可证

查看这些镜像中包含的 Motor 的[许可证信息](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/LICENSE.md)。

与所有容器镜像一样，预装软件包（Python、系统库等）可能受其自身许可证约束。
