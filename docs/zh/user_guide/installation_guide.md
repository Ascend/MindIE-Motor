# 1 安装说明

本文主要向用户介绍如何快速完成MindIE（Mind Inference Engine，昇腾推理引擎）软件的安装。

## 1.1 安装方案

**主要包含镜像场景下安装方式，安装MindIE软件的方案部署。**

镜像安装：该方式是最简单的一种安装方式，用户直接从昇腾社区下载已经打包好的镜像，镜像中已经包含了CANN、PTA、MindIE等必要的依赖与软件，用户只需拉取镜像并启动容器即可。

建议使用镜像安装方式，本文仅介绍镜像安装相关内容

## 1.2 硬件配套和支持的操作系统

本章节提供软件包支持的操作系统清单，请执行以下命令查询当前操作系统，如果查询的操作系统版本不在对应产品列表中，请替换为支持的操作系统。

```
uname -m && cat /etc/*release
```

表1 操作系统支持列表

| 硬件                                                    | 操作系统                                                     |
| ------------------------------------------------------- | ------------------------------------------------------------ |
| Atlas 800I A2 推理服务器                                | AArch64：<br>CentOS 7.6<br/>CTYunOS 23.01<br/>CULinux 3.0<br/>Kylin V10 GFB<br/>Kylin V10 SP2<br/>Kylin V10 SP3<br/>Ubuntu 22.04<br/>AliOS3<br/>BCLinux 21.10 U4<br/>Ubuntu 24.04 LTS<br/>openEuler 22.03 LTS<br/>openEuler 24.03 LTS SP1<br/>openEuler 22.03 LTS SP4 |
| Atlas 300I Duo 推理卡+Atlas 800 推理服务器（型号 3000） | AArch64：<br/>BCLinux 21.10<br/>Debian 10.8<br/>Kylin V10 SP1<br/>Ubuntu 20.04<br/>Ubuntu 22.04<br/>UOS20-1020<br/>eopenEuler 24.03 SP1<br/>openEuler 22.03 LTS SP4 |
| Atlas 300I Duo 推理卡+Atlas 800 推理服务器（型号 3010） | X86_64：<br/>Ubuntu 22.04                                    |
| Atlas 800I A3 超节点服务器                              | AArch64：<br/>openEuler 22.03<br/>CULinux 3.0<br/>Kylin V10 SP3 2403 |

# 2 docker app安装与使用

1. 确认系统

   ```
   cat /etc/os-release
   ```

   ubuntu系统显示结果：
   ```
   PRETTY_NAME="Ubuntu 22.04 LTS"
   NAME="Ubuntu"
   VERSION_ID="22.04"
   VERSION="22.04 (Jammy Jellyfish)"
   VERSION_CODENAME=jammy
   ID=ubuntu
   ID_LIKE=debian
   HOME_URL="https://www.ubuntu.com/"
   SUPPORT_URL="https://help.ubuntu.com/"
   BUG_REPORT_URL="https://bugs.launchpad.net/ubuntu/"
   PRIVACY_POLICY_URL="https://www.ubuntu.com/legal/terms-and-policies/privacy-policy"
   UBUNTU_CODENAME=jammy
   ```

   关注**NAME**、**ID**等参数，确认是否为Ubuntu系统
2. ubuntu系统安装docker app：

   - 切换可用源：

   ```
   sudo mv /etc/apt/sources.list.d/kubernetes.list /etc/apt/sources.list.d/kubernetes.list.disabled && sudo apt update
   ```

   - 成功显示：

   ```
   Get:1 http://mirrors.tools.huawei.com/ubuntu-ports jammy InRelease [270 kB]
   Hit:2 http://mirrors.tools.huawei.com/ubuntu-ports jammy-updates InRelease
   Hit:3 http://mirrors.tools.huawei.com/ubuntu-ports jammy-backports InRelease
   Fetched 270 kB in 0s (560 kB/s)
   Reading package lists... Done
   Building dependency tree... Done
   Reading state information... Done
   381 packages can be upgraded. Run 'apt list --upgradable' to see them.
   ```

   - 安装docker：

   ```
   sudo apt install docker.io -y
   ```

   - 安装成功显示结果：

   ```
   Reading package lists... Done
   Building dependency tree... Done
   Reading state information... Done
   The following package was automatically installed and is no longer required:
     libjs-highlight.js
   Use 'sudo apt autoremove' to remove it.
   Suggested packages:
     aufs-tools cgroupfs-mount | cgroup-lite debootstrap docker-buildx docker-compose-v2 docker-doc rinse zfs-fuse | zfsutils
   The following packages will be upgraded:
     docker.io
   1 upgraded, 0 newly installed, 0 to remove and 380 not upgraded.
   Need to get 25.6 MB of archives.
   After this operation, 6,515 kB of additional disk space will be used.
   Get:1 http://mirrors.tools.huawei.com/ubuntu-ports jammy-updates/universe arm64 docker.io arm64 28.2.2-0ubuntu1~22.04.1 [25.6 MB]
   Fetched 25.6 MB in 0s (57.3 MB/s)
   Preconfiguring packages ...
   (Reading database ... 166464 files and directories currently installed.)
   Preparing to unpack .../docker.io_28.2.2-0ubuntu1~22.04.1_arm64.deb ...
   Unpacking docker.io (28.2.2-0ubuntu1~22.04.1) over (26.1.3-0ubuntu1~22.04.1) ...
   Setting up docker.io (28.2.2-0ubuntu1~22.04.1) ...
   Warning: The unit file, source configuration file or drop-ins of docker.service changed on disk. Run 'systemctl daemon-reload' to reload units.
   Processing triggers for man-db (2.10.2-1) ...
   Scanning processes...
   Scanning processor microcode...
   Scanning linux images...
   
   Running kernel seems to be up-to-date.
   
   Failed to check for processor microcode upgrades.
   
   No services need to be restarted.
   
   No containers need to be restarted.
   
   No user sessions are running outdated binaries.
   
   No VM guests are running outdated hypervisor (qemu) binaries on this host.
   ```

   - 查看升级docker app版本

   ```
   # 查看docker版本
   docker --version
   
   # 升级到最新版本
   sudo apt update
   sudo apt upgrade docker-ce docker-ce-cli containerd.io
   ```

# 3  下载安装HDK

1. 下载HDK（[点此下载](https://www.hiascend.com/hardware/firmware-drivers/community?product=1&model=30&cann=8.3.RC2&driver=Ascend+HDK+25.3.RC1)），点开链接后：

   - 在产品系列中，300I系列选择加速卡，A2与A3系列选择服务器；
   - 然后选择对应的机器型号；在下面的软件包格式中选择run；
   - 请选择与MindIE镜像发布日期最接近的HDK版本
   - （说明：大部分情况是不需要更换HDK的，但是如果MindIE镜像版本时间和HDK差距过大，可能会出现运行失败、模型精度性能不佳等情况）

2. 安装HDK：

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

3. 查看是否安装成功：

   ```
   npu-smi info
   ```

# 4 镜像安装与使用

1. 下载镜像：请从昇腾官网中下载镜像（[点此下载](https://www.hiascend.com/developer/ascendhub/detail/af85b724a7e5469ebd7ea13c3439d48f)），点击镜像版本栏目进行下载。请根据机器型号、操作系统版本（Ubuntu、openEuler）选择镜像进行下载，其中机器型号包括Atlas 800I A2 推理服务器、Atlas 800I A3 超节点服务器、Atlas 300I DUO推理卡。如需体验新特性、当前最优精度性能，请尽量选择最新版本。

   注意，请点击此处下载镜像：

   ![img](https://raw.gitcode.com/user-images/assets/8772838/244fd6e6-2b68-4932-994c-0a89116858cf/image.png)

2. 安装镜像：

   安装镜像前，请确保已经配置HDK、docker 应用等环境

   安装指令：

   ```
   docker load < image_file_name
   ```

   查看指令：

   ```
   docker images
   ```

3. 创建容器脚本：

   新键一个脚本，取名为start_docker.sh（自定义），指令为：

   ```
   vim start_docker.sh
   ```

   进入编辑界面后，将下面指令复制粘贴：

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

   输入:wq保存退出，得到一个创建容器的脚本，脚本的最后一行为镜像的REPOSITORY:TAG，REPOSITORY保持为MindIE不用改，TAG需要根据要选择的镜像来创建容器。

   创建容器指令（your_docker_name是自定义的容器名，建议姓名+MindIE版本）：

   ```
   bash start_docker.sh your_docker_name
   ```

4. 使用容器：

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

5. 卸载环境

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

# 5 源码编译与安装

1. 根据前面的内容创建MindIE镜像对应的容器，然后进入容器后编译代码（说明：编译的前提是已经安装好docker app、镜像、容器、HDK等环境，MindIE镜像的容器已经配好所有需要编译的环境）

2. 编译代码需要执行下列环境变量：

   ```
   export NO_CHECK_CERTIFICATE=1
   ```

   请保证服务器可以联网

3. 拉取代码

   建议保存这个脚本，并且配上自己的gitcode的id和令牌（或者密码），每次直接一键拉取代码

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

4. 版本匹配

   <div style="background:#f0f9ff;border-left:4px solid #2196f3;padding:14px;margin:16px 0;border-radius:6px;">
   <strong>💡 说明</strong>
   <ul style="margin:8px 0;padding-left:20px;">
       拉取大EP服务需要自行选择一个版本的MindIE镜像。如果需要修改MindIE-Motor、MindIE-LLM代码仓的代码（比如添加打印），在进行编译之前，需要将代码仓版本回退到和需要使用的MindIE镜像版本一致，如果不一致可能会报错。
   </ul></div>

   - 参考前面的步骤创建并进入要使用的MindIE镜像对应的容器

   - 进入容器后，查看代码版本：

     ```
     cat /usr/local/Ascend/atb-models/version.info
     ```

   - 打屏显示：

     ```
     MindIE-ATB Tag/Branch : master
     MindIE-ATB Version :
     ATB-Models Version : 2.2.RC1.B120
     Commit id : 0245b1fe907844715fe260528943c9b9f61be862
     Platform : aarch64
     Time: 2025-10-24 12:26:52 PM CST
     ```

   - 请选择第四行的commit id来回退MindIE-LLM代码仓，回退指令为（以上面的结果举例）：

     ```
     git checkout 0245b1fe907844715fe260528943c9b9f61be862
     ```

   - 根据上面的时间2025-10-24 12:26:52 PM CST，在MindIE-Motor代码仓中，找出最接近这个时间点的[commit id(点此链接进行查找)](https://gitcode.com/Ascend/MindIE-Motor/commits/master)，并用此commit id对Motor仓进行回退

   - 执行界面如图：

   ![img](https://raw.gitcode.com/user-images/assets/8772838/cb6b6c4e-482c-47c0-8b35-9743c06a812b/image.png)

5. 执行编译：

   执行编译之前，请确认是否已经将代码仓调整到正确的版本。


   一键编译所有组件指令：

   ```
   bash build/build.sh -a
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
   - -p为打包，将MindIE LLM的包和MindIE Motor的包打包成MindIE包

   首次编译时间约为45分钟，受磁盘传输数据速度影响

6. 编译成功会得到三个包

   - MindIE-LLM仓编译出的包：

     ```
     #your_code_path为代码仓路径
     /your_code_path/MindIE-Motor/output/modules/Ascend-mindie-llm_1.0.RC3_py311_linux-aarch64.run
     ```
   - MindIE-Motor仓编译出的包：

     ```
     /your_code_path/MindIE-Motor/output/modules/Ascend-mindie-service_1.0.0_py311_linux-aarch64.run
     ```
   - 两个仓打包成的MindIE包：

     ```
     /your_code_path/MindIE-Motor/output/aarch64/Ascend-mindie_1.0.0_linux-aarch64.run
     ```
   - 打印对应包的hash值：

     ```
     md5sum /your_code_path/MindIE-Motor/output/aarch64/Ascend-mindie_1.0.0_linux-aarch64.run
     ```

7. 在PD分离拉起服务脚本中的boot.sh中，在#!/bin/bash和set_common_env之间加入下列代码，安装编译得到的MindIE包：

   ```
   #!/bin/bash
   bash_path=/your_code_path/1209/MindIE-Motor/output/aarch64
   mindie_run=Ascend-mindie_1.0.0_linux-aarch64.run
   cd ${bash_path} && chmod +x ./${mindie_run} && echo "y" | ./${mindie_run} --install
   md5sum  ${bash_path}/${mindie_run} 
   unset http_proxy https_proxy
   set_common_env
   ```

k8s集群在拉起服务时，会创建各个节点对应的容器，每个容器创建后，均会执行boot.sh脚本，即每个容器都会安装编译的MindIE包

PD分离环境配置请参考此[教程](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docs/zh/User_Guide/PD%E5%88%86%E7%A6%BB%E7%8E%AF%E5%A2%83%E9%85%8D%E7%BD%AE.md)

# 6 常见问题

请参考[FAQ的编译类问题](https://gitcode.com/Ascend/MindIE-Motor/blob/dev/docs/zh/user_guide/faq.md)章节

