# 基于vllm-ascend/sglang镜像安装MindIE Motor

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
    pip install --force-reinstall ./dist/motor-0.1.0-py3-none-any.whl

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
    pip install --force-reinstall /mnt/MindIE-PyMotor/dist/motor-0.1.0-py3-none-any.whl --force-reinstall --no-index -v

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
