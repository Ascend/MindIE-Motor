# 基于麒麟系统的 MindIE Motor 镜像制作指导

> 本文介绍在麒麟 V11（Kylin Linux Advanced Server V11，代号 Swan25）aarch64 服务器上，基于 vLLM 0.30.0 与 vllm-ascend 0.30.0 制作包含 MindIE Motor 的推理镜像的完整流程，包括版本配套、制作步骤、镜像验证、镜像分发和常见问题处理。本文配套的参考 Dockerfile 见[附录 A](#附录-a参考-dockerfile)。

## 1 概述

### 1.1 适用范围

本文适用于以下场景：

- 服务器操作系统为麒麟 V11 aarch64，不在官方发布镜像的操作系统覆盖范围内，需要自行制作 MindIE Motor 镜像。
- 硬件为 Atlas 800I A3 超节点服务器（`SOC_VERSION=ascend910_9391`）。
- 需要在镜像中同时集成 vLLM、vllm-ascend 与 MindIE Motor，用于 PD 分离或 PD 混部部署。

构建机要求：aarch64 架构，可访问本文[3.3 网络与软件源](#33-网络与软件源)中列出的网络地址。构建机不需要安装 NPU 驱动（Dockerfile 通过 `--force` 跳过 CANN 的环境校验），但运行镜像的服务器需要安装配套的 NPU 驱动，并在运行容器时挂载驱动目录。

>[!NOTE]说明
> 本文以 vLLM 0.30.0 + vllm-ascend 0.30.0 + CANN 9.2.1 为例。vllm-ascend 的正式版本配套关系以[版本说明](../../release_note_motor.md)和 vllm-ascend 发布分支的 `requirements.txt` 为准，升级引擎版本时需同步调整本文中的版本参数。

### 1.2 镜像版本配套

| 项目 | 版本或说明 |
| --- | --- |
| 基础镜像 | `cr.kylinos.cn/kylin/kylin-server-platform:v11-2503` |
| 容器内操作系统 | Kylin Linux Advanced Server V11（Swan25），aarch64 |
| CANN | 9.2.1（toolkit、A3-ops、nnal） |
| Python | 3.12.16（源码编译） |
| vLLM | 0.30.0 |
| vllm-ascend | `releases/v0.30.0` |
| torch（vLLM 构建依赖） | 2.13.0+cpu |
| torch（最终运行时） | 2.10.0+cpu |
| torch_npu | 2.10.0.post7 |
| triton-ascend | 3.6.0.post2 |
| MindIE Motor | master 分支源码（制作时镜像内为 3.1.0） |
| 产出镜像 | `mindie-motor:0.30.0-cann9.2.1-a3-kylin-v11-motor`，约 25 GB |

### 1.3 与官方发布镜像的差异

- 官方发布镜像覆盖 Ubuntu、openEuler 等操作系统，麒麟系统镜像需按本文自行制作。
- 本镜像未包含 `moxing_framework`，如需 ModelArts 的 moxing 能力，需额外准备 wheel 并安装。
- 本镜像未包含 CCAE Reporter，如需可观测能力，需在 MindIE Motor 构建后追加 `examples/features/observability` 的构建与安装。
- 参考 Dockerfile 在镜像精简阶段卸载 Ray，如需多机 Ray 并行能力，需保留或重新安装 Ray。
- 本文仅完成镜像内导入与进程拉起验证，完整推理链路的验证方法见[5.5 完整链路验证建议](#55-完整链路验证建议)。

## 2 制作思路

### 2.1 整体流程

镜像制作分为以下阶段，所有阶段在同一个 Dockerfile 中完成：

1. 以麒麟基础镜像为底座，安装构建工具并源码编译 Python 3.12。
2. 从华为 OBS 下载并安装 CANN 9.2.1 的 toolkit、A3-ops 和 nnal 子包。
3. 创建镜像内用户，配置 pip、git 软件源镜像。
4. 安装 vLLM 0.30.0（`VLLM_TARGET_DEVICE=empty`，不依赖 NPU 设备）。
5. 安装并源码编译 vllm-ascend `releases/v0.30.0`。
6. 安装 Mooncake、mfcli kernel、flash-linear-attention 等固定依赖。
7. 编译 vLLM Rust frontend。
8. 精简镜像，整理运行时依赖。
9. 拉取 MindIE Motor 源码，构建 `motor` wheel（含 kv-conductor）并安装。

### 2.2 版本关系说明

vLLM 0.30.0 的 `pyproject.toml` 将 `torch==2.13.0` 声明为 PEP517 构建依赖，而 vllm-ascend `releases/v0.30.0` 的 `requirements.txt` 要求运行时版本：

```text
torch==2.10.0
torch-npu==2.10.0.post7
torchvision==0.25.0
torchaudio==2.10.0
triton-ascend==3.6.0.post2
```

因此构建过程分为两段：

1. 构建 vLLM 时使用 torch 2.13.0 作为构建依赖。
2. 安装 vllm-ascend 依赖时替换为 torch 2.10.0、torch_npu 2.10.0.post7 等运行时版本。

最终镜像内 torch 为 2.10.0+cpu，vLLM 可正常导入。若后续出现 vLLM 运行时的 ABI 问题，需将 vLLM 固定到与 vllm-ascend 0.30.0 完全匹配的 commit，而不是 GitHub 的 `v0.30.0` 标签。

### 2.3 麒麟系统适配点

麒麟 V11 与 Ubuntu、openEuler 的差异需要在 Dockerfile 中显式处理，主要适配点如下：

| 适配点 | 处理方式 |
| --- | --- |
| 缺少 `hostname`、`flock` 等基础命令 | 安装 `util-linux`、`hostname`；CANN `.run` 安装包会调用 `flock`、`wget`、`gcc` |
| 系统 Python 为 3.11 | 源码编译 Python 3.12.16 并安装到 `/usr/local`，CANN `.run` 的 pyACL 会装入当前 `python3` |
| bisheng、cce-ld 找不到 crt 文件 | 将 GCC 12 的 `crt*.o`、`libgcc*` 链接到 `/usr/lib64`，并设置 `LIBRARY_PATH` |
| HCC 的 g++ 为 GCC 7.3 | HCC 编译器仅用于 AICPU 编译；为其增加包装脚本，执行前 `unset CPLUS_INCLUDE_PATH`，使 bisheng 使用 GCC 12 头文件 |
| 系统无 `clang-15` | 安装系统 `clang`；triton-ascend 使用 cp312 wheel，不在镜像内源码编译 |
| 无 `delgroup` 命令 | 使用 `groupdel` 替代，依次执行 `userdel`、`groupdel`、`groupadd` |
| 构建机无 NPU 驱动 | 为 vllm-ascend 的 `build_aclnn.sh` 中的 `.run` 增加 `--force`，跳过环境校验 |
| kv-conductor 依赖 libzmq 与 pkg-config | 安装 `zeromq-devel`、`pkgconfig`，否则 Rust `zmq` crate 无法编译 |
| 代理环境下的 TLS 中间人证书 | 注入代理 CA 并设置 CA bundle 环境变量，见[3.4 代理与证书](#34-代理与证书可选) |

### 2.4 本地 wheelhouse

vLLM 构建依赖的 torch 2.13.0、运行时 torch 2.10.0、triton 3.6.0、numpy 1.26.4 以及 flash-linear-attention 的 wheel 体积较大（部分 wheel 超过 140 MB，其中 flash-linear-attention 的 wheel 约 60 MB）。当构建网络经代理访问 PyTorch CDN 或 GitHub Release 不稳定时，建议提前在可联网环境下载 wheel，再通过 `COPY` 指令复制到镜像中安装，避免构建过程反复中断。附录 A 的 Dockerfile 默认从构建上下文 `wheels/` 安装，下载命令见[4.3 准备本地 wheel](#43-准备本地-wheel)。

## 3 前置条件

### 3.1 硬件与操作系统

- 架构：aarch64。
- 构建机操作系统：Linux，本文在 openEuler 与麒麟宿主机上验证；容器内操作系统为麒麟 V11。
- 运行节点：Atlas 800I A3 超节点服务器，已安装与 CANN 9.2.1 配套的 NPU 驱动。

### 3.2 软件与资源

- Docker Engine 24 及以上版本，支持 BuildKit 和 `--platform linux/arm64`。
- 磁盘：镜像约 25 GB，加上构建缓存与本地 wheel，建议预留 50 GB 以上可用空间。
- 构建上下文：参考 Dockerfile（见附录 A）、`wheels/`（[4.3](#43-准备本地-wheel) 的 7 个 wheel，附录 Dockerfile 默认从本地安装）、可选代理 CA `proxy-ca.pem`。MindIE Motor 源码由 Dockerfile 在构建阶段从 GitCode 自动克隆，无需提前下载（无法访问 GitCode 时见 3.5 节）。
- 内存建议 32 GB 以上，编译 vllm-ascend 自定义算子与 Rust frontend 时对内存要求较高。

### 3.3 网络与软件源

构建过程需要访问以下网络地址：

| 用途 | 地址 |
| --- | --- |
| 麒麟基础镜像 | `cr.kylinos.cn` |
| CANN 安装包 | `ascend-repo.obs.cn-east-2.myhuaweicloud.com` |
| Python 源码 | `repo.huaweicloud.com` |
| PyPI 源 | `pypi.tuna.tsinghua.edu.cn` |
| vLLM、vllm-ascend 源码 | `github.com`，或配置 `GH_PROXY` 参数使用镜像代理 |
| MindIE Motor 源码 | `gitcode.com` |
| PyTorch CPU wheel | `download.pytorch.org` |
| 昇腾 PyPI 源 | `mirrors.huaweicloud.com/ascend/repos/pypi` |
| Mooncake wheel | `mindie.obs.cn-north-4.myhuaweicloud.com` |
| flash-linear-attention wheel | `github.com/flashserve/flash-linear-attention-npu/releases` |
| Rust 工具链 | `mirrors.ustc.edu.cn/rust-static` |

>[!NOTE]说明
> 上述地址可通过 Dockerfile 中的 `ARG` 参数覆盖。内网环境请将对应地址替换为内部镜像源，并保证构建机可解析和访问。

### 3.4 代理与证书（可选）

构建网络通过 TLS 代理访问外网时，代理会以中间人方式重签证书，而麒麟基础镜像内不含该 CA，`yum`、`pip` 会报 `self-signed certificate in certificate chain`。附录 A 的 Dockerfile 已内置可删除的 CA 注入段（`COPY proxy-ca.pem`、`update-ca-trust extract`，以及 `PIP_CERT`、`REQUESTS_CA_BUNDLE`、`SSL_CERT_FILE`、`CURL_CA_BUNDLE`、`GIT_SSL_CAINFO` 环境变量），只需准备好证书文件：

```bash
openssl s_client -connect <代理地址>:443 -showcerts </dev/null > chain.txt 2>&1
```

从输出中提取自签名根证书保存为 `proxy-ca.pem`，放入构建目录即可。构建前可用 `docker run --rm --network host ... bash -lc 'update-ca-trust extract; yum -y makecache'` 验证 CA 是否生效。

>[!NOTE]说明
> 不建议在正式镜像中使用 `yum --setopt=sslverify=0` 或 `pip --trusted-host` 关闭校验，这些参数仅可用于临时排障；若目标网络没有 TLS 中间人，可整段删除附录 A 中的 CA 注入。

### 3.5 MindIE Motor 源码获取方式

参考 Dockerfile 在构建阶段会从 GitCode 克隆 MindIE Motor 源码，通过 `MOTOR_REPO`、`MOTOR_REF` 指定仓库和分支，通过 `MOTOR_COMMIT` 可精确固定 commit，避免 `master` 分支漂移导致镜像不可复现。

>[!NOTE]说明
> 如构建环境无法访问 GitCode，可将源码提前下载到构建目录，并修改 Dockerfile 中的 Motor 构建阶段，改为 `COPY` 本地源码后再执行 `bash build.sh`。

## 4 制作步骤

### 4.1 预检

在构建机执行以下命令，确认架构、Docker 版本和磁盘空间：

```bash
uname -m
docker version
docker info | grep -E "Server Version|Storage Driver|Docker Root Dir"
df -h
```

确认基础镜像是否已存在：

```bash
docker images | grep kylin
```

### 4.2 准备构建目录与 Dockerfile

```bash
mkdir -p /opt/motor-build && cd /opt/motor-build
```

将[附录 A](#附录-a参考-dockerfile)中的参考 Dockerfile 保存为 `Dockerfile_motor_kylin`，构建目录结构如下：

```text
/opt/motor-build/
├── Dockerfile_motor_kylin
├── proxy-ca.pem           # 可选，代理 CA（3.4）
└── wheels/                # 4.3 的 7 个 wheel，附录 Dockerfile 直接 COPY
```

### 4.3 准备本地 wheel

在可联网环境中下载以下 wheel，放入构建目录的 `wheels` 子目录：

```bash
mkdir -p wheels && cd wheels

# 1) vLLM 的构建依赖 torch 2.13.0
curl -k -L --retry 30 --retry-delay 2 --retry-all-errors -C - \
  -o 'torch-2.13.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl' \
  'https://download.pytorch.org/whl/cpu/torch-2.13.0%2Bcpu-cp312-cp312-manylinux_2_28_aarch64.whl'

# 2) vllm-ascend 运行时 torch 2.10.0
curl -k -L --retry 30 --retry-delay 2 --retry-all-errors -C - \
  -o 'torch-2.10.0+cpu-cp312-cp312-linux_aarch64.whl' \
  'https://download.pytorch.org/whl/cpu/torch-2.10.0%2Bcpu-cp312-cp312-linux_aarch64.whl'

# 3) torchvision、torchaudio
curl -k -L --retry 30 --retry-delay 2 --retry-all-errors -C - \
  -o 'torchvision-0.25.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl' \
  'https://download.pytorch.org/whl/cpu/torchvision-0.25.0%2Bcpu-cp312-cp312-manylinux_2_28_aarch64.whl'

curl -k -L --retry 30 --retry-delay 2 --retry-all-errors -C - \
  -o 'torchaudio-2.10.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl' \
  'https://download.pytorch.org/whl/cpu/torchaudio-2.10.0%2Bcpu-cp312-cp312-manylinux_2_28_aarch64.whl'

# 4) triton 3.6.0（triton-ascend 依赖）
python3 -m pip download triton==3.6.0 \
  --no-deps --only-binary=:all: \
  --python-version 3.12 --implementation cp --abi cp312 \
  --platform manylinux_2_28_aarch64 \
  -d . -i https://pypi.tuna.tsinghua.edu.cn/simple

# 5) numpy 1.26.4（memfabric、memcache 的版本约束）
python3 -m pip download numpy==1.26.4 \
  --no-deps --only-binary=:all: \
  --python-version 3.12 --implementation cp --abi cp312 \
  --platform manylinux2014_aarch64 \
  -d . -i https://pypi.tuna.tsinghua.edu.cn/simple

# 6) flash-linear-attention wheel
curl -k -L --retry 20 --retry-delay 2 --retry-all-errors -C - \
  -o 'flash_linear_attention_npu_a3-26.9.1-py3-none-manylinux_2_34_aarch64.whl' \
  'https://github.com/flashserve/flash-linear-attention-npu/releases/download/v26.9.1-release/flash_linear_attention_npu_a3-26.9.1-py3-none-manylinux_2_34_aarch64.whl'
```

下载完成后建议用 `sha256sum *.whl` 记录校验值，便于制品管理。附录 A 的 Dockerfile 已直接 `COPY` 这些 wheel（torch 2.13.0、torch 2.10.0、torchvision、torchaudio、triton、numpy、FLA 共 7 个），无需再改 Dockerfile；若不使用本地 wheel，删除对应 `COPY` 与该层的本地 `pip install`，改回在线安装即可。

### 4.4 构建镜像

在构建目录执行构建命令，输出镜像 `mindie-motor:0.30.0-cann9.2.1-a3-kylin-v11-motor`：

```bash
cd /opt/motor-build

export DOCKER_BUILDKIT=1

docker build --progress=plain --network=host --platform linux/arm64 \
  --build-arg PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple" \
  --build-arg MOTOR_REF="master" \
  --build-arg MOTOR_COMMIT="" \
  -f Dockerfile_motor_kylin \
  -t mindie-motor:0.30.0-cann9.2.1-a3-kylin-v11-motor \
  . 2>&1 | tee build.log
```

如构建网络需要代理，可在 `docker build` 前导出 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`，并通过 `--build-arg` 传入同一组变量，保证 `RUN` 阶段也能使用代理。

常用构建参数如下，均可在 Dockerfile 的 `ARG` 中覆盖：

| 参数 | 说明 | 本文取值 |
| --- | --- | --- |
| `CANN_TOOLKIT_URL` / `CANN_OPS_URL` / `CANN_NNAL_URL` | CANN 9.2.1 的 toolkit、A3-ops、nnal 下载地址 | 华为 OBS |
| `PYTHON_VERSION` | Python 版本 | `3.12.16` |
| `VLLM_TAG` | vLLM 版本 | `v0.30.0` |
| `VLLM_ASCEND_BRANCH` | vllm-ascend 分支 | `releases/v0.30.0` |
| `SOC_VERSION` | 芯片型号 | `ascend910_9391` |
| `MOTOR_REPO` | MindIE Motor 仓库地址 | `https://gitcode.com/Ascend/MindIE-Motor.git` |
| `MOTOR_REF` | 构建使用的分支或标签 | `master` |
| `MOTOR_COMMIT` | 固定 commit，非空时优先使用 | 空 |
| `PIP_INDEX_URL` | pip 软件源 | `https://pypi.tuna.tsinghua.edu.cn/simple` |

>[!NOTE]说明
> CANN 版本没有独立变量，`9.2.1` 写死在三个 `.run` 下载地址与镜像内 `/usr/local/Ascend/cann-9.2.1` 路径中，换版本需全文替换。
> 上表只列常用参数，全部 `ARG`（含各仓库 commit、Mooncake/FLA wheel、Rust 镜像源、用户与 UID/GID 等）及默认值见附录 A 开头的声明段。
> 镜像用户由 `USER`/`GROUP`/`U_ID`/`GID` 四个参数决定（默认 `aiuser`/1001），Dockerfile 内的用户目录与 `HOME` 均引用该参数；自定义时需四个参数一起传，例如 `--build-arg USER=motor --build-arg GROUP=motor --build-arg U_ID=1002 --build-arg GID=1002`。
> FLA wheel 由 `FLA_VERSION`/`FLA_WHL` 决定，覆盖时需同时把对应文件放入 `wheels/`（附录 A 用 `COPY wheels/${FLA_WHL}` 复制）。
> `MOTOR_REF=master` 仅适合开发验证。生产环境建议通过 `MOTOR_COMMIT` 固定 MindIE Motor 版本，并同时固定 vLLM 与 vllm-ascend 的 commit。

### 4.5 监控构建过程

```bash
ps -ef | grep -E "docker build|pip|wget"              # 构建进程
docker ps --format '{{.ID}} {{.Image}} {{.Command}}'  # 构建容器
tail -c 3000 build.log                                # 日志尾部
df -h                                                 # 磁盘使用
```

BuildKit 日志关键字：`#N [x/y] RUN ...`（第 N 层第 x/y 步）、`CACHED`（命中缓存未重跑）、`DONE`（该层成功）、`ERROR: process ... did not complete successfully`（该层失败）、`[output clipped, log limit 2MiB reached]`（单层日志超 2 MiB 被截断，不代表失败，用 `tail -c` 看尾部）。

## 5 镜像验证

### 5.1 版本与依赖导入

```bash
docker run --rm --network host \
  mindie-motor:0.30.0-cann9.2.1-a3-kylin-v11-motor \
  bash -lc '
    cat /etc/os-release | head -4
    python3 --version
    source /usr/local/Ascend/cann-9.2.1/set_env.sh
    source /usr/local/Ascend/nnal/atb/set_env.sh
    python3 -c "import torch, torch_npu, vllm, vllm_ascend, motor; print(torch.__version__, torch_npu.__version__, vllm.__version__, motor.__version__)"
  '
```

预期输出与 1.2 节版本配套一致：`Kylin Linux Advanced Server V11`、`Python 3.12.16`、`2.10.0+cpu 2.10.0.post7 0.30.0 3.1.0`。

### 5.2 kv-conductor 验证

kv-conductor 以二进制形式打包在 `motor` wheel 中：

```bash
docker run --rm --network host \
  mindie-motor:0.30.0-cann9.2.1-a3-kylin-v11-motor \
  bash -lc '
    python3 -c "from motor.kv_conductor import get_binary_path; import os; p=get_binary_path(); print(p, os.path.isfile(str(p)))"
    python3 -m motor.kv_conductor --help | head -8
  '
```

预期输出二进制路径存在且 `--help` 正常打印，例如 `/usr/local/lib/python3.12/site-packages/motor/kv_conductor/bin/kv-conductor True`；若为 `False`，处理方法见[7.8 kv-conductor 未打进 wheel](#78-kv-conductor-未打进-wheel)。

### 5.3 Motor 组件导入与 examples 检查

```bash
docker run --rm --network host \
  mindie-motor:0.30.0-cann9.2.1-a3-kylin-v11-motor \
  bash -lc '
    python3 -c "import importlib; [importlib.import_module(m) for m in [\"motor.coordinator.main\",\"motor.controller.main\",\"motor.node_manager.main\",\"motor.kv_conductor\",\"kubernetes\"]]; print(\"MOTOR_IMPORT_OK\")"
    ls -ld /tmp/motor /tmp/motor/examples
  '
```

预期：打印 `MOTOR_IMPORT_OK`，且 `/tmp/motor`、`/tmp/motor/examples` 存在（属主 `aiuser`）。

### 5.4 进程启动验证

本节不挂载 NPU，只验证镜像内 MindIE Motor 与 kv-conductor 进程能否正常拉起。

```bash
mkdir -p /opt/motor-smoke/logs && chmod 777 /opt/motor-smoke/logs
docker rm -f motor-smoke-test kv-conductor-smoke-test >/dev/null 2>&1 || true

docker run -d --name motor-smoke-test --network host \
  -v /opt/motor-smoke/logs:/opt/motor-logs -e MOTOR_LOG_PATH=/opt/motor-logs \
  mindie-motor:0.30.0-cann9.2.1-a3-kylin-v11-motor \
  bash -lc 'source /usr/local/Ascend/cann-9.2.1/set_env.sh; source /usr/local/Ascend/nnal/atb/set_env.sh; exec python3 -m motor.coordinator.main'
sleep 20
curl -sS --max-time 5 http://127.0.0.1:1026/liveness
curl -sS --max-time 5 http://127.0.0.1:1026/readiness

docker run -d --name kv-conductor-smoke-test --network host \
  mindie-motor:0.30.0-cann9.2.1-a3-kylin-v11-motor \
  bash -lc 'exec python3 -m motor.kv_conductor --host 127.0.0.1 --port 13333'
sleep 8
curl -sS --max-time 5 http://127.0.0.1:13333/health
curl -sS --max-time 5 http://127.0.0.1:13333/workers
docker rm -f motor-smoke-test kv-conductor-smoke-test
```

预期：Coordinator 容器为 `Up`，日志出现 `MgmtProcess`、`ObsProcess`、`InferenceWorkers` started successfully 与 `Scheduler started`、`Uvicorn running on http://127.0.0.1:1026`；`/liveness` 返回 `{"status":"ok","message":"Coordinator is alive"}`，`/readiness` 返回 `{"status":"ok","message":"Coordinator is ok","ready":false}`（空拓扑启动、未注册 P/D 实例，属正常）。kv-conductor 的 `/health` 返回 `OK`，`/workers` 返回 `{"indexer":[],"workers":[]}`（未注册 Worker，属正常）。

### 5.5 完整链路验证建议

完整链路需在安装 NPU 驱动的服务器上验证：确认 `npu-smi info` 正常，启动 P/D vLLM 实例，使用 `python3 -m motor.coordinator.register` 注册实例，请求 `/v1/completions` 或 `/v1/chat/completions`，开启 KV Cache 亲和后检查 `matched`、`hit` 指标与 kv-conductor 的 `/workers` 返回。部署方法请参考[PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md)或[PD 混部服务部署](../deployment/k8s/pd_aggregation_deployment.md)。

## 6 镜像导出与分发

```bash
# 导出
docker save mindie-motor:0.30.0-cann9.2.1-a3-kylin-v11-motor -o mindie-motor-0.30.0-cann9.2.1-a3-kylin-v11-motor.tar
# 其他服务器导入
docker load -i mindie-motor-0.30.0-cann9.2.1-a3-kylin-v11-motor.tar
# K8s 集群使用 containerd 时直接导入 k8s 命名空间
ctr -n k8s.io images import mindie-motor-0.30.0-cann9.2.1-a3-kylin-v11-motor.tar
```

>[!NOTE]说明
> 部署 K8s 集群时需将镜像加载到集群全部节点，具体参考[镜像安装](./build_motor_image_from_vllm_ascend.md)。

## 7 常见问题处理

### 7.1 docker pull 报 Proxy Authentication Required

原因：Docker daemon 的代理密码含 `@` 等特殊字符未做百分号编码。处理：不改动共享 daemon，用 `skopeo` 以正确编码的代理导出归档后再导入：

```bash
env HTTPS_PROXY="http://<代理用户>:<编码后的密码>@<代理地址>:<端口>" \
    HTTP_PROXY="http://<代理用户>:<编码后的密码>@<代理地址>:<端口>" \
  skopeo copy --src-tls-verify=false \
  docker://cr.kylinos.cn/kylin/kylin-server-platform:v11-2503 \
  docker-archive:./kylin-v11-2503.tar:cr.kylinos.cn/kylin/kylin-server-platform:v11-2503

docker load -i ./kylin-v11-2503.tar
```

如确需修正 daemon 代理，应在维护窗口调整 `/etc/systemd/system/docker.service.d/` 后执行 `systemctl daemon-reload && systemctl restart docker`。

### 7.2 yum 报 self-signed certificate in certificate chain

构建网络存在 TLS 中间人代理，麒麟基础镜像内不含代理 CA。处理：按[3.4 代理与证书](#34-代理与证书可选)准备 `proxy-ca.pem`（附录 A 的 Dockerfile 已内置注入段），再用 `yum makecache` 验证。

### 7.3 torch、triton、numpy、FLA 等 wheel 下载中断

现象为 `error: incomplete-download` 或 `Connection interrupted while downloading`；FLA 的 GitHub Release wheel（约 60 MB）经代理下载还会长时间无进度。原因：PyTorch CDN、GitHub Release 经代理不稳定，且 vLLM 的 PEP517 构建隔离会重复下载构建依赖。处理：按[4.3 准备本地 wheel](#43-准备本地-wheel)预下载后由附录 A 的 Dockerfile 直接 `COPY` 安装；vLLM 安装使用 `--no-build-isolation`；vllm-ascend 依赖安装前先装本地 torch、torchvision、torchaudio、triton、numpy。

### 7.4 rm 报 Operation not permitted

`COPY` 到 `/tmp` 的文件属主为 root，`RUN` 以普通用户执行且 `/tmp` 带 sticky bit。处理：`sudo rm -f`，或将 wheel 放到普通用户可写目录并 `chown`。

### 7.5 Kylin 无 delgroup、GID 被占用

创建用户阶段 `delgroup` 不存在，或 GID 已被占用导致 `groupadd` 失败。附录 A 的 Dockerfile 已改为先用 `getent` 判断，再依次 `userdel`、`groupdel`、`groupadd`，无需再手工打补丁。

### 7.6 docker run 报 docker0、veth 错误

宿主机 Docker bridge 网络异常。构建与验证统一使用 `--network host` 绕过。

### 7.7 构建日志被截断

`[output clipped, log limit 2MiB reached]` 是 BuildKit 单层 2 MiB 日志上限，不代表构建失败。用 `--progress=plain` 重定向到 `build.log`，通过 `tail -c 3000 build.log` 查看尾部，或看该层最终的 `DONE`／`ERROR` 状态。

### 7.8 kv-conductor 未打进 wheel

现象：`motor` 可导入，但 `motor/kv_conductor/bin/kv-conductor` 不存在，或 `python3 -m motor.kv_conductor` 报错。原因：`build.sh` 在缺少 cargo、libzmq 或 pkg-config 时会跳过 kv-conductor。处理：确认镜像内已安装 `zeromq-devel`、`pkgconfig`、`g++` 和 Rust 工具链，检查构建日志，并用 `unzip -l dist/motor-*.whl | grep -E 'kv-conductor|libmindie_workload_shm.so'` 确认 wheel 内容。

### 7.9 pip check 依赖冲突

| 提示 | 说明 |
| --- | --- |
| `flash-linear-attention-npu-a3` 缺 `expecttest` | FLA 测试依赖，不影响推理主流程 |
| `vllm` 缺 `anthropic` | 镜像精简阶段被卸载；如使用 Anthropic Messages API，需重新安装 |
| `vllm-ascend` 要求 `fastapi<0.124.0` | vLLM 0.30 与 vllm-ascend 0.30.0 的上游约束冲突，需结合 API 路径验证；必要时固定到相互匹配的 commit |
| `opencv-python-headless` 要求 `numpy>=2` | memfabric、memcache 将 numpy 约束为 1.26.4，仅影响 OpenCV 图像与视频路径 |

### 7.10 Ray 被卸载影响多机部署

参考 Dockerfile 在镜像精简阶段执行 `pip uninstall -y ray`。如 PD 分离、多机 vLLM 并行依赖 Ray，需在镜像中保留或重新安装 Ray，否则多机分布式能力不可用。

## 8 已知限制

1. 未安装 `moxing_framework`，如需 ModelArts moxing 能力需额外安装。
2. 未安装 CCAE Reporter，如需可观测能力需追加 `examples/features/observability` 的构建。
3. 默认卸载 Ray，多机 Ray 场景需恢复。
4. vLLM 0.30 与 vllm-ascend 0.30.0 的 FastAPI 版本约束存在上游冲突，涉及 FastAPI 路由的接口需单独验证。
5. 本文仅完成镜像内导入和进程启动验证，未在 NPU 上完成完整推理链路验证。
6. `MOTOR_REF=master` 不适合可复现交付，生产环境应固定 `MOTOR_COMMIT`、`VLLM_COMMIT`、`VLLM_ASCEND_COMMIT`。
7. 麒麟 V11 与官方镜像的操作系统差异需持续关注，内核、glibc、GCC 版本变化可能引入新的适配点。

## 附录 A：参考 Dockerfile

以下为在麒麟 V11 aarch64 上制作 MindIE Motor 镜像的完整 Dockerfile，正文各节的适配点均已固化在其中。将其保存为 `Dockerfile_motor_kylin` 放在构建目录后，按[4.4 构建镜像](#44-构建镜像)执行构建；构建上下文需包含[4.3 准备本地 wheel](#43-准备本地-wheel) 的 `wheels/` 目录，如构建网络有 TLS 中间人代理还需放入 `proxy-ca.pem`（无代理时删除 CA 注入段即可）。

```dockerfile
FROM cr.kylinos.cn/kylin/kylin-server-platform:v11-2503

SHELL ["/bin/bash", "-c"]

ARG PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
ARG PIP_TRUSTED_HOST="pypi.tuna.tsinghua.edu.cn"
ARG GH_PROXY=""

# CANN 9.2.1 A3，与 Ascend/cann-container-image 的 9.2.1-a3 使用同一组 .run
# 说明：CANN 版本没有变量。9.2.1 写死在三个 .run 下载 URL、以及本文件其余 31 处
# /usr/local/Ascend/cann-9.2.1 路径与 ASCEND_HOME_PATH 中；换 CANN 版本要全文替换 "9.2.1"。
ARG CANN_TOOLKIT_URL="https://ascend-repo.obs.cn-east-2.myhuaweicloud.com/CANN/CANN%209.2.1/Ascend-cann-toolkit_9.2.1_linux-aarch64.run"
ARG CANN_OPS_URL="https://ascend-repo.obs.cn-east-2.myhuaweicloud.com/CANN/CANN%209.2.1/Ascend-cann-A3-ops_9.2.1_linux-aarch64.run"
ARG CANN_NNAL_URL="https://ascend-repo.obs.cn-east-2.myhuaweicloud.com/CANN/CANN%209.2.1/Ascend-cann-nnal_9.2.1_linux-aarch64.run"

ARG PYTHON_VERSION="3.12.16"
ARG PYTHON_SRC_URL="https://repo.huaweicloud.com/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz"

ARG VLLM_REPO="https://github.com/vllm-project/vllm.git"
ARG VLLM_TAG="v0.30.0"
ARG VLLM_COMMIT=""
ARG VLLM_ASCEND_REPO="https://github.com/vllm-project/vllm-ascend.git"
ARG VLLM_ASCEND_BRANCH="releases/v0.30.0"
ARG VLLM_ASCEND_COMMIT=""
ARG MOTOR_REPO="https://gitcode.com/Ascend/MindIE-Motor.git"
ARG MOTOR_REF="master"
ARG MOTOR_COMMIT=""
ARG SOC_VERSION="ascend910_9391"
ARG COMPILE_CUSTOM_KERNELS=1
ARG PYTORCH_INDEX_URL="https://download.pytorch.org/whl/cpu/"
ARG ASCEND_INDEX_URL="https://mirrors.huaweicloud.com/ascend/repos/pypi"

# 注意：OBS 目录名是 v0.3.14.rc1，但该目录下实际存放的 wheel 文件名是 0.3.13（发布侧命名不一致，
# 不是笔误）。升级 Mooncake 时目录名与文件名要一起改。
ARG MOONCAKE_OBS_PREFIX="https://mindie.obs.cn-north-4.myhuaweicloud.com/buildtools/docker_images_tools/Mooncake/v0.3.14.rc1"
ARG MOONCAKE_WHL="mooncake_transfer_engine-0.3.13-cp312-cp312-manylinux_2_35_aarch64.whl"
ARG FLA_VERSION="26.9.1"
ARG FLA_WHL="flash_linear_attention_npu_a3-${FLA_VERSION}-py3-none-manylinux_2_34_aarch64.whl"


ARG RUSTUP_DIST_SERVER="https://mirrors.ustc.edu.cn/rust-static"
ARG RUSTUP_UPDATE_ROOT="https://mirrors.ustc.edu.cn/rust-static/rustup"
ARG CRATES_IO_INDEX="https://mirrors.ustc.edu.cn/crates.io-index/"

ARG USER=aiuser
ARG U_ID=1001
ARG GID=1001
ARG GROUP=aiuser

ENV SOC_VERSION=${SOC_VERSION} \
    COMPILE_CUSTOM_KERNELS=${COMPILE_CUSTOM_KERNELS} \
    TASK_QUEUE_ENABLE=1 \
    OMP_NUM_THREADS=1 \
    ASCEND_HOME_PATH=/usr/local/Ascend/cann-9.2.1

WORKDIR /workspace

# 企业代理 HTTPS 拦截 CA；与 build context 中的 proxy-ca.pem 一起使用。
# 若目标环境不是该代理，可删除本段。
COPY proxy-ca.pem /etc/pki/ca-trust/source/anchors/proxy-ca.pem
RUN update-ca-trust extract

ENV PIP_CERT=/etc/pki/tls/certs/ca-bundle.crt \
    REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt \
    CURL_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt \
    GIT_SSL_CAINFO=/etc/pki/tls/certs/ca-bundle.crt

#================ 1. 系统工具 + hostname（必须在 CANN .run 之前，root） ================
# .run 子包会调用 flock（util-linux）、wget、gcc。
# 麒麟基座经常没有 hostname 命令，CANN / 业务脚本会直接调用它。
# 麒麟 yum 没有 clang-15：装系统 clang。triton-ascend 使用 cp312 wheel，不在镜像里从源码编译。
# protobuf-devel 提供 protoc，供 vLLM Rust frontend。
RUN set -e && \
    yum install -y \
        util-linux hostname \
        git vim wget curl net-tools pciutils which findutils file sudo \
        gcc gcc-c++ gcc-gfortran make cmake patch clang \
        automake autoconf libtool binutils \
        tar gzip bzip2 xz unzip zip \
        zlib-devel libffi-devel openssl openssl-devel sqlite-devel \
        readline-devel ncurses-devel bzip2-devel xz-devel gdbm-devel libuuid-devel \
        lapack-devel blas-devel \
        numactl numactl-devel libibverbs-devel \
        jemalloc jemalloc-devel hiredis-devel zeromq-devel \
        protobuf-devel \
        python3-devel python3-pip && \
    yum clean all && \
    rm -rf /var/cache/yum/* && \
    command -v hostname

# pkg-config 供 kv-conductor 的 zmq-sys 构建脚本查找 libzmq。
RUN yum install -y pkgconfig || yum install -y pkgconf-pkg-config || yum install -y pkgconf

#================ 2. 源码编译 Python 3.12（root） ================
# CANN .run 会把 pyACL 装进当前 python3。先把 3.12 放到 PATH 前面。
# /usr/bin/python3 仍是系统自带的 3.11。
# 3.12.16 与官方 CANN 9.2.1 镜像使用的 Python 小版本一致。
RUN set -e && \
    uname -m | grep -qx aarch64 && \
    unset LIBRARY_PATH && \
    cd /tmp && \
    wget -nv --tries=3 --timeout=120 "${PYTHON_SRC_URL}" -O Python.tgz && \
    tar -xzf Python.tgz && \
    cd Python-${PYTHON_VERSION} && \
    ./configure --prefix=/usr/local --enable-shared --with-ensurepip=install || { \
        echo '----- config.log (compiler test) -----'; \
        grep -n -A 40 'cannot create executables\|crt1.o\|libgcc_s' config.log | head -n 80 || true; \
        exit 1; \
    } && \
    make -j"$(nproc)" && \
    make altinstall && \
    echo /usr/local/lib >/etc/ld.so.conf.d/python3.12.conf && \
    ldconfig && \
    ln -sf /usr/local/bin/python3.12 /usr/local/bin/python3 && \
    ln -sf /usr/local/bin/python3.12 /usr/local/bin/python && \
    ln -sf /usr/local/bin/pip3.12 /usr/local/bin/pip3 && \
    ln -sf /usr/local/bin/pip3.12 /usr/local/bin/pip && \
    cd / && rm -rf /tmp/Python-${PYTHON_VERSION} /tmp/Python.tgz && \
    export PATH="/usr/local/bin:${PATH}" && \
    export LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" && \
    python3 --version && \
    python3 -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version' && \
    pip config set global.index-url "${PIP_INDEX_URL}" && \
    pip config set global.trusted-host "${PIP_TRUSTED_HOST}" && \
    python3 -m pip install --upgrade pip setuptools wheel && \
    python3 -m pip install \
        attrs 'numpy<2.0.0' decorator sympy cffi pyyaml pathlib2 psutil \
        protobuf scipy requests absl-py wheel typing_extensions && \
    pip cache purge

ENV PATH="/usr/local/bin:${PATH}" \
    LD_LIBRARY_PATH="/usr/local/lib"

# LIBRARY_PATH 只给后续 bisheng 找 libgcc_s，放在 Python 编译之后。
# "12" 是 kylin-server-platform:v11-2503 实测的 GCC 主版本号。
ENV LIBRARY_PATH="/usr/lib/gcc/aarch64-kylin-linux/12"

#================ 2b. bisheng/cce-ld 的 crt 兜底（必须在 Python 编译之后，root） ================
RUN set -e && \
    link_if_distinct() { \
        src="$1"; dest="$2"; \
        [ -f "${src}" ] || return 0; \
        [ "$(readlink -f "${src}")" = "$(readlink -f "${dest}" 2>/dev/null || true)" ] && return 0; \
        [ "${src}" = "${dest}" ] && return 0; \
        ln -sfn "${src}" "${dest}"; \
    }; \
    GCC_CRT_DIR="$(dirname "$(gcc -print-file-name=crtbeginS.o)")" && \
    echo "GCC_CRT_DIR=${GCC_CRT_DIR}" && \
    for f in crt1.o crti.o crtn.o crtbegin.o crtbeginS.o crtbeginT.o crtend.o crtendS.o; do \
        link_if_distinct "${GCC_CRT_DIR}/${f}" "/usr/lib64/${f}"; \
    done && \
    link_if_distinct "$(gcc -print-libgcc-file-name)" /usr/lib64/libgcc.a && \
    LIBGCC_S="$(gcc -print-file-name=libgcc_s.so)" && \
    link_if_distinct "${LIBGCC_S}" /usr/lib64/libgcc_s.so && \
    if [ -f "${LIBGCC_S}" ]; then \
        REAL_SO1="$(find "$(dirname "${LIBGCC_S}")" -maxdepth 1 -name 'libgcc_s.so.1' 2>/dev/null | head -1)"; \
        link_if_distinct "${REAL_SO1}" /usr/lib64/libgcc_s.so.1; \
    fi && \
    echo 'int main(void){return 0;}' | gcc -x c - -o /tmp/cc-test && rm -f /tmp/cc-test

#================ 3. CANN 9.2.1 A3：toolkit -> A3-ops -> nnal（root） ================
# 安装器会生成 /usr/local/Ascend/cann-9.2.1，以及 ascend-toolkit/latest 指向它。
# nnal 的安装脚本要求先 source toolkit 的 set_env.sh。
RUN set -e && \
    uname -m | grep -qx aarch64 && \
    cd /tmp && \
    wget -nv --tries=3 --timeout=120 --header="Referer: https://www.hiascend.com/" \
        "${CANN_TOOLKIT_URL}" -O Ascend-cann-toolkit.run && \
    wget -nv --tries=3 --timeout=120 --header="Referer: https://www.hiascend.com/" \
        "${CANN_OPS_URL}" -O Ascend-cann-ops.run && \
    wget -nv --tries=3 --timeout=120 --header="Referer: https://www.hiascend.com/" \
        "${CANN_NNAL_URL}" -O Ascend-cann-nnal.run && \
    chmod +x Ascend-cann-toolkit.run Ascend-cann-ops.run Ascend-cann-nnal.run && \
    ./Ascend-cann-toolkit.run --quiet --install --install-for-all && \
    ./Ascend-cann-ops.run --quiet --install --install-for-all && \
    if [ ! -f /usr/local/Ascend/cann-9.2.1/set_env.sh ] && [ -f /usr/local/Ascend/ascend-toolkit/set_env.sh ]; then \
        ln -sfn /usr/local/Ascend/ascend-toolkit/set_env.sh /usr/local/Ascend/cann-9.2.1/set_env.sh; \
    fi && \
    test -f /usr/local/Ascend/cann-9.2.1/set_env.sh && \
    source /usr/local/Ascend/cann-9.2.1/set_env.sh && \
    ./Ascend-cann-nnal.run --quiet --install --install-for-all && \
    test -f /usr/local/Ascend/nnal/atb/set_env.sh && \
    rm -f /tmp/Ascend-cann-*.run

#================ 4. 创建 aiuser（UID/GID 1001），移交 Python 与工作目录（root） ================
RUN set -e && \
    if getent passwd ${U_ID} >/dev/null; then \
        userdel -r "$(getent passwd ${U_ID} | cut -d: -f1)" || true; \
    fi && \
    if getent group ${GID} >/dev/null; then \
        groupdel "$(getent group ${GID} | cut -d: -f1)" || true; \
    fi && \
    groupadd -g ${GID} ${GROUP} && \
    useradd -m -u ${U_ID} -g ${GID} -s /bin/bash -c "AI User" ${USER} && \
    mkdir -p /etc/sudoers.d && \
    echo "${USER} ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/${USER} && \
    chmod 440 /etc/sudoers.d/${USER} && \
    sed -i '/# If not running interactively/,/^esac$/d' /home/${USER}/.bashrc

RUN set -e && \
    mkdir -p /workspace /vllm-workspace && \
    chown -R ${U_ID}:${GID} \
        /workspace /vllm-workspace /home/${USER} \
        /usr/local/lib/python3.12 /usr/local/bin

#================ 5. 切换到 aiuser，配置 git/pip 镜像 ================
USER ${USER}
ENV HOME=/home/${USER}
WORKDIR /home/${USER}

RUN set -e && \
    git config --global url."${GH_PROXY}https://github.com/".insteadOf "https://github.com/" && \
    pip config --user set global.index-url "${PIP_INDEX_URL}" && \
    pip config --user set global.trusted-host "${PIP_TRUSTED_HOST}"

#================ 6. modelscope + ray + protobuf（官方 Dockerfile.a3 同款约束） ================
# ray 在这里按官方版本装上。安全整改段会把它卸载掉。
RUN set -e && \
    python3 -m pip install 'modelscope<1.38' 'ray>=2.47.1,<=2.48.0' 'protobuf>3.20.0' && \
    pip cache purge

#================ 7. vLLM @ v0.30.0（或 VLLM_COMMIT） ================
RUN set -e && \
    if [ -n "${VLLM_COMMIT}" ]; then \
        git init /vllm-workspace/vllm && \
        git -C /vllm-workspace/vllm fetch --depth 1 "${VLLM_REPO}" "${VLLM_COMMIT}" && \
        git -C /vllm-workspace/vllm checkout FETCH_HEAD; \
    else \
        git clone --depth 1 -b "${VLLM_TAG}" "${VLLM_REPO}" /vllm-workspace/vllm; \
    fi && \
    git -C /vllm-workspace/vllm rev-parse HEAD

# 预先放入 torch wheel，避免构建机访问 download.pytorch.org 不稳定导致下载失败。
# 本地 wheel 文件名与正文 4.3 的下载清单一致；升级版本时两处需同步修改。
COPY wheels/torch-2.13.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl /tmp/torch-2.13.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl

# Python 3.12 上 vLLM 需要 setuptools>=77,<81，避免升到 81 后丢掉 distutils。
# 先装齐 vLLM 的 PEP517 build 依赖并关闭 build isolation，避免构建隔离环境再次下载 torch。
# Ascend 上卸载 GPU 版 triton。triton-ascend 由后面的 vllm-ascend 依赖安装。
RUN set -e && \
    source /usr/local/Ascend/cann-9.2.1/set_env.sh && \
    source /usr/local/Ascend/nnal/atb/set_env.sh && \
    python3 -m pip install 'setuptools>=77.0.3,<81' cmake ninja packaging setuptools-scm setuptools-rust wheel jinja2 && \
    python3 -m pip install /tmp/torch-2.13.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl && \
    VLLM_TARGET_DEVICE="empty" python3 -m pip install -e "/vllm-workspace/vllm" \
        --no-build-isolation --extra-index-url ${PYTORCH_INDEX_URL} && \
    python3 -m pip uninstall -y triton && \
    pip cache purge

#================ 8. vllm-ascend @ releases/v0.30.0（官方是 COPY，这里改为 git clone） ================
RUN set -e && \
    if [ -n "${VLLM_ASCEND_COMMIT}" ]; then \
        git init /vllm-workspace/vllm-ascend && \
        git -C /vllm-workspace/vllm-ascend fetch --depth 1 "${VLLM_ASCEND_REPO}" "${VLLM_ASCEND_COMMIT}" && \
        git -C /vllm-workspace/vllm-ascend checkout FETCH_HEAD; \
    else \
        git clone --depth 1 -b "${VLLM_ASCEND_BRANCH}" "${VLLM_ASCEND_REPO}" /vllm-workspace/vllm-ascend; \
    fi && \
    git -C /vllm-workspace/vllm-ascend rev-parse HEAD

# requirements.txt 含 arctic-inference。它的 setup.py 会 import torch，
# torch 2.10 会连带 import torch_npu。先摘掉，下一步单独装。
# 同样预置 torch/torchvision/torchaudio 2.10 的 CPU wheel，规避下载不稳定。
# triton 必须按三步处理，缺一不可：
#   ① 先装本地 3.6.0 wheel，让 requirements 解析时不从 PyTorch 索引下载；
#   ② requirements 安装可能把 triton 替换成 GPU 版，装完统一卸载；
#   ③ 编译 vllm-ascend 前再装回本地 wheel（triton-ascend 依赖 triton==3.6.0）。
COPY wheels/torch-2.10.0+cpu-cp312-cp312-linux_aarch64.whl \
     wheels/torchvision-0.25.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl \
     wheels/torchaudio-2.10.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl \
     wheels/triton-3.6.0-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl \
     wheels/numpy-1.26.4-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl \
     /tmp/wheels/
RUN set -e && \
    export PIP_EXTRA_INDEX_URL="${ASCEND_INDEX_URL}" && \
    source /usr/local/Ascend/cann-9.2.1/set_env.sh && \
    source /usr/local/Ascend/nnal/atb/set_env.sh && \
    python3 -m pip install \
        /tmp/wheels/torch-2.10.0+cpu-cp312-cp312-linux_aarch64.whl \
        /tmp/wheels/torchvision-0.25.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl \
        /tmp/wheels/torchaudio-2.10.0+cpu-cp312-cp312-manylinux_2_28_aarch64.whl \
        /tmp/wheels/triton-3.6.0-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl \
        /tmp/wheels/numpy-1.26.4-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl && \
    grep -v '^arctic-inference' /vllm-workspace/vllm-ascend/requirements.txt \
        > /tmp/requirements-no-arctic.txt && \
    python3 -m pip install -r /tmp/requirements-no-arctic.txt \
        --extra-index-url ${PYTORCH_INDEX_URL} && \
    (python3 -m pip uninstall -y triton || true) && \
    python3 -m pip install attrs googleapis-common-protos ninja 'setuptools>=77.0.3,<81' && \
    rm -f /tmp/requirements-no-arctic.txt && \
    pip cache purge

RUN set -e && \
    export PIP_EXTRA_INDEX_URL="${ASCEND_INDEX_URL}" && \
    export TORCH_DEVICE_BACKEND_AUTOLOAD=0 && \
    CANN_LIB="" && \
    for d in \
        /usr/local/Ascend/cann-9.2.1/lib64 \
        /usr/local/Ascend/cann-9.2.1/devlib \
        /usr/local/Ascend/cann-9.2.1/aarch64-linux/lib64 \
        /usr/local/Ascend/cann-9.2.1/aarch64-linux/devlib \
        /usr/local/lib; do \
        [ -d "${d}" ] && CANN_LIB="${CANN_LIB:+${CANN_LIB}:}${d}"; \
    done && \
    export LD_LIBRARY_PATH="${CANN_LIB}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" && \
    source /usr/local/Ascend/cann-9.2.1/set_env.sh && \
    source /usr/local/Ascend/nnal/atb/set_env.sh && \
    python3 -m pip install nanobind && \
    python3 -m pip install --no-build-isolation arctic-inference==0.1.1 \
        --extra-index-url ${PYTORCH_INDEX_URL} && \
    pip cache purge

# triton 三步处理中的第 ③ 步（①② 见上一层注释）：
# triton-ascend 依赖 triton==3.6.0；这里用本地 wheel 重新安装，避免再次访问 PyTorch 索引。
RUN python3 -m pip install /tmp/wheels/triton-3.6.0-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl

#================ 9. 麒麟编译安装 vllm-ascend ================
# HCC g++/c++ 是 GCC 7.3，只编 AICPU。包装里 unset CPLUS_INCLUDE_PATH。
# bisheng 使用本层导出的 CPLUS_INCLUDE_PATH（GCC 12 头）。
# build_aclnn.sh 的 .run 加 --force，跳过无驱动构建机的环境校验。
# hcc/bin 属主是 root，改写用 sudo。源码在 /vllm-workspace/vllm-ascend，属主是 aiuser。
RUN set -e && \
    if [ -d /usr/local/Ascend/ascend-toolkit/latest/toolkit/toolchain/hcc/bin ]; then \
        HCC_BIN=/usr/local/Ascend/ascend-toolkit/latest/toolkit/toolchain/hcc/bin; \
    elif [ -d /usr/local/Ascend/cann-9.2.1/toolkit/toolchain/hcc/bin ]; then \
        HCC_BIN=/usr/local/Ascend/cann-9.2.1/toolkit/toolchain/hcc/bin; \
    else \
        echo "hcc bin not found under cann-9.2.1" >&2; \
        find /usr/local/Ascend -type d -name hcc -print >&2 || true; \
        exit 1; \
    fi && \
    echo "HCC_BIN=${HCC_BIN}" && \
    REAL_GPP="$(readlink -f "${HCC_BIN}/aarch64-target-linux-gnu-g++")" && \
    sudo mv "${REAL_GPP}" "${HCC_BIN}/aarch64-target-linux-gnu-g++.real" && \
    sudo rm -f "${HCC_BIN}/aarch64-target-linux-gnu-g++" "${HCC_BIN}/aarch64-target-linux-gnu-c++" && \
    printf '%s\n' \
        '#!/bin/bash' \
        'unset CPLUS_INCLUDE_PATH' \
        "exec ${HCC_BIN}/aarch64-target-linux-gnu-g++.real \"\$@\"" \
        | sudo tee "${HCC_BIN}/aarch64-target-linux-gnu-g++" > /dev/null && \
    sudo cp "${HCC_BIN}/aarch64-target-linux-gnu-g++" "${HCC_BIN}/aarch64-target-linux-gnu-c++" && \
    sudo chmod 755 "${HCC_BIN}/aarch64-target-linux-gnu-g++" "${HCC_BIN}/aarch64-target-linux-gnu-c++" && \
    sed -i 's/--install-path=/--force --install-path=/' \
        /vllm-workspace/vllm-ascend/csrc/build_aclnn.sh && \
    export PIP_EXTRA_INDEX_URL="${ASCEND_INDEX_URL}" && \
    export VLLM_BATCH_INVARIANT=1 && \
    CANN_LIB="" && \
    for d in \
        /usr/local/Ascend/cann-9.2.1/lib64 \
        /usr/local/Ascend/cann-9.2.1/devlib \
        /usr/local/Ascend/cann-9.2.1/aarch64-linux/lib64 \
        /usr/local/Ascend/cann-9.2.1/aarch64-linux/devlib; do \
        [ -d "${d}" ] && CANN_LIB="${CANN_LIB:+${CANN_LIB}:}${d}"; \
    done && \
    export LD_LIBRARY_PATH="${CANN_LIB}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" && \
    source /usr/local/Ascend/cann-9.2.1/set_env.sh && \
    source /usr/local/Ascend/nnal/atb/set_env.sh && \
    GCC_VER="$(gcc -dumpversion | cut -d. -f1)" && \
    ARCH_INCL="$(ls -d /usr/include/c++/${GCC_VER}/aarch64-*linux* 2>/dev/null | head -1 || true)" && \
    export CPLUS_INCLUDE_PATH="/usr/include/c++/${GCC_VER}${ARCH_INCL:+:${ARCH_INCL}}${CPLUS_INCLUDE_PATH:+:${CPLUS_INCLUDE_PATH}}" && \
    python3 -m pip install -v -e /vllm-workspace/vllm-ascend/ \
        --no-build-isolation \
        --extra-index-url ${PYTORCH_INDEX_URL} && \
    python3 -m pip install concurrent-log-handler && \
    pip cache purge

#================ 10. 官方仍生效的固定依赖：mooncake、mfcli kernel、flash-linear-attention ================
# mfcli 来自已安装的 memfabric-hybrid。官方 RUN 在这里调用 mfcli kernel install。
# acc_offload 由 BiSheng 编译，麒麟上要带 GCC 12 的 libstdc++ 头，否则找不到 cstdint。
COPY wheels/${FLA_WHL} /tmp/${FLA_WHL}
RUN set -e && \
    uname -m | grep -qx aarch64 && \
    source /usr/local/Ascend/cann-9.2.1/set_env.sh && \
    source /usr/local/Ascend/nnal/atb/set_env.sh && \
    wget -q --tries=3 --timeout=120 \
        "${MOONCAKE_OBS_PREFIX}/${MOONCAKE_WHL}" -P /tmp && \
    python3 -m pip install "/tmp/${MOONCAKE_WHL}" --force-reinstall --no-deps && \
    GCC_VER="$(gcc -dumpversion | cut -d. -f1)" && \
    ARCH_INCL="$(ls -d /usr/include/c++/${GCC_VER}/aarch64-*linux* 2>/dev/null | head -1 || true)" && \
    export CPLUS_INCLUDE_PATH="/usr/include/c++/${GCC_VER}${ARCH_INCL:+:${ARCH_INCL}}${CPLUS_INCLUDE_PATH:+:${CPLUS_INCLUDE_PATH}}" && \
    case "${SOC_VERSION:-}" in \
        ascend950dt_9582) TORCH_DEVICE_BACKEND_AUTOLOAD=0 mfcli kernel install --soc-version A5 ;; \
        ascend910_9391) TORCH_DEVICE_BACKEND_AUTOLOAD=0 mfcli kernel install --soc-version A3 ;; \
        ascend910b1) TORCH_DEVICE_BACKEND_AUTOLOAD=0 mfcli kernel install --soc-version A2 ;; \
    esac && \
    python3 -m pip install --force-reinstall --no-deps "/tmp/${FLA_WHL}" && \
    sudo rm -f "/tmp/${MOONCAKE_WHL}" "/tmp/${FLA_WHL}" && \
    pip cache purge

#================ 11. vLLM Rust frontend（cargo 装在 /home/${USER}/.cargo） ================
ENV RUSTUP_DIST_SERVER=${RUSTUP_DIST_SERVER} \
    RUSTUP_UPDATE_ROOT=${RUSTUP_UPDATE_ROOT}
RUN set -e && \
    TOOLCHAIN=$(sed -n 's/^channel *= *"\([^"]*\)".*/\1/p' /vllm-workspace/vllm/rust-toolchain.toml) && \
    curl -fsSL "${RUSTUP_UPDATE_ROOT}/rustup-init.sh" -o /tmp/rustup-init.sh && \
    sed -i "s/--proto '=https'//g; s/--https-only//g" /tmp/rustup-init.sh && \
    sh /tmp/rustup-init.sh -y --default-toolchain "${TOOLCHAIN:-1.95}" && \
    rm -f /tmp/rustup-init.sh
ENV PATH="/home/${USER}/.cargo/bin:${PATH}"
RUN set -e && \
    mkdir -p /home/${USER}/.cargo && \
    printf '[net]\ngit-fetch-with-cli = true\n' > /home/${USER}/.cargo/config.toml && \
    printf '[source.crates-io]\nreplace-with = "mirror"\n\n[source.mirror]\nregistry = "sparse+%s"\n' "${CRATES_IO_INDEX}" >> /home/${USER}/.cargo/config.toml
# 构建容器 nofile 软限制常为 1024，cargo 按核数并行会报 Too many open files。
RUN set -e && \
    cd /vllm-workspace/vllm && \
    ulimit -n "$(ulimit -Hn)" && \
    export PROTOC_INCLUDE=/usr/include && \
    export CARGO_BUILD_JOBS=8 && \
    python3 -m pip install setuptools-rust && \
    ./build_rust.sh && \
    pip cache purge

#================ 12. aiuser 运行时环境 ================
# 非交互判断已在创建用户时从 .bashrc 删掉，这里的 source 在 source ~/.bashrc 时会执行。
RUN { \
      echo 'export PATH=/usr/local/bin:/home/${USER}/.cargo/bin:$PATH'; \
      echo 'export LD_LIBRARY_PATH=/usr/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}'; \
      echo 'source /usr/local/Ascend/cann-9.2.1/set_env.sh'; \
      echo 'source /usr/local/Ascend/nnal/atb/set_env.sh'; \
      echo 'export LD_PRELOAD=/usr/lib64/libjemalloc.so.2${LD_PRELOAD:+:$LD_PRELOAD}'; \
      echo 'export LD_LIBRARY_PATH=/usr/local/lib:/usr/local/Ascend/driver/lib64/driver:${LD_LIBRARY_PATH}:/usr/local/lib'; \
      echo "alias ll='ls -alF'"; \
    } >> /home/${USER}/.bashrc

#================ 13. 安全整改（aiuser 执行，仅 rpm/root 目录用 sudo） ================
# 相对 9.2.0 麒麟 aiuser 镜像：ray 不再升级，改为卸载。
# ray 自带的 aiohttp 目录随卸载消失，不再对 ray/_private 做补丁。
# aiohttp / starlette / fastapi 仍按原整改版本抬升。
RUN set -ex; \
    set +u; \
    source /usr/local/Ascend/cann-9.2.1/set_env.sh; \
    source /usr/local/Ascend/nnal/atb/set_env.sh; \
    pip uninstall -y ray || true; \
    pip install --no-cache-dir --upgrade "aiohttp>=3.13.2"; \
    pip install --no-cache-dir "starlette>=1.0.1,<2.0" "fastapi[standard]>=0.133.0,<0.137.0"; \
    rm -rf /vllm-workspace/vllm/rust /home/${USER}/.cargo/registry /home/${USER}/.cargo/git; \
    sudo rm -rf /root/.cargo/registry /root/.cargo/git; \
    rm -f /vllm-workspace/vllm-ascend/csrc/third_party/ascend_protobuf/Cargo.lock; \
    sudo rpm -e --nodeps python3-libcomps libcomps || true; \
    pip uninstall -y anthropic || true; \
    python -c "import importlib.util,sys; spec=importlib.util.find_spec('ray'); assert spec is None, spec; print('ray uninstalled')"; \
    python -c "import aiohttp, starlette; print('aiohttp', aiohttp.__version__, 'starlette', starlette.__version__)"; \
    python -c "import vllm._rust_tool_parser, vllm, vllm_ascend; print('vllm ok', vllm.__version__)"; \
    test -z "$(sudo find / -name Cargo.lock -not -path '/proc/*' 2>/dev/null)"; \
    ! rpm -q python3-libcomps libcomps; \
    pip check | grep -iE "ray|aiohttp|starlette|fastapi|anthropic" || true; \
    sudo rm -rf /root/.cache/pip; \
    rm -rf /home/${USER}/.cache/pip; \
    sudo rm -rf /tmp/*

#================ 14. MindIE-Motor（最新版，GitCode） ================
# 默认 MOTOR_REF=master；生产建议用 MOTOR_COMMIT 固定 commit。
# 该阶段放在安全整改之后，避免 Motor 安装的依赖覆盖整改后的 fastapi/aiohttp 等版本；
# 若需要 ccae_reporter/moxing，请在此阶段末尾追加。
RUN set -e && \
    if [ -n "${MOTOR_COMMIT}" ]; then \
        git init /home/${USER}/MindIE-Motor && \
        git -C /home/${USER}/MindIE-Motor fetch --depth 1 "${MOTOR_REPO}" "${MOTOR_COMMIT}" && \
        git -C /home/${USER}/MindIE-Motor checkout FETCH_HEAD; \
    else \
        git clone --depth 1 --branch "${MOTOR_REF}" "${MOTOR_REPO}" /home/${USER}/MindIE-Motor; \
    fi && \
    git -C /home/${USER}/MindIE-Motor rev-parse HEAD && \
    cd /home/${USER}/MindIE-Motor && \
    sed -i '/^transformers/d' requirements.txt && \
    sed -i "s#-i https://pypi.tuna.tsinghua.edu.cn/simple#-i ${PIP_INDEX_URL}#" build.sh && \
    python3 -m pip install -r requirements.txt && \
    bash build.sh && \
    python3 -m pip install --force-reinstall ./dist/motor-*.whl && \
    mkdir -p /tmp/motor && \
    cp -a ./examples /tmp/motor/ && \
    python3 -m pip cache purge

# 基本校验：Motor 可导入，且 kv-conductor 已打进 wheel。
RUN set -e && \
    python3 -c "import motor, os; print('motor', motor.__version__); p=os.path.join(os.path.dirname(motor.__file__), 'kv_conductor', 'bin', 'kv-conductor'); print('kv-conductor', p, os.path.isfile(p)); assert os.path.isfile(p), 'kv-conductor missing from motor wheel'" && \
    python3 -m motor.kv_conductor --help >/dev/null && \
    (python3 -m pip check || true)

# 可选：CCAE 可观测组件（需要时取消注释）
# RUN set -eux; \
#     cd /home/${USER}/MindIE-Motor/examples/features/observability; \
#     python3 -m pip install -r requirements.txt; \
#     bash build.sh; \
#     python3 -m pip install --force-reinstall dist/ccae_reporter-*.whl

# 清理构建残留：Motor 源码的 .git 与编译产物 dist 不进最终镜像；
# /tmp/wheels 已在安全整改层清掉；保留 /tmp/motor/examples（5.3 节的 examples 检查依赖它）。
RUN set -e && \
    rm -rf /home/${USER}/MindIE-Motor/.git /home/${USER}/MindIE-Motor/dist /home/${USER}/.cache/pip && \
    du -sh /home/${USER}/MindIE-Motor /tmp/motor

USER ${USER}
ENV HOME=/home/${USER}
WORKDIR /workspace
CMD ["/bin/bash"]
```
