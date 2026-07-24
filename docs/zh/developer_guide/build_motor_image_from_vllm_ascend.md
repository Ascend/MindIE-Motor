# 基于vllm-ascend/sglang镜像安装MindIE Motor

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

该命令默认使用 A2 Ubuntu 基础镜像，构建 `linux/arm64` 镜像并通过 `type=docker` 加载为 `mindie-motor-vllm:master`。A3 开发环境可以执行：

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
| A2 | Ubuntu | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0` |
| A3 | Ubuntu | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3` |
| A2 | openEuler | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-openeuler` |
| A3 | openEuler | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3-openeuler` |

Dockerfile 的构建过程包括：

1. 按基础系统选择 `apt-get`、`dnf` 或 `yum` 安装 `pciutils`。
2. 安装项目 Python 依赖。
3. 从当前工作区构建并安装 `motor` wheel 包。
4. 构建并安装 `ccae_reporter` 可观测组件。
5. 将示例复制到 `/tmp/motor/examples`，并生成容器入口脚本和使用协议。

> [!NOTE]说明
> 当前工作区中的未提交修改也会被复制到镜像中。开发构建不固定源码 commit；正式交付应使用 `docker/mindie-motor-vllm/<tag>/Dockerfile`。

## 手动安装和离线构建

## 依赖下载（可选）

>[!NOTE]说明
>如果制作镜像的机器不能联网，先下载依赖。

在有网的环境执行如下步骤：

### 1. 下载pciutils

```sh
mkdir -p /mnt/pciutils-offline
cd /mnt/pciutils-offline

apt-get install -y apt-rdepends
apt-get download $(apt-rdepends pciutils | grep -v "^ ")

cd /mnt/
tar -czvf pciutils-offline.tar.gz pciutils-offline
```

将`/mnt/pciutils-offline.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下

### 2. 下载whl依赖

下载MindIE Motor代码到`/mnt/`路径下

```bash
cd /mnt/
git clone <MindIE Motor的git链接>

mkdir -p /mnt/packages-offline

# 镜像已自带 transformers，下载前删除该依赖，避免版本冲突
sed -i '/^transformers/d' /mnt/MindIE-PyMotor/requirements.txt

pip download -r /mnt/MindIE-PyMotor/requirements.txt -d /mnt/packages-offline -i https://pypi.tuna.tsinghua.edu.cn/simple

cd /mnt/
tar -czvf packages-offline.tar.gz packages-offline
```

将`/mnt/packages-offline.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下

### 3. 构建MindIE Motor的whl包

```bash
cd /mnt/MindIE-PyMotor

# 构建好的whl包在/mnt/MindIE-PyMotor/dist/路径下
bash build.sh

cd /mnt/
tar -czvf MindIE-PyMotor.tar.gz MindIE-PyMotor
```

将`/mnt/MindIE-PyMotor.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下

## 获取基础镜像，以vLLM-Ascend为例

>[!NOTE]说明
>为提高下载速度，可将`quay.io`替换为`quay.nju.edu.cn`。

获取方法：打开[RED HAT](https://quay.io/repository/ascend/vllm-ascend?tab=tags)，点击需要下载的版本。
以v0.13.0版本为例，下载命令为：

```bash
docker pull quay.io/ascend/vllm-ascend:v0.13.0
```

## 安装MindIE Motor

### 1. 查看镜像

```bash
docker images
```

### 2. 创建容器，并挂载mnt目录

```bash
docker run -d --name docker-vllm-ascend -v /mnt/:/mnt/ <镜像名称>
```

### 3. 启动容器

```bash
docker start docker-vllm-ascend
```

### 4. 进入容器

```bash
docker exec -it docker-vllm-ascend bash
```

### 5. 安装MindIE Motor及其依赖

#### 5.1 安装 pciutils

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

#### 5.2 安装whl依赖

- 在线安装：

    ```bash
    # 下载MindIE Motor代码，执行以下命令，git命令根据需要下载的分支或tag进行修改
    cd /mnt/
    git clone <MindIE Motor的git链接>

    cd /mnt/MindIE-PyMotor

    # 镜像已自带 transformers，安装前删除该依赖，避免版本冲突
    sed -i '/^transformers/d' requirements.txt

    pip install -r requirements.txt

    bash build.sh
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
    pip install --force-reinstall /mnt/MindIE-PyMotor/dist/motor-*.whl --force-reinstall --no-index -v

    # 拷贝examples
    mkdir -p /tmp/motor/
    cp -r /mnt/MindIE-PyMotor/examples/ /tmp/motor/

    # 退出容器
    exit
    ```

### 6. 保存镜像

```bash
docker commit -m "add motor"  docker-vllm-ascend  mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
```

保存后，`mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64`镜像就是制作好之后带MindIE Motor的镜像。

### 7. 打包镜像

```bash
docker save -o /mnt/motor-vllm-ascend.tar mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
```

### 8. 导入带有MindIE Motor的镜像

在非制作镜像的节点导入镜像

```bash
docker load -i /mnt/motor-vllm-ascend.tar
```
