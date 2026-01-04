# 安装说明

本文主要向用户介绍如何快速完成MindIE（Mind Inference Engine，昇腾推理引擎）软件的安装。

## 安装方案

**本文档包含镜像/容器、物理机场景下，安装MindIE软件的方案**部署。

- 镜像安装：该方式是最简单的一种安装方式，用户直接从昇腾社区下载已经打包好的镜像，镜像中已经包含了CANN、PTA、MindIE等必要的依赖与软件，用户只需拉取镜像并启动容器即可。
- 物理机安装：该方式是在不使用Docker容器的情况下，将CANN、PTA、MindIE等软件逐个手动安装到物理机上。这种方式将所有软件直接安装到物理机的操作系统中。
- 容器安装：该方式是将CANN、PTA、MindIE等软件逐个安装到容器中，相当于手动创建镜像。这种方式为用户提供了更高的灵活性，用户可以自由选择和指定软件版本，同时每个容器中的软件环境都是独立的。



## 硬件配套和支持的操作系统

本章节提供软件包支持的操作系统清单，请执行以下命令查询当前操作系统，如果查询的操作系统版本不在对应产品列表中，请替换为支持的操作系统。

```
uname -m && cat /etc/*release
```

表1 操作系统支持列表

| 硬件                                                    | 操作系统                                                     |
| ------------------------------------------------------- | ------------------------------------------------------------ |
| Atlas 800I A2 推理服务器                                | AArch64：CentOS 7.6CTYunOS 23.01CULinux 3.0Kylin V10 GFBKylin V10 SP2Kylin V10 SP3Ubuntu 22.04AliOS3BCLinux 21.10 U4Ubuntu 24.04 LTSopenEuler 22.03 LTSopenEuler 24.03 LTS SP1openEuler 22.03 LTS SP4 |
| Atlas 300I Duo 推理卡+Atlas 800 推理服务器（型号 3000） | AArch64：BCLinux 21.10Debian 10.8Kylin V10 SP1Ubuntu 20.04Ubuntu 22.04UOS20-1020eopenEuler 24.03 SP1openEuler 22.03 LTS SP4 |
| Atlas 300I Duo 推理卡+Atlas 800 推理服务器（型号 3010） | X86_64：Ubuntu 22.04                                         |
| Atlas 800I A3 超节点服务器                              | AArch64：openEuler 22.03CULinux 3.0Kylin V10 SP3 2403        |

# 镜像安装与使用

1. 前置条件：

   下载安装HDK（[点此下载](https://www.hiascend.com/hardware/firmware-drivers/community?product=1&model=30&cann=8.3.RC2&driver=Ascend+HDK+25.3.RC1)），安装指令：

   ```
   # 卸载
   bash /usr/local/Ascend/driver/script/uninstall.sh --force 
   bash /usr/local/Ascend/firmware/script/uninstall.sh 
   rm -rf /usr/local/Ascend/driver./ 
   rm -rf /usr/local/Ascend/firmware 
   
   # 安装
   ./driver_filename.run --full 
   ./firmware_filename.run --full
   ```

   下载安装docker app（版本要求大于等于24.x.x）：

   ```
   # 下载安装
   curl -fsSL https://get.docker.com | sudo sh
   
   # 启动 Docker
   sudo systemctl start docker
   
   # 设置开机自启
   sudo systemctl enable docker
   
   # 测试是否安装成功
   sudo docker run hello-world
   
   # 查看docker版本
   docker --version
   
   # 升级到最新版本
   sudo apt update
   sudo apt upgrade docker-ce docker-ce-cli containerd.io
   ```

2. 下载镜像：请从昇腾官网中下载镜像（[点此下载](https://www.hiascend.com/developer/ascendhub/detail/af85b724a7e5469ebd7ea13c3439d48f)），点击镜像版本栏目进行下载。请根据机器型号（A2、A3、DUO）、操作系统版本（ubuntu、openeuler）选择镜像进行下载。如需体验新特性、当前最优精度性能，请尽量选择最新版本。

3. 安装镜像：

   安装镜像前，请确保已经配置HDK、docker 应用等环境

   安装指令：

   ```
   docker load <image_file_name
   ```

   查看指令：

   ```
   docker load <image_file_name
   ```

4. 创建容器脚本：

   ```
   if [ $#  -ne 1 ]; then
        echo "error: need one argument describing your container name."
        echo "usage: $0 [arg], arg in the format of acltransformer_[your model name]_[your name]."
        exit 1
   fi
   docker run --name $1 -it -d --net=host --shm-size=500g \
        --privileged=true \
        -w /home \
        --device=/dev/davinci_manager \
        --device=/dev/hisi_hdc \
        --device=/dev/devmm_svm \
        --entrypoint=bash \
        -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
        -v /usr/local/dcmi:/usr/local/dcmi \
        -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
        -v /usr/local/sbin/:/usr/local/sbin \
        -v /home:/home \
        -v /tmp:/tmp \
        -v /data:/data \
        -v /mnt:/mnt \
        -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
        mindie:mindie_image_file_name
   ```

   创建容器指令（假如创建容器脚本文件名为start_docker.sh）：

   ```
   bash start_docker.sh your_docker_name
   ```

5. 使用容器：

   查看容器：

   ```
   # 查看所有开启容器
   docker ps
   # 查看所有容器
   docker ps -a
   ```

   打开容器指令：

   ```
   docker start your_docker_name
   ```

   进入容器指令：

   ```
   docker exec -it your_docker_name bash
   ```

6. 卸载环境

   卸载容器

   ```
   #先关闭容器
   docker stop your_docker_name
   #删除容器
   docker rm docker_name
   ```

   卸载镜像

   ```
   docker rmi image_id
   ```


# 源码编译与安装

1. 创建并进入mindie镜像对应的容器后编译代码，镜像容器参考MindIE-Motor/docs/zh/User_Guide/image installation and usage.md

2. 编译代码需要执行下列环境变量：

   ```
   export NO_CHECK_CERTIFICATE=1
   unset TUNE_BANK_PATH
   ```

   拉取编译代码可能需要配置proxy

3. 拉取代码

   ```
   USERNAME="your_gitcode_id"
   PASSWORD="your_gitcode_passwd"
   git clone https://${USERNAME}:${PASSWORD}@gitcode.com/Ascend/MindIE-Motor.git
   cd MindIE-Motor/
   mkdir modules
   cd modules/
   git clone https://${USERNAME}:${PASSWORD}@gitcode.com/Ascend/MindIE-LLM.git
   cd ..
   ```

4. 执行编译：

   一键编译所有组件指令：

   ```
   bash MindIE-Motor/build/build.sh -a
   ```

   该指令等价于：

   ```
   bash build/build.sh -d 3rd -b 3rd llm service -p
   ```

   首次编译需要下载并编译第三方，后续无需此操作，单独下载编译第三方指令为：

   ```
   bash build/build.sh -d 3rd -b 3rd
   ```

   - -d为下载，可下载的组件为3rd
   - -b为编译可编译的组件为llm、service、server、ms
     - 其中llm对应MindIE-LLM仓
     - service对应MindIE-Motor仓
     - server对应MindIE-LLM仓的server模块
     - ms对应MindIE-Motor仓的ms（management service）模块
   - -p为打包，将MindIE-LLM的包和MindIE-Motor的包打包成MindIE包

5. 编译成功会得到三个包

   - MindIE-LLM仓编译出的包：

     ```
     /your_code_path/MindIE-Motor/output/modules/Ascend-mindie-llm_1.0.RC3_py311_linux-aarch64.run
     ```

   - MindIE-Motor仓编译出的包：

     ```
     /your_code_path/MindIE-Motor/output/modules/Ascend-mindie-service_1.0.0_py311_linux-aarch64.run
     ```

   - 两个仓打包成的mindie包：

     ```
     /your_code_path/MindIE-Motor/output/aarch64/Ascend-mindie_1.0.0_linux-aarch64.run
     ```

   - 打印对应包的hash值：

     ```
     md5sum /code_path/MindIE-code/MindIE-Motor/output/aarch64/Ascend-mindie_1.0.0_linux-aarch64.run
     ```

6. 在PD分离拉起服务脚本中的boot.sh中，在#!/bin/bash和set_common_env之间加入下列代码，安装编译得到的mindie包：

```
#!/bin/bash
unset TUNE_BANK_PATH
bash_path=/your_code_path/1209/MindIE-Motor/output/aarch64
mindie_run=Ascend-mindie_1.0.0_linux-aarch64.run
cd ${bash_path} && chmod +x ./${mindie_run} && echo "y" | ./${mindie_run} --install
md5sum  ${bash_path}/${mindie_run} 
unset http_proxy https_proxy
set_common_env
```

k8s集群在拉起服务时，会创建各个节点对应的容器，每个容器创建后，均会执行boot.sh脚步，即每个容器都会安装你编译的mindie包