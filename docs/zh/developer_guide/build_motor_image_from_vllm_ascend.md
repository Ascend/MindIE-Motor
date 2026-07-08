# 镜像制作

## 获取vLLM-Ascend发布的镜像版本

获取方法：打开[RED HAT](https://quay.io/repository/ascend/vllm-ascend?tab=tags)，点击需要下载的版本。
以v0.13.0版本为例，下载命令为：

```bash
docker pull quay.io/ascend/vllm-ascend:v0.13.0
```

>[!NOTE]说明
>为提高下载速度，可将`quay.io`替换为`quay.nju.edu.cn`。

## 在镜像中安装PyMotor

>[!NOTE]说明
>如果制作镜像的机器不能联网，需要在步骤4中下载依赖。

1. 准备好目标motor代码，执行以下命令，git命令根据需要下载的分支或tag进行修改。

    ```bash
    cd /mnt/
    git clone <motor的git链接>
    ```

2. 执行以下命令查看第一步下载下来的镜像。

    ```bash
    docker images
    ```

3. 执行以下命令运行容器并挂载mnt目录。

    ```bash
    docker run -d --name docker-vllm-ascend -v /mnt/:/mnt/ <镜像名称>
    ```

4. 依赖下载，**如果制作镜像的机器能联网，可在线安装，忽略此步骤**

    1. 下载`pciutils`

        ```sh
        mkdir -p /mnt/pciutils-offline
        cd /mnt/pciutils-offline

        apt-get install -y apt-rdepends
        apt-get download $(apt-rdepends pciutils | grep -v "^ ")

        cd /mnt/
        tar -czvf pciutils-offline.tar.gz pciutils-offline
        ```

        将`/mnt/pciutils-offline.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下

    2. 下载`whl`依赖

        ```sh
        mkdir -p /mnt/packages-offline
        pip download -r MindIE-PyMotor/requirements.txt -d /mnt/packages-offline -i https://pypi.tuna.tsinghua.edu.cn/simple

        cd /mnt/
        tar -czvf packages-offline.tar.gz packages-offline
        ```

        将`/mnt/packages-offline.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下

5. 执行以下命令制作镜像。

    **进入容器**

    ```bash
    docker exec -it docker-vllm-ascend bash
    ```

    **安装 `pciutils`**

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

    **安装whl依赖**

    - 在线安装：

        ```bash
        cd /mnt/MindIE-PyMotor
        pip install -r requirements.txt
        bash build.sh
        pip install --force-reinstall ./dist/motor-0.1.0-py3-none-any.whl

        mkdir -p /tmp/motor/
        cp -r ./examples/ /tmp/motor/

        exit
        ```

    - 离线安装

        ```bash
        cd /mnt/
        tar -xzvf packages-offline.tar.gz

        cd /mnt/MindIE-PyMotor
        pip install --no-index --find-links=/mnt/packages-offline -r requirements.txt
        bash build.sh
        pip install --force-reinstall ./dist/motor-0.1.0-py3-none-any.whl

        mkdir -p /tmp/motor/
        cp -r ./examples/ /tmp/motor/

        exit
        ```

6. 执行以下命令保存镜像。

    ```bash
    docker commit -m "add motor"  docker-vllm-ascend  mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
    ```

    保存后，`mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64`镜像就是制作好之后带motor的镜像。

7. 打包镜像

    ```bash
    docker save -o /mnt/motor-vllm-ascend.tar mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
    ```

8. 导入带有motor的镜像

    在非制作镜像的节点导入镜像

    ```bash
    docker load -i /mnt/motor-vllm-ascend.tar
    ```
