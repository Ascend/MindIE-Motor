# MindIE Motor 镜像使用

本文介绍 MindIE Motor 镜像的获取、制作、升级与卸载方法。可按下面的指引选择要看的章节：

1. 使用官方镜像 —— 能访问昇腾镜像仓库，直接下载官方完整镜像。

    [使用 MindIE Motor 官方完整镜像](#使用mindie-motor官方完整镜像)

2. 自行制作镜像 —— 需要基于 vllm-ascend / sglang 基础镜像自行制作。

    [自行制作 MindIE Motor 镜像](#自行制作mindie-motor镜像)

如需**升级或卸载** MindIE Motor，请参考 [MindIE Motor 升级](#mindie-motor升级) 或 [MindIE Motor 卸载](#mindie-motor卸载)。

<a id="使用mindie-motor官方完整镜像"></a>

## 使用 MindIE Motor 官方完整镜像

官方已集成 MindIE Motor，直接下载使用，无需自行制作。

1. 进入 [昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，搜索 `motor`，按设备型号选择对应 MindIE Motor 镜像。

2. 获取镜像后，请使用以下命令**将镜像加载至 K8s 集群的所有服务器**。

    ```bash
    docker load -i xxxx.tar
    ```

3. 待镜像导入后，请使用以下命令查看 Docker 镜像是否存在。

    ```bash
    docker images
    ```

4. 镜像准备完成，可参考[PD分离部署指导](../deployment/README.md#k8s-pd-disaggregation)或[PD混部部署指导](../deployment/k8s/pd_aggregation_deployment.md)部署服务。

<a id="自行制作mindie-motor镜像"></a>

## 自行制作 MindIE Motor 镜像

基于 vllm-ascend 或 sglang 基础镜像制作 MindIE Motor 镜像，提供两种制作方式：

- 方式一：使用项目提供的 **Dockerfile 构建**，适合联网环境下的日常开发和测试。

- 方式二：在**容器内手动安装**并通过 `docker commit` 保存，**按制作镜像的机器能否联网**，分为[有网环境（在线安装）](#有网环境)和[无网环境（离线安装）](#无网环境)两条路线。

### 方式一：使用 Dockerfile 构建（推荐）

1. 确认 Dockerfile 分类。

    项目中的 Dockerfile 分为两类：

    - `docker/mindie-motor-vllm/<tag>/Dockerfile`：绑定发布版本、硬件和操作系统，适合构建可复现的发布镜像。

    - `docker/mindie-motor-vllm/master/Dockerfile`：使用当前工作区源码，适合验证 master 分支或尚未提交的本地改动。

2. 构建当前平台的镜像。

    在项目根目录执行以下命令，构建当前平台的开发测试镜像并加载到本地 Docker：

    ```bash
    make build-pymotor-image
    ```

    默认生成 `mindie-motor-vllm:master`，基础镜像为 Atlas 800I A2 推理服务器 Ubuntu 版本。构建其他硬件或操作系统时，通过 `BASE_IMAGE` 指定基础镜像，不需要修改 Dockerfile：

    ```bash
    # Atlas 800I A3超节点服务器 Ubuntu
    make build-pymotor-image \
      BASE_IMAGE=quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3 \
      PLATFORMS=linux/arm64 \
      TAG=master-a3

    # Atlas 800I A3超节点服务器 openEuler
    make build-pymotor-image \
      BASE_IMAGE=quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3-openeuler \
      PLATFORMS=linux/arm64 \
      TAG=master-a3-openeuler
    ```

    基础镜像的常用选择如下：

    | 硬件 | 操作系统 | `BASE_IMAGE` |
    | --- | --- | --- |
    | Atlas 800I A2推理服务器 | Ubuntu | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0` |
    | Atlas 800I A3超节点服务器 | Ubuntu | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3` |
    | Atlas 800I A2推理服务器 | openEuler | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-openeuler` |
    | Atlas 800I A3超节点服务器 | openEuler | `quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-a3-openeuler` |

3. 构建多架构镜像并推送。

    需要构建多架构镜像并推送到镜像仓库时，先登录仓库，再执行：

    ```bash
    docker login example.com

    make build-pymotor-image \
      REGISTRY=example.com/team \
      TAG=master \
      PLATFORMS=linux/amd64,linux/arm64 \
      OUTPUT=type=registry
    ```

<a id="方式二手动安装"></a>

### 方式二：在容器内手动安装

在基础镜像的容器内安装 MindIE Motor，再用 `docker commit` 保存为新镜像。

本方式分为两条独立路线，**按制作镜像的机器能否联网选择其中一条**：

- [有网环境（在线安装）](#有网环境)：容器内可直接访问互联网和 Python 软件源。

- [无网环境（离线安装）](#无网环境)：先在有网机器上准备离线包，再拷贝到制作镜像的机器。

<a id="有网环境"></a>

#### 有网环境（在线安装）

1. <a id="获取基础镜像以vllm-ascend为例"></a>获取基础镜像。

    打开 [RED HAT](https://quay.io/repository/ascend/vllm-ascend?tab=tags)，点击需要下载的版本。以 v0.13.0 版本为例，下载命令为：

    ```bash
    docker pull quay.io/ascend/vllm-ascend:v0.13.0
    ```

    **注：为提高下载速度，可将 `quay.io` 替换为 `quay.nju.edu.cn`。**

2. 创建并进入容器。

    查看镜像。

    ```bash
    docker images
    ```

    创建容器，并挂载 mnt 目录，`docker-vllm-ascend`为你自己创建的容器的名字。

    ```bash
    docker run -d --name docker-vllm-ascend -v /mnt/:/mnt/ <镜像名称>
    ```

    启动容器。

    ```bash
    docker start docker-vllm-ascend
    ```

    进入容器。

    ```bash
    docker exec -it docker-vllm-ascend bash
    ```

3. 安装 MindIE Motor 及其依赖。

    - 安装 pciutils

        ```bash
        apt-get update && apt-get install pciutils -y
        ```

    - 安装 whl 依赖

        ```bash
        # 下载motor代码，执行以下命令，git命令根据需要下载的分支或tag进行修改
        cd /mnt/
        git clone <motor的git链接>

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

    安装完成后，可执行以下命令确认 MindIE Motor 已安装：

    ```bash
    pip show motor
    ```

4. 保存、打包并导入镜像。

    保存镜像。

    ```bash
    docker commit -m "add motor"  docker-vllm-ascend  mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
    ```

    保存后，`mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64` 镜像就是制作好之后带 MindIE Motor 的镜像。

    打包镜像。

    ```bash
    docker save -o /mnt/motor-vllm-ascend.tar mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
    ```

    导入带有 MindIE Motor 的镜像，在所有 K8s 服务器内安装该镜像。

    ```bash
    docker load -i /mnt/motor-vllm-ascend.tar
    ```

    镜像准备完成，可参考[PD分离部署指导](../deployment/README.md#k8s-pd-disaggregation)或[PD混部部署指导](../deployment/k8s/pd_aggregation_deployment.md)部署服务。

<a id="无网环境"></a>

#### 无网环境（离线安装）

制作镜像的机器不能联网，需要先在一台有网机器上准备离线包，再拷贝到制作镜像的机器。

1. 准备离线包（在有网机器上执行）。

    - 下载 pciutils。

        ```sh
        mkdir -p /mnt/pciutils-offline
        cd /mnt/pciutils-offline

        apt-get install -y apt-rdepends
        apt-get download $(apt-rdepends pciutils | grep -v "^ ")

        cd /mnt/
        tar -czvf pciutils-offline.tar.gz pciutils-offline
        ```

        将 `/mnt/pciutils-offline.tar.gz` 拷贝到制作镜像机器的 `/mnt/` 路径下。

    - 下载 whl 依赖。

        将 MindIE Motor 代码下载到 `/mnt/` 路径下。

        ```bash
        cd /mnt/
        git clone <motor的git链接>

        mkdir -p /mnt/packages-offline

        # 镜像已自带 transformers，下载前删除该依赖，避免版本冲突
        sed -i '/^transformers/d' /mnt/MindIE-Motor/requirements.txt

        pip download -r /mnt/MindIE-Motor/requirements.txt -d /mnt/packages-offline -i https://pypi.tuna.tsinghua.edu.cn/simple

        cd /mnt/
        tar -czvf packages-offline.tar.gz packages-offline
        ```

        将 `/mnt/packages-offline.tar.gz` 拷贝到制作镜像机器的 `/mnt/` 路径下。

    - 构建 MindIE Motor 的 whl 包。

        ```bash
        cd /mnt/MindIE-Motor

        # 构建好的whl包在/mnt/MindIE-Motor/dist/路径下
        # 请在与运行镜像相同的 OS 容器内执行（需 gcc/curl）
        # 缺 libzmq 时 build.sh 会自动跳过 kv-conductor；显式跳过仍可用：
        SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh

        cd /mnt/
        tar -czvf MindIE-Motor.tar.gz MindIE-Motor
        ```

        将 `/mnt/MindIE-Motor.tar.gz` 拷贝到制作镜像机器的 `/mnt/` 路径下。

2. 获取基础镜像（导出后拷贝）。

    在有网机器上执行以下命令，下载基础镜像并导出为 tar 包：

    ```bash
    docker pull quay.io/ascend/vllm-ascend:v0.13.0

    docker save -o /mnt/vllm-ascend-v0.13.0.tar quay.io/ascend/vllm-ascend:v0.13.0
    ```

    将 `/mnt/vllm-ascend-v0.13.0.tar` 拷贝到制作镜像机器的 `/mnt/` 路径下，并导入：

    ```bash
    docker load -i /mnt/vllm-ascend-v0.13.0.tar
    ```

3. 创建并进入容器。

    查看镜像。

    ```bash
    docker images
    ```

    创建容器，并挂载 mnt 目录。

    ```bash
    docker run -d --name docker-vllm-ascend -v /mnt/:/mnt/ <镜像名称>
    ```

    启动容器。

    ```bash
    docker start docker-vllm-ascend
    ```

    进入容器。

    ```bash
    docker exec -it docker-vllm-ascend bash
    ```

4. 安装 MindIE Motor 及其依赖。

    - 安装 pciutils

        ```sh
        cd /mnt/
        tar -xzvf pciutils-offline.tar.gz
        cd pciutils-offline

        dpkg -i *.deb
        ```

    - 安装 whl 依赖

        ```bash
        # 安装whl依赖
        cd /mnt/
        tar -xzvf packages-offline.tar.gz
        pip install /mnt/packages-offline/*.whl --force-reinstall --no-index -v

        # 安装motor
        pip install --force-reinstall /mnt/MindIE-Motor/dist/motor-*.whl --no-index -v

        # 拷贝examples
        mkdir -p /tmp/motor/
        cp -r /mnt/MindIE-Motor/examples/ /tmp/motor/

        # 退出容器
        exit
        ```

    安装完成后，可执行以下命令确认 MindIE Motor 已安装：

    ```bash
    pip show motor
    ```

5. 保存、打包并导入镜像。

    保存镜像。

    ```bash
    docker commit -m "add motor"  docker-vllm-ascend  mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
    ```

    保存后，`mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64` 镜像就是制作好之后带 MindIE Motor 的镜像。

    打包镜像。

    ```bash
    docker save -o /mnt/motor-vllm-ascend.tar mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
    ```

    导入带有 MindIE Motor 的镜像，在所有 K8s 服务器内安装该镜像。

    ```bash
    docker load -i /mnt/motor-vllm-ascend.tar
    ```

    镜像准备完成，可参考[PD分离部署指导](../deployment/README.md#k8s-pd-disaggregation)或[PD混部部署指导](../deployment/k8s/pd_aggregation_deployment.md)部署服务。

<a id="mindie-motor升级"></a>

## MindIE Motor 升级

请参考[方式二：在容器内手动安装](#方式二手动安装)小节的全部内容，重新安装 MindIE Motor。在获取基础镜像的步骤中，将希望升级的 MindIE Motor 镜像作为基础镜像使用，其余步骤保持不变。

<a id="mindie-motor卸载"></a>

## MindIE Motor 卸载

1. 基于希望卸载的 MindIE Motor 的镜像创建容器，并执行以下命令进入容器。

    ```bash
    docker exec -it <容器名称> bash
    ```

2. 确认当前环境已经安装了 MindIE Motor。

    ```bash
    pip show motor
    ```

3. 执行卸载并退出当前容器。

    ```bash
    pip uninstall motor -y
    exit
    ```

4. 将容器提交为新镜像，新镜像已完成 MindIE Motor 卸载。

    ```bash
    docker commit <容器名称>  <新镜像名称>
    ```
    