# 基于vllm-ascend安装MindIE Motor

先打出带 `libmindie_workload_shm.so` 的 wheel 再灌进镜像。**首次打包依赖 Rust 工具链（rustc + cargo）**：`bash build.sh` 会自动探测已有 cargo，找不到且缺 `.so` 时则联网 rustup 安装（默认国内 rsproxy 镜像），详见下文「构建依赖：Rust 工具链」。已有 `lib/*.so` / `bin/kv-conductor` 则跳过对应 cargo；缺了才编。wheel 始终必须含 workload-shm；有 cargo + libzmq 且缺 conductor 二进制时再编 kv-conductor，缺 libzmq 时自动跳过（也可显式 `SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh`）。改 `.rs` 后用 `SKIP_WORKLOAD_SHM_BUILD=0` / `SKIP_KV_CONDUCTOR_BUILD=0` 强制重编；离线用 `WORKLOAD_SHM_PREBUILT`。缺 `.so` 时 `build.sh` 拒绝出包。官方 Dockerfile 默认两个 SKIP 为 `0`，镜像内按目标 ABI 编译。

## 构建开发测试镜像

项目提供 `docker/mindie-motor-vllm/master/Dockerfile`，用于将当前工作区源码构建到 vLLM-Ascend 基础镜像中。该 Dockerfile 与发布镜像 Dockerfile 的定位不同：

| 类型 | 路径 | 源码来源 | 适用场景 |
|---|---|---|---|
| 开发镜像 | `docker/mindie-motor-vllm/master/Dockerfile` | 当前工作区 | master 分支、本地改动和 CI 验证 |
| 发布镜像 | `docker/mindie-motor-vllm/<tag>/Dockerfile` | 固定分支和 commit | 发布版本的可复现构建 |

### 本地单架构构建

在项目根目录执行：

```bash
make build-pymotor-image
```

该命令默认使用 Atlas 800I A2 推理服务器 Ubuntu 基础镜像，构建 `linux/arm64` 镜像并通过 `type=docker` 加载为 `mindie-motor-vllm:master`。Atlas 800I A3 超节点服务器 开发环境可以执行：

```bash
make build-pymotor-image \
  BASE_IMAGE=quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3 \
  PLATFORMS=linux/arm64 \
  TAG=master-a3
```

### 多架构构建和推送

同一个 `build-pymotor-image` 目标也支持多架构构建。推送到镜像仓库时，将 `OUTPUT` 设置为 `type=registry`，并提供 `REGISTRY`：

```bash
docker login example.com

make build-pymotor-image \
  REGISTRY=example.com/team \
  TAG=master \
  PLATFORMS=linux/amd64,linux/arm64 \
  OUTPUT=type=registry
```

最终镜像名为 `example.com/team/mindie-motor-vllm:master`。

> [!NOTE]说明
> `type=docker` 只能将单架构镜像加载到本地 Docker。`PLATFORMS` 包含多个平台时，应使用 `OUTPUT=type=registry` 推送多架构镜像。

### Make 变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DOCKERFILE` | `docker/mindie-motor-vllm/master/Dockerfile` | 构建使用的 Dockerfile |
| `BASE_IMAGE` | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0` | vLLM-Ascend 基础镜像 |
| `IMAGE_NAME` | `mindie-motor-vllm` | 镜像名称 |
| `TAG` | `master` | 镜像标签 |
| `REGISTRY` | 空 | 多架构推送的仓库及命名空间 |
| `PLATFORMS` | `linux/arm64` | 构建平台；多架构时使用逗号分隔 |
| `PIP_INDEX_URL` | 华为云 PyPI 镜像 | Python 软件源 |
| `PIP_TRUSTED_HOST` | `repo.huaweicloud.com` | Python 软件源信任主机 |
| `OUTPUT` | `type=docker` | buildx 输出类型；多架构推送使用 `type=registry` |

常用基础镜像如下：

| 硬件 | 操作系统 | `BASE_IMAGE` |
|---|---|---|
| Atlas 800I A2 推理服务器 | Ubuntu | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0` |
| Atlas 800I A3 超节点服务器 | Ubuntu | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3` |
| Atlas 800I A2 推理服务器 | openEuler | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-openeuler` |
| Atlas 800I A3 超节点服务器 | openEuler | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3-openeuler` |

Dockerfile 的构建过程包括：

1. 按基础系统选择 `apt-get`、`dnf` 或 `yum` 安装 `pciutils`。
2. 安装项目 Python 依赖。
3. 从当前工作区构建并安装 `motor` wheel 包。
4. 构建并安装 `ccae_reporter` 可观测组件。
5. 将示例复制到 `/tmp/motor/examples`，并生成容器入口脚本和使用协议。

> [!NOTE]说明
> 当前工作区中的未提交修改也会被复制到镜像中。开发构建不固定源码 commit；正式交付应使用 `docker/mindie-motor-vllm/<tag>/Dockerfile`。

## 手动安装和离线构建

### 构建依赖：Rust 工具链

`bash build.sh` 会把 `libmindie_workload_shm.so` 打进 wheel（**必需**，Coordinator 没有 Python 账本回退，缺库直接拒绝出包）：已有 `lib/*.so` 则跳过 cargo，缺了才编。缺 `bin/kv-conductor` 且有 cargo + libzmq 时还会顺带编 kv-conductor（可选）。官方 Dockerfile 默认 `SKIP_WORKLOAD_SHM_BUILD=0` 与 `SKIP_KV_CONDUCTOR_BUILD=0`，镜像内始终按目标 ABI 编译。因此制作镜像的容器内需要 **Rust 工具链（rustc + cargo）**，以及编译所需的 C 工具链和依赖库：

| 组件 | 用途 | 是否必需 |
|---|---|---|
| gcc / g++ / build-essential（或 `gcc gcc-c++ make`） | 链接 cdylib；kv-conductor 的 `zmq-sys` 需要 `c++` | 必需（编 kv-conductor 时必须有 g++） |
| curl 或 wget | 下载 rustup 安装脚本 | 无 cargo 时必需 |
| Rust（rustc + cargo，建议 stable） | 编译 `workload_shm_rs` 与 `kv_conductor` 两个 crate | 必需 |
| libzmq 头文件 + pkg-config（Ubuntu: `libzmq3-dev pkg-config`；openEuler: `zeromq-devel pkgconf`） | 编译可选组件 `kv-conductor` | 仅打包 kv-conductor 时必需 |

**在线安装 Rust（推荐国内 rsproxy 镜像）：**

```bash
export RUSTUP_DIST_SERVER=https://rsproxy.cn
export RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup
curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh | sh -s -- -y
source "$HOME/.cargo/env"

rustup --version && rustc --version && cargo --version
```

`build.sh` 自身也会做同样的探测/安装：先找 `PATH` / `$HOME/.cargo` / `CARGO_HOME` 下已有的 cargo（`scripts/ensure_rust.sh`），找不到才走上面的 rustup 安装（默认 rsproxy，可用 `RUSTUP_DIST_SERVER` / `RUSTUP_UPDATE_ROOT` / `RUSTUP_INIT_URL` 换其它镜像源）。因此制作镜像的容器只要能连网并装好 `curl` + C 编译器，直接执行 `bash build.sh` 即可自动装好 Rust，不必手动预装。

**离线环境（容器不能联网、必须禁止 rustup 联网下载）：**

1. 在有网的机器上编译好 `.so`（需与运行镜像同一 OS/glibc），再用 `WORKLOAD_SHM_PREBUILT=/path/to/libmindie_workload_shm.so bash build.sh` 直接拷入；打包 kv-conductor 同理可用 `KV_CONDUCTOR_PREBUILT=/path/to/kv-conductor`。
2. 或者提前把编好的 `.so` 放进 `motor/coordinator/workload_shm_rs/lib/`（`kv-conductor` 放进 `motor/kv_conductor/bin/`），执行 `SKIP_RUST_INSTALL=1 bash build.sh` 禁止联网装 rustup，`build.sh` 会直接复用已有产物。
3. 若两者都没有，`build.sh` 会在编译前 `exit 1` 并打印 `refusing to emit dist/motor-*.whl: ...`，不会打出缺库的 wheel。

缺 libzmq 时 `build.sh` 会探测后自动跳过可选的 kv-conductor（不因此失败，也不在脚本里安装 libzmq）；仍可用 `SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh` 显式跳过。workload-shm 仍会照常编译，不受影响。开关与优先级细节见仓库根 `AGENTS.md`「构建」一节。

## 依赖下载（可选）

>[!NOTE]说明
>如果制作镜像的机器不能联网，先下载依赖。

在有网的环境执行如下步骤：

### 下载pciutils

```sh
mkdir -p /mnt/pciutils-offline
cd /mnt/pciutils-offline

apt-get install -y apt-rdepends
apt-get download $(apt-rdepends pciutils | grep -v "^ ")

cd /mnt/
tar -czvf pciutils-offline.tar.gz pciutils-offline
```

将`/mnt/pciutils-offline.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下

### 下载Rust工具链（离线必需）

下一步「构建MindIE Motor的whl包」要用 `bash build.sh` 编译 `libmindie_workload_shm.so`，制作镜像的机器如果不能联网，需要提前在有网环境下载好 rustup 安装包和 Rust 工具链离线包：

```sh
mkdir -p /mnt/rust-offline
cd /mnt/rust-offline

# rustup-init 本体（按目标机架构选择，这里以 aarch64 Linux 为例）
curl --proto '=https' --tlsv1.2 -sSf -o rustup-init \
  https://rsproxy.cn/rustup-init.sh

# 用 rustup 的离线归档模式下载 stable 工具链组件到本地，供无网机器安装
RUSTUP_DIST_SERVER=https://rsproxy.cn RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup \
  rustup toolchain install stable --profile minimal

tar -czvf /mnt/rust-offline.tar.gz -C "$HOME" .cargo .rustup
```

将`/mnt/rust-offline.tar.gz`和`rustup-init`拷贝到制作镜像机器的`/mnt/`路径下。制作镜像机器上解压并放到 `$HOME` 后即可直接使用（`build.sh` 会自动探测 `$HOME/.cargo`，无需再跑一次 rustup 安装）：

```bash
tar -xzvf /mnt/rust-offline.tar.gz -C "$HOME"
source "$HOME/.cargo/env"
rustc --version && cargo --version
```

若制作镜像机器全程不具备任何编译条件，也可以直接在**另一台与运行镜像同 OS/glibc 的机器**上编出 `.so`（和可选的 `kv-conductor` 二进制），随离线包一起拷贝，构建 whl 时改用 `WORKLOAD_SHM_PREBUILT=/path/to/libmindie_workload_shm.so`（可选组件同理 `KV_CONDUCTOR_PREBUILT=/path/to/kv-conductor`），并对 `build.sh` 设置 `SKIP_RUST_INSTALL=1` 禁止联网安装 rustup。

### 下载whl依赖

下载MindIE Motor代码到`/mnt/`路径下

```bash
cd /mnt/
git clone <MindIE Motor的git链接>

mkdir -p /mnt/packages-offline

# 镜像已自带 transformers，下载前删除该依赖，避免版本冲突
sed -i '/^transformers/d' /mnt/MindIE-Motor/requirements.txt

pip download -r /mnt/MindIE-Motor/requirements.txt -d /mnt/packages-offline -i https://pypi.tuna.tsinghua.edu.cn/simple

cd /mnt/
tar -czvf packages-offline.tar.gz packages-offline
```

将`/mnt/packages-offline.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下

### 构建MindIE Motor的whl包

```bash
cd /mnt/MindIE-Motor

# 构建好的whl包在/mnt/MindIE-Motor/dist/路径下
# 请在与运行镜像相同的 OS 容器内执行（需 gcc/curl；无 cargo 时 build.sh 会自动 rustup 安装，
# 离线环境改用上一步下载好的 /mnt/rust-offline.tar.gz，或 WORKLOAD_SHM_PREBUILT 直接给预编译 .so）
# 缺 libzmq 时 build.sh 会自动跳过 kv-conductor；显式跳过仍可用：
SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh

cd /mnt/
tar -czvf MindIE-Motor.tar.gz MindIE-Motor
```

将`/mnt/MindIE-Motor.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下。

## 获取基础镜像，以vLLM-Ascend为例

>[!NOTE]说明
>为提高下载速度，可将`quay.io`替换为`quay.nju.edu.cn`。

获取方法：打开[RED HAT](https://quay.io/repository/ascend/vllm-ascend?tab=tags)，点击需要下载的版本。
以v0.13.0版本为例，下载命令为：

```bash
docker pull quay.io/ascend/vllm-ascend:v0.13.0
```

## 安装MindIE Motor

### 查看镜像

```bash
docker images
```

### 创建容器，并挂载mnt目录

```bash
docker run -d --name docker-vllm-ascend -v /mnt/:/mnt/ <镜像名称>
```

### 启动容器

```bash
docker start docker-vllm-ascend
```

### 进入容器

```bash
docker exec -it docker-vllm-ascend bash
```

### 安装MindIE Motor及其依赖

#### 安装 pciutils

- 在线安装：

```bash
apt-get update && apt-get install pciutils -y
```

- 离线安装：

```sh
cd /mnt/
tar -xzvf pciutils-offline.tar.gz
cd pciutils-offline

dpkg -i *.deb
```

#### 安装whl依赖

- 在线安装：

    ```bash
    # 下载MindIE Motor代码，执行以下命令，git命令根据需要下载的分支或tag进行修改
    cd /mnt/
    git clone <MindIE Motor的git链接>

    cd /mnt/MindIE-Motor

    # 镜像已自带 transformers，安装前删除该依赖，避免版本冲突
    sed -i '/^transformers/d' requirements.txt

    pip install -r requirements.txt

    # 有 libzmq 时 bash build.sh 会打 kv-conductor；缺 zmq 自动跳过。显式跳过：
    SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh
    pip install --force-reinstall ./dist/motor-*.whl

    mkdir -p /tmp/motor/
    cp -r ./examples/ /tmp/motor/

    # 退出容器
    exit
    ```

- 离线安装

    ```bash
    # 安装whl依赖
    cd /mnt/
    tar -xzvf packages-offline.tar.gz
    pip install /mnt/packages-offline/*.whl --force-reinstall --no-index -v

    # 安装MindIE Motor
    pip install --force-reinstall /mnt/MindIE-Motor/dist/motor-*.whl --force-reinstall --no-index -v

    # 拷贝examples
    mkdir -p /tmp/motor/
    cp -r /mnt/MindIE-Motor/examples/ /tmp/motor/

    # 退出容器
    exit
    ```

### 保存镜像

```bash
docker commit -m "add motor"  docker-vllm-ascend  mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
```

保存后，`mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64`镜像就是制作好之后带MindIE Motor的镜像。

### 打包镜像

```bash
docker save -o /mnt/motor-vllm-ascend.tar mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
```

### 导入带有MindIE Motor的镜像

在非制作镜像的节点导入镜像

```bash
docker load -i /mnt/motor-vllm-ascend.tar
```
