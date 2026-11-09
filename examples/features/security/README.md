# Security 特性

## 功能

`secure_h2d` 保护指定 Host 与 Device 之间的数据传输。Host 指服务器 CPU
和系统内存侧，Device 指 Ascend NPU 及其设备内存。该示例在 vLLM 的 token
H2D 路径和异步输出 D2H 路径执行加解密。

它不提供同一主机内进程之间的隔离。能访问 KMSAgent socket 的主机进程
属于信任范围，可能取得密钥。

## 使用前准备

### 宿主机

- **环境**：Linux、Docker、可用的 Ascend NPU 驱动和 DCMI，以及正在运行的 KMSAgent。
- **权限**：安装脚本需要 root 权限；示例使用 `sudo`。
- **安装动作**：脚本安装 Huawei Equipment Root CA、启动 H2D key-manage，并通过 Docker 管理容器。
- **联网要求**：首次安装需访问软件源、GitCode 和华为证书下载站点；离线环境须提前准备依赖和证书。

证书优先使用 `CERT_SRC` 指定的文件；未指定时，优先使用当前目录下的
`Huawei Equipment Root CA.pem`。

### 容器

- **运行环境**：Docker 和 Ascend NPU；不支持纯 CPU 环境或软件模拟。
- **依赖准备**：构建、安装时需要联网；离线环境按部署步骤提前准备依赖。
- **使用限制**：本示例中，每张 NPU 卡不能同时分配给多个模型，也不支持模型并行。

先按[部署步骤](./secure_h2d/README.md)创建并启动容器，设置所需权限，并挂载以下资源：

- NPU 设备。
- 驱动和 DCMI 文件。
- KMSAgent 目录：`/run/kmsagent/socket`。

默认 KMSAgent socket：`/run/kmsagent/socket/kmsagent.sock`。

### 版本与源码

| 组件 | 版本 | 依据 |
| --- | --- | --- |
| Ascend Inference HDK | `26.1.0` | 部署文档 |
| vLLM-Ascend | `26.1.0` | 部署文档 |
| `op-plugin` | `26.0.0` 分支 | `setup.sh` 首次克隆，用于构建 `torch_npu` |

MindIE Motor 首次克隆时使用仓库默认分支，不固定 commit。
下载目录已有 Git 源码时，脚本复用该 checkout，不自动更新分支。
安装前核对实际源码 commit，以及 CANN、PyTorch、`torch_npu` 和 vLLM-Ascend 的兼容性。

### 安装目录

- 脚本会删除并重建容器内的 `/workspace/op-plugin` 和 `/workspace/MindIE-Motor`；执行前先备份。
- 旧的 `/workspace/MindIE-PyMotor` 不会自动迁移；旧服务的源码路径须单独核对。

## 配置说明

按设备编号模式和算法选择环境脚本：

| 设备模式 | AES-CTR | AES-GCM |
| --- | --- | --- |
| A2 | `env_aes_ctr_a2.sh` | `env_aes_gcm_a2.sh` |
| A3 | `env_aes_ctr_a3.sh` | `env_aes_gcm_a3.sh` |

配置脚本启用 token H2D 与异步输出 D2H patch，并设置 KMS socket；
权重加载 patch 默认为关闭。AES-CTR 的 `ALG_ID` 为 `1`，
AES-GCM 为 `2`。完整参数说明见[参数说明与限制](./secure_h2d/secure_h2d_user_guide.md)。

`SECURE_PATCH_STRICT` 默认值为 `True`。设为 `False` 时，
运行期补丁异常可能回退到原实现，请求仍可能成功。
`SECURE_PATCH_DEBUG` 默认值为 `0`；此时不打印
`installed patch` 等调试日志，缺少该日志本身不代表失败。

`source` 只在当前 Bash 会话设置环境。直接从该容器会话启动的引擎
会继承这些变量。宿主机 shell 或一次性 `docker exec` 不会自动设置
已运行服务的环境；Deployer 或服务管理器须将变量配置到实际引擎进程或容器。

## 操作步骤

### 1. 在宿主机安装

**操作**：在仓库根目录的宿主机 shell 中备份准备部分提到的容器目录，
设置实际容器名，并从脚本目录运行安装。

```bash
container_name='实际运行中的容器名'
cd examples/features/security/secure_h2d &&
sudo bash setup.sh "$container_name"
```

**成功标志**：命令退出码为 `0`，并打印完成日志：

```text
[secure_h2d_deploy] all secure_h2d deployment steps finished
```

**失败处理**：非零退出或没有完成日志时，查看首个报错。脚本使用
`set -euo pipefail`，会在首个失败命令处退出；同时核对容器名、
root 权限、网络与证书。

### 2. 进入容器并加载配置

**操作**：先在宿主机 shell 设置或确认 `container_name`，运行
`docker exec` 进入容器；随后在容器内 Bash 加载所选配置。
以下示例使用 A2/AES-CTR。

```bash
docker exec -it "$container_name" bash
```

在打开的容器 Bash 中运行：

```bash
cd /workspace/MindIE-Motor &&
cd examples/features/security/secure_h2d &&
source env_aes_ctr_a2.sh &&
{
  printf 'DEVICE_MODE=%s ALG_ID=%s ENABLE=%s\n' \
    "$SECURE_PATCH_DEVICE_ID_MODE" "$SECURE_PATCH_ALG_ID" \
    "$SECURE_PATCH_ENABLE"
  printf 'H2D=%s D2H=%s WEIGHT=%s\n' \
    "$SECURE_PATCH_PATCH_VLLM_COPY_TO_GPU" \
    "$SECURE_PATCH_PATCH_ASYNC_OUTPUT_D2H" \
    "$SECURE_PATCH_PATCH_WEIGHT_LOADER"
  printf 'KMS_SOCKET=%s\n' "$SECURE_PATCH_KMS_SOCKET"
}
```

**成功标志**：已打开容器 Bash，`source` 成功，输出与预期一致：

```text
DEVICE_MODE=A2 ALG_ID=1 ENABLE=1
H2D=1 D2H=1 WEIGHT=0
KMS_SOCKET=/run/kmsagent/socket/kmsagent.sock
```

**失败处理**：`docker exec` 非零退出或未进入容器时，核对容器名和运行状态。
`cd`、`source` 失败或任一值不匹配时，先修正容器路径和配置；
不要启动引擎。

### 3. 检查容器内扩展

**操作**：在宿主机沿用前面设置的容器名运行下列命令；若更换 shell，
先重新设置 `container_name`。它检查 Python 扩展是否可导入、
`torch.ops.npu.crypto` 是否已注册；不检查实际服务是否加载配置。

```bash
docker exec -i "$container_name" python - <<'PY'
import torch
import torch_npu
import aes_ctr_crypt
import aes_gcm_crypt

assert hasattr(torch.ops.npu, "crypto")
print("扩展检查通过")
PY
```

**成功标志**：`docker exec` 退出码为 `0`，且输出：

```text
扩展检查通过
```

**失败处理**：非零退出、模块导入报错或算子断言失败时，查看首个 traceback，
核对扩展安装和算子注册。

### 4. 启动实际推理服务

**操作**：在容器内使用部署环境的引擎启动命令。直接从第 2 步加载配置的
同一 Bash 会话启动时，引擎会继承环境。通过 Deployer 或服务管理器启动时，
将变量配置到实际引擎服务或容器。

**成功标志**：实际引擎报告健康或就绪。

**失败处理**：启动命令非零退出或服务未就绪时，查看实际引擎日志和服务环境配置。
不要用宿主机 shell 或临时 `docker exec` 的环境代替服务配置。

### 5. 从客户端发送真实请求

**操作**：从能访问已部署接口的请求客户端发送真实请求，参照
[接口说明](../../../docs/zh/user_guide/api/service_interfaces.md)中的非流式接口。
使用部署实际的 HTTP(S) 地址与端口，向 `POST /v1/chat/completions` 发送请求。
`model` 字段填写实际 `served_model_name`，并按部署设置 Bearer 鉴权和 TLS。
不要猜地址或端口，也不要把密钥写入日志或命令历史。

**成功标志**：HTTP 状态为 `200`，响应包含 `choices` 中的模型回复。

**失败处理**：连接失败时核对服务地址、端口和网络；`401`/`403`
时核对部署鉴权。服务未就绪或 `5xx` 时查看引擎日志；模型名错误时核对
实际 `served_model_name`。请求成功只证明接口可用，不证明加密生效。

### 6. 确认加密链路

**操作**：在宿主机查看 KMSAgent 日志，在容器或实际服务管理器查看引擎日志；
将证据与本次请求对应。不要输出或复制密钥。

**成功标志**：现有日志或追踪能证明本次请求经过启用的 patch、H2D Host 加密与
Device 解密、D2H Device 加密与 Host 解密，并有成功的 KMS 通信证据。
本示例没有统一的加密成功日志。

**失败处理**：出现以下日志时，按异常内容检查 patch 安装、算子与 KMS 连通性；
出现错误不能记为加密通过。

```text
[sitecustomize] secure_patch install failed: <异常内容>
[secure_patch][WARN] install patch <patch_name> failed: <异常内容>
[secure_patch][WARN] <patch_name> failed: <异常内容>
```

**尚未确认**：请求成功或服务健康，但没有与本次请求对应的加密和 KMS 证据时，
记录为“加密链路尚未确认”。`SECURE_PATCH_DEBUG` 默认为 `0`，
因此没有 `installed patch` 调试日志本身不表示失败。

## 相关文档

- [secure_h2d 部署步骤](./secure_h2d/README.md)
- [环境变量与限制](./secure_h2d/secure_h2d_user_guide.md)
- [OpenAI Chat Completion 接口](../../../docs/zh/user_guide/api/service_interfaces.md#openai-chat-completion-接口)
