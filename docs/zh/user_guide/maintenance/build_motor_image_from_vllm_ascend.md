# MindIE Motor镜像安装

## 使用说明

使用 MindIE Motor 前，请先准备镜像，并**将同一镜像加载到 K8s 集群的全部节点**。

可通过以下**两种方式获取镜像**：

- 方式一：[使用 MindIE Motor 官方完整镜像](#使用mindie-motor官方完整镜像)
- 方式二：[基于 vllm-ascend/sglang 镜像安装 MindIE Motor](#基于vllm-ascendsglang镜像安装mindie-motor)

如需**升级或卸载** MindIE Motor，请参考：

- [MindIE Motor 升级](#mindie-motor升级)
- [MindIE Motor 卸载](#mindie-motor卸载)

## 使用MindIE Motor官方完整镜像

1. 进入 [昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，搜索 `motor`，按设备型号选择对应 MindIE Motor 镜像。

2. 获取镜像后，请使用以下命令将镜像加载至k8s集群的所有服务器。

     ```bash
     docker load -i xxxx.tar
     ```

3. 待镜像导入后，请使用以下命令查看docker镜像是否存在。

     ```bash
     docker images
     ```

4. 镜像准备完成，可参考[PD分离部署指导](../deployment/k8s/pd_disaggregation_deployment.md)或[PD混部部署指导](../deployment/k8s/pd_aggregation_deployment.md)部署服务。

## 基于vllm-ascend/sglang镜像安装MindIE Motor

### 依赖下载（可选）

>[!NOTE]说明
>如果制作镜像的机器可以访问互联网，请直接跳转至[获取基础镜像](#获取基础镜像以vllm-ascend为例)小节；如果制作镜像的服务器不能联网，请执行当前步骤。

在有网的环境执行如下步骤：

1. 下载pciutils。

     ```sh
     mkdir -p /mnt/pciutils-offline
     cd /mnt/pciutils-offline

     apt-get install -y apt-rdepends
     apt-get download $(apt-rdepends pciutils | grep -v "^ ")

     cd /mnt/
     tar -czvf pciutils-offline.tar.gz pciutils-offline
     ```

     将`/mnt/pciutils-offline.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下。

2. 下载whl依赖。

     下载MindIE Motor代码到`/mnt/`路径下。

     ```bash
     cd /mnt/
     git clone <motor的git链接>

     mkdir -p /mnt/packages-offline

     # 镜像已自带 transformers，下载前删除该依赖，避免版本冲突
     sed -i '/^transformers/d' /mnt/MindIE-PyMotor/requirements.txt

     pip download -r /mnt/MindIE-PyMotor/requirements.txt -d /mnt/packages-offline -i https://pypi.tuna.tsinghua.edu.cn/simple

     cd /mnt/
     tar -czvf packages-offline.tar.gz packages-offline
     ```

     将`/mnt/packages-offline.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下。

3. 构建MindIE Motor的whl包。

     ```bash
     cd /mnt/MindIE-PyMotor

     # 构建好的whl包在/mnt/MindIE-PyMotor/dist/路径下
     bash build.sh

     cd /mnt/
     tar -czvf MindIE-PyMotor.tar.gz MindIE-PyMotor
     ```

     将`/mnt/MindIE-PyMotor.tar.gz`拷贝到制作镜像机器的`/mnt/`路径下。

### 获取基础镜像，以vLLM-Ascend为例

获取方法：打开[RED HAT](https://quay.io/repository/ascend/vllm-ascend?tab=tags)，点击需要下载的版本。
以v0.13.0版本为例，下载命令为：

```bash
docker pull quay.io/ascend/vllm-ascend:v0.13.0
```

>[!NOTE]说明
>为提高下载速度，可将`quay.io`替换为`quay.nju.edu.cn`。

### 安装MindIE Motor

1. 查看镜像。

     ```bash
     docker images
     ```

2. 创建容器，并挂载mnt目录。

     ```bash
     docker run -d --name docker-vllm-ascend -v /mnt/:/mnt/ <镜像名称>
     ```

3. 启动容器。

     ```bash
     docker start docker-vllm-ascend
     ```

4. 进入容器。

     ```bash
     docker exec -it docker-vllm-ascend bash
     ```

5. 安装MindIE Motor及其依赖。

     - 安装 pciutils

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

     - 安装whl依赖

       - 在线安装：

         ```bash
         # 下载motor代码，执行以下命令，git命令根据需要下载的分支或tag进行修改
         cd /mnt/
         git clone <motor的git链接>

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

       - 离线安装：

         ```bash
         # 安装whl依赖
         cd /mnt/
         tar -xzvf packages-offline.tar.gz
         pip install /mnt/packages-offline/*.whl --force-reinstall --no-index -v

         # 安装motor
         pip install --force-reinstall /mnt/MindIE-PyMotor/dist/motor-0.1.0-py3-none-any.whl --no-index -v

         # 拷贝examples
         mkdir -p /tmp/motor/
         cp -r /mnt/MindIE-PyMotor/examples/ /tmp/motor/

         # 退出容器
         exit
         ```

6. 保存镜像。

     ```bash
     docker commit -m "add motor"  docker-vllm-ascend  mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
     ```

     保存后，`mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64`镜像就是制作好之后带MindIE Motor的镜像。

7. 打包镜像。

     ```bash
     docker save -o /mnt/motor-vllm-ascend.tar mindie-motor-vllm:dev-800I-A3-py311-lts-aarch64
     ```

8. 导入带有MindIE Motor的镜像。

     在所有k8s服务器内安装该镜像。

     ```bash
     docker load -i /mnt/motor-vllm-ascend.tar
     ```

9. 镜像准备完成，可参考[PD分离部署指导](../deployment/k8s/pd_disaggregation_deployment.md)或[PD混部部署指导](../deployment/k8s/pd_aggregation_deployment.md)部署服务。

## MindIE Motor升级

请参考[基于vllm-ascend/sglang镜像安装MindIE Motor](#基于vllm-ascendsglang镜像安装mindie-motor)小节的全部内容，重新安装MindIE Motor。在[获取基础镜像](#获取基础镜像以vllm-ascend为例)小节，将希望升级的MindIE Motor镜像作为基础镜像使用，其余步骤保持不变。

## MindIE Motor卸载

1. 基于希望卸载的MindIE Motor的镜像创建容器，并执行以下命令进入容器。

     ```bash
     docker exec -it <容器名称> bash
     ```

2. 确认当前环境已经安装了MindIE Motor。

     ```bash
     pip show motor
     ```

3. 执行卸载并退出当前容器。

     ```bash
     pip uninstall motor -y
     exit
     ```

4. 将容器提交为新镜像，新镜像已完成MindIE Motor卸载。

     ```bash
     docker commit <容器名称>  <新镜像名称>
     ```
