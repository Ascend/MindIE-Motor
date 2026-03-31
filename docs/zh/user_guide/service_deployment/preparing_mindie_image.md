# 准备MindIE镜像

本章节指导用户如何制作MindIE镜像包，仅用于集群部署服务时使用。

## 环境准备

硬件环境和操作系统如下所示：

|硬件|操作系统（建议）|
|--|--|
|Atlas 800I A2 推理服务器|Arm：<br>Ubuntu 22.04|
|Atlas 300I Duo 推理卡+Atlas 800 推理服务器（型号 3000）|Arm：<br>Ubuntu 20.04|

## 准备软件包<a id="zbrjb"></a>

制作MindIE镜像包对所依赖的软件版本有特定的要求，具体所需版本如下所示。

**表 1**  软件介绍

|软件类型|软件包名称|软件说明|获取链接|
|--|--|--|--|
|MindIE|Ascend-mindie_2.3.0_linux-aarch64_abi0.run|推理引擎软件包。|[获取链接](https://www.hiascend.com/developer/download/community/result?module=ie+pt+cann)|
|CANN|Ascend-cann-toolkit_8.5.0_linux-aarch64.run|开发套件包。|[获取链接](https://www.hiascend.com/developer/download/community/result?module=ie+pt+cann)|
|CANN|<li>Atlas 800I A2 推理服务器：</li><br>Ascend-cann-910*x*-ops_8.5.0_linux-aarch64.run<li>Atlas 300I Duo 推理卡+Atlas 800 推理服务器（型号 3000）：</li><br>Ascend-cann-310*x*-ops_8.5.0_linux-aarch64.run<br>**以上软件包名中的910*x*和310*x*请根据具体的硬件型号进行替换。**|二进制算子包。|[获取链接](https://www.hiascend.com/developer/download/community/result?module=ie+pt+cann)|
|CANN|Ascend-cann-nnal_8.5.0_linux-aarch64.run|加速库软件包|[获取链接](https://www.hiascend.com/developer/download/community/result?module=ie+pt+cann)|
|ATB Models|Ascend-mindie-atb-models_2.3.0_linux-aarch64_torch2.2.0-abi0.tar.gz|模型库安装包。|[获取链接](https://www.hiascend.com/developer/download/community/result?module=ie+pt+cann)|
|Ascend Extension for PyTorch|torch_npu-2.1.0.post10-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl|torch_npu插件whl包。|[获取链接](https://www.hiascend.com/developer/download/community/result?module=ie+pt+cann)<li>获取2.1.0版本的torch_npu，请在社区版资源下载页面左上方“配套资源”中，选择PyTorch版本为7.2.0。</li><li>在PyTorch栏单击对应版本后方的“获取源码”，跳转至PyTorch的gitcode仓库发布页，然后再页面下方获取对应版本的torch_npu。</li>|
|Ascend Extension for PyTorch|torch-2.1.0-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl|PyTorch框架2.1.0版本的whl包。|[获取链接](https://download.pytorch.org/whl/cpu/torch-2.1.0-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl)|

## 前提条件

- 在一个全新的容器内可能会出现apt源下载路径问题，请用户配置Ubuntu 22.04的专用源，提升下载速度。
- 安装过程需要下载相关依赖，请确保安装环境能够连接网络。
- 请在宿主机执行如下命令检查源是否可用。

    ```bash
    apt update
    ```

    如果命令执行报错或者后续安装依赖时等待时间过长甚至报错，则检查网络是否连接或者把 “/etc/apt/sources.list” 文件中的源更换为可用的源或使用镜像源（以配置华为镜像源为例，可参考[华为开源镜像站](https://mirrors.huaweicloud.com/)）。

- 用户在宿主机自行安装Docker（版本要求大于等于24.x.x）。

## 操作步骤

以Atlas 800I A2 推理服务器为例制作MindIE镜像。

>[!NOTE]说明
>制作镜像时非root用户ID应该和执行环境上安装的驱动用户ID保持一致，或安装驱动时使用--install-for-all参数为所有用户安装。

1. 将从[软件包获取](#zbrjb)下载或制作的软件放到某一目录下，例如：/home/package。
2. 使用以下命令拉取Ubuntu 22.04镜像。
    
    ```bash
    docker pull ubuntu:22.04
    ```

    执行以下命令检查Ubuntu镜像。

    ```bash
    docker images | grep ubuntu
    ```

    屏幕打印以下类似结果，则说明镜像已拉取成功：

    ```bash
    ubuntu                    22.04                         981912c48e9a   7 weeks ago    69.2MB
    ```

3. <a id="li818718573308"></a>在/home/package路径下编写Dockerfile及其他安装时需要的脚本文件，其文件目录结构必须为以下结构。

    ```linux
    ├── Ascend-cann-xxxx-ops_8.5.0_linux-aarch64.run
    ├── Ascend-cann-nnal_8.5.0_linux-aarch64.run
    ├── Ascend-cann-toolkit_8.5.0_linux-aarch64.run
    ├── Ascend-mindie_2.3.0_linux-aarch64_abi0.run
    ├── Ascend-mindie-atb-models_2.3.0_linux-aarch64_torch2.1.0-abi0.tar.gz
    ├── docker
    │   ├── docker_build.sh
    │   └── Dockerfile
    ├── install_cann.sh
    ├── install_mindie.sh
    ├── install_pta.sh
    ├── requirements-2.1.0.txt
    ├── server.js
    ├── torch_npu-2.1.0.post10-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
    └── torch-2.1.0-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
    ```

    其中docker_build.sh、Dockerfile、install_cann.sh、install_mindie.sh、install_pta.sh、requirements-2.1.0.txt、server.js文件为用户自行编写的文件。

    1. 编写docker_build.sh文件。

        ```shell
        docker build \
        --build-arg no_proxy=127.0.0.1,localhost,local,.local,172.17.0.1 \
        --build-arg DEVICE=9xxx \
        --build-arg ARCH=aarch64 \
        --build-arg CANN_VERSION=8.5.0 \
        --build-arg TORCH_VERSION=2.1.0 \
        --build-arg MINDIE_VERSION=2.3.0 \
        --build-arg PY_VERSION=310 \
        -t mindie:2.3.0-aarch64-800I-A2 \
        --target mindie .
        ```

        >[!NOTE]说明
        >- DEVICE为硬件型号。
        >- PY_VERSION为Python版本号。
        >- mindie:2.3.0-aarch64-800I-A2为自定义的镜像名称。

    2. 编写Dockerfile文件。

        该Dockerfile默认使用Ubuntu 22.04、AArch架构以及Python 3.10，仅作为参考，用户可自行修改。（如果基于Ubuntu 24.04操作系统制作镜像，Dockerfile文件中的**libgl1-mesa-glx**需改为**libglx-mesa0**）

        ```json
        # Please make sure all `ARG` have been set correctly
        # Set the arguments for different images
        
        FROM ubuntu:22.04 AS base
        
        ARG UBUNTU_VERSION=22.04
        
        ARG ARCH
        ARG DEVICE
        LABEL description="Image for ${DEVICE} based on Ubuntu${UBUNTU_VERSION} ${ARCH}"
        
        RUN groupadd -g 1000 mindie && \
            useradd -u 1000 -g 1000 -m mindieuser
        
        ENV PYTHONPATH=/home/mindieuser/.local/lib/python3.10/site-packages:$PATH
        ENV PATH=/home/mindieuser/.local/bin:$PYTHONPATH
        
        RUN echo 'export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/driver:$LD_LIBRARY_PATH' >> /home/mindieuser/.bashrc && \
            echo 'export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/common:$LD_LIBRARY_PATH' >> /home/mindieuser/.bashrc && \
            echo 'export PYTHONPATH=/home/mindieuser/.local/lib/python3.10/site-packages:$PATH' >> /home/mindieuser/.bashrc && \
            echo 'export PATH=/home/mindieuser/.local/bin:$PYTHONPATH' >> /home/mindieuser/.bashrc && \
            echo "export LANG=en_US.UTF-8" >> /home/mindieuser/.bashrc && \
            echo "export LANGUAGE=en_US:en" >> /home/mindieuser/.bashrc && \
            echo "export LC_ALL=en_US.UTF-8" >> /home/mindieuser/.bashrc && \
            # Configure the Ubuntu mirror. An example is provided below:
            apt-get update && \
            apt-get install --no-install-recommends -y ca-certificates && \
            DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y tzdata && \
            apt-get install --no-install-recommends -y gcc g++ make cmake && \
            apt-get install --no-install-recommends -y zlib1g zlib1g-dev libbz2-dev liblzma-dev libffi-dev libssl-dev openssl libsqlite3-dev && \
            apt-get install --no-install-recommends -y libblas-dev liblapack-dev libopenblas-dev libblas3 liblapack3 gfortran libhdf5-dev && \
            apt-get install --no-install-recommends -y wget curl pkg-config vim libxml2 libxslt1-dev locales && \
            apt-get install --no-install-recommends -y pciutils net-tools ipmitool numactl linux-tools-common && \
            apt-get install --no-install-recommends -y libgl1-mesa-glx libgmpxx4ldbl && \
            apt-get install --no-install-recommends -y xz-utils unzip && \
            apt-get install --no-install-recommends -y python3-pip python-is-python3 && \
            # Configure the pip mirror. An example is provided below:
            # pip config --user set global.index https://mirrors.tools.huawei.com/pypi && \
            # pip config --user set global.index-url https://mirrors.tools.huawei.com/pypi/simple && \
            # pip config --user set global.trusted-host mirrors.tools.huawei.com && \
            locale-gen en_US.UTF-8
        
        WORKDIR /home/mindieuser
        USER mindieuser
        ENV TZ=Asia/Shanghai
        
        #######################################################################################
        # docker build -t cann --target cann .
        #######################################################################################
        FROM base AS cann
        
        ARG DEVICE
        ARG ARCH
        ARG CANN_VERSION
        
        RUN echo "source /home/mindieuser/Ascend/cann/set_env.sh" >> /home/mindieuser/.bashrc && \
            echo "source /home/mindieuser/Ascend/nnal/atb/set_env.sh" >> /home/mindieuser/.bashrc && \
            wget -q http://172.17.0.1:3000/Ascend-cann-toolkit_${CANN_VERSION}_linux-${ARCH}.run -P /home/mindieuser/package/ && \
            wget -q http://172.17.0.1:3000/Ascend-cann-${DEVICE}-ops_${CANN_VERSION}_linux-${ARCH}.run -P /home/mindieuser/package/ && \
            wget -q http://172.17.0.1:3000/Ascend-cann-nnal_${CANN_VERSION}_linux-${ARCH}.run -P /home/mindieuser/package/ && \
            wget -q http://172.17.0.1:3000/install_cann.sh -P /home/mindieuser/package/ && \
            cd /home/mindieuser/package && \
            bash install_cann.sh && \
            rm -rf /home/mindieuser/package/*
        
        
        #######################################################################################
        # docker build -t pta --target pta .
        #######################################################################################
        FROM cann AS pta
        
        ARG DEVICE
        ARG ARCH
        ARG TORCH_VERSION
        
        RUN wget -q http://172.17.0.1:3000/torch_npu-${TORCH_VERSION}.post10-cp310-cp310-manylinux_2_17_${ARCH}.manylinux2014_${ARCH}.whl -P /home/mindieuser/package/ && \
            wget -q http://172.17.0.1:3000/torch-${TORCH_VERSION}-cp310-cp310-manylinux_2_17_${ARCH}.manylinux2014_${ARCH}.whl -P /home/mindieuser/package/ && \
            wget -q http://172.17.0.1:3000/requirements-${TORCH_VERSION}.txt -P /home/mindieuser/package/ && \
            wget -q http://172.17.0.1:3000/install_pta.sh -P /home/mindieuser/package/ && \
            cd /home/mindieuser/package && \
            pip install -r requirements-${TORCH_VERSION}.txt --no-cache-dir && \
            bash install_pta.sh && \
            pip cache purge && \
            rm -rf /home/mindieuser/package/*
        
        
        #######################################################################################
        # docker build -t mindie --target mindie .
        #######################################################################################
        FROM pta AS mindie
        
        ARG DEVICE
        ARG ARCH
        ARG TORCH_VERSION
        ARG MINDIE_VERSION
        
        RUN echo "source /home/mindieuser/Ascend/mindie/set_env.sh" >> /home/mindieuser/.bashrc && \
            echo "source /home/mindieuser/Ascend/atb-models/set_env.sh" >> /home/mindieuser/.bashrc && \
            wget -q http://172.17.0.1:3000/Ascend-mindie_${MINDIE_VERSION}_linux-${ARCH}_abi0.run -P /home/mindieuser/package/ && \
            wget -q http://172.17.0.1:3000/Ascend-mindie-atb-models_${MINDIE_VERSION}_linux-${ARCH}_py310_torch${TORCH_VERSION}-abi0.tar.gz -P /home/mindieuser/package/ && \
            wget -q http://172.17.0.1:3000/install_mindie.sh -P /home/mindieuser/package/ && \
            cd /home/mindieuser/package && \
            bash install_mindie.sh && \
            pip cache purge && \
            rm -rf /home/mindieuser/package/*
        ```

        >[!NOTE]说明
        >- Dockerfile文件中软件包的名称必须和实际准备的软件包名称保持一致。
        >- 在pta构建阶段，可以自行指定transformers库的版本，因为有些模型对于其版本有较为严格的要求，在Dockerfile中已给出了示例修改的位置。在构建时修改，好处在于构建出来的镜像可以直接启动推理服务，而不再需要额外进入容器操作。
        >- 由于当前版本同样适配Python 3.11.4，以下给出更换Python 3.11.4的参考方法。
        >   1. 首先需要从Python官网获取Python 3.11.4压缩包，下载链接如下所示：
        >
        >        ```text
        >        https://www.python.org/ftp/python/3.11.4/Python-3.11.4.tgz
        >        ```
        >
        >   2. 将获取到的Python 3.11.4压缩包移至构建目录/home/package。
        >   3. 在Dockerfile中**替换base层的构建命令，并且修改脚本内与Python版本相关的内容**：
        >
        >        ```json
        >        FROM ubuntu:22.04 AS base
        >        ARG UBUNTU_VERSION=22.04
        >        ARG PYTHON_VERSION=3.11.4  # Set python version
        >        ARG ARCH
        >        ARG DEVICE
        >        LABEL description="Image for ${DEVICE} based on Ubuntu${UBUNTU_VERSION} ${ARCH}"
        >        RUN groupadd -g 1000 mindie && \
        >            useradd -u 1000 -g 1000 -m mindieuser
        >        ENV PYTHONPATH=/home/mindieuser/.local/lib/python3.10/site-packages:$PATH
        >        ENV PATH=/home/mindieuser/.local/bin:$PYTHONPATH
        >        ENV LD_LIBRARY_PATH="/usr/local/lib:$LD_LIBRARY_PATH"
        >        RUN echo 'export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/driver:$LD_LIBRARY_PATH' >> /home/mindieuser/.bashrc && \
        >            echo 'export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/common:$LD_LIBRARY_PATH' >> /home/mindieuser/.bashrc && \
        >            echo 'export PYTHONPATH=/home/mindieuser/.local/lib/python3.11.4/site-packages:$PATH' >> /home/mindieuser/.bashrc && \
        >            echo 'export PATH=/home/mindieuser/.local/bin:$PYTHONPATH' >> /home/mindieuser/.bashrc && \
        >            echo "export LANG=en_US.UTF-8" >> /home/mindieuser/.bashrc && \
        >            echo "export LANGUAGE=en_US:en" >> /home/mindieuser/.bashrc && \
        >            echo "export LC_ALL=en_US.UTF-8" >> /home/mindieuser/.bashrc && \
        >            # Configure the Ubuntu mirror. An example is provided below:
        >            apt-get update && \
        >            apt-get install --no-install-recommends -y ca-certificates && \
        >            DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y tzdata && \
        >            apt-get install --no-install-recommends -y gcc g++ make cmake && \
        >            apt-get install --no-install-recommends -y zlib1g zlib1g-dev libbz2-dev liblzma-dev libffi-dev libssl-dev openssl libsqlite3-dev && \
        >            apt-get install --no-install-recommends -y libblas-dev liblapack-dev libopenblas-dev libblas3 liblapack3 gfortran libhdf5-dev && \
        >            apt-get install --no-install-recommends -y wget curl pkg-config vim libxml2 libxslt1-dev locales && \
        >            apt-get install --no-install-recommends -y pciutils net-tools ipmitool numactl linux-tools-common && \
        >            apt-get install --no-install-recommends -y libgl1-mesa-glx libgmpxx4ldbl && \
        >            apt-get install --no-install-recommends -y xz-utils unzip && \
        >            wget -q http://172.17.0.1:3000/Python-$PYTHON_VERSION.tgz -P /opt/package/ && \
        >            cd /opt/package && \
        >            tar -xf Python-$PYTHON_VERSION.tgz && \
        >            cd Python-$PYTHON_VERSION && \
        >            ./configure --enable-optimizations --enable-shared --enable-loadable-sqlite-extensions --with-lto --with-ensurepip --with-computed-gotos && \
        >            make -j$(nproc) && \
        >            make altinstall && \
        >            cd .. && \
        >            rm -rf Python-$PYTHON_VERSION Python-$PYTHON_VERSION.tgz && \
        >            ln -sf /usr/local/bin/python3.11 /usr/bin/python3 && \
        >            ln -sf /usr/local/bin/python3.11 /usr/bin/python && \
        >            python -m ensurepip --default-pip && \
        >            ln -sf /usr/local/bin/pip3.11 /usr/bin/pip3 && \
        >            ln -sf /usr/local/bin/pip3.11 /usr/bin/pip && \
        >            # Configure the pip mirror. An example is provided below:
        >            # pip config --user set global.index https://mirrors.tools.huawei.com/pypi && \
        >            # pip config --user set global.index-url https://mirrors.tools.huawei.com/pypi/simple && \
        >            # pip config --user set global.trusted-host mirrors.tools.huawei.com && \
        >            locale-gen en_US.UTF-8
        >        WORKDIR /home/mindieuser
        >        USER mindieuser
        >        ENV TZ=Asia/Shanghai
        >        ```

    3. 编写install_cann.sh文件。

        ```shell
        #!/bin/bash
        
        CANN_TOOKIT="Ascend-cann-toolkit_*_linux-*.run"
        CANN_OPS="Ascend-cann-*-ops_*_linux-*.run"
        CANN_NNAL="Ascend-cann-nnal_*_linux-*.run"
        chmod +x *.run
        yes | ./${CANN_TOOKIT} --install --quiet
        toolkit_status=$?
        if [ ${toolkit_status} -eq 0 ]; then
            echo "install toolkit successfully"
        else
            echo "install toolkit failed with status ${toolkit_status}"
        fi
        
        yes | ./${CANN_OPS} --install --quiet
        ops_status=$?
        if [ ${ops_status} -eq 0 ]; then
            echo "install ops successfully"
        else
            echo "install ops failed with status ${ops_status}"
        fi
        source /home/mindieuser/Ascend/cann/set_env.sh
        yes | ./${CANN_NNAL} --install --quiet
        nnal_status=$?
        if [ ${nnal_status} -eq 0 ]; then
            echo "install nnal successfully"
        else
            echo "install nnal failed with status ${nnal_status}"
        fi
        ```

    4. 编写install_mindie.sh文件。

        ```shell
        #!/bin/bash
        
        source /home/mindieuser/Ascend/cann/set_env.sh
        
        mkdir -p /home/mindieuser/Ascend/atb-models
        MINDIE="Ascend-mindie_*_linux-*.run"
        MODEL="Ascend-mindie-atb-models_*_linux-aarch64_py310_torch2.1.0-abi0.tar.gz"
        chmod +x *.run
        tar -xzf ./${MODEL} -C /home/mindieuser/Ascend/atb-models
        yes | ./${MINDIE} --install --quiet 2> /dev/null
        mindie_status=$?
        if [ ${mindie_status} -eq 0 ]; then
            echo "install mindie successfully"
        else
            echo "install mindie failed with status ${mindie_status}"
        fi
        ```

    5. 编写install_pta.sh文件。

        ```shell
        #!/bin/bash
        
        pip3 install torch-2.1.0-cp310-cp310-manylinux_2_17_*.manylinux2014_*.whl
        
        TORCH_NPU_IN_PYTORCH_MANYLINUX=torch_npu-2.1.0*aarch64.whl
        
        echo "start install torch_npu, wait for a minute..."
        pip install ${TORCH_NPU_IN_PYTORCH_MANYLINUX}
        ```

    6. 编写requirements-2.1.0.txt文件。

        ```txt
        #
        # This file is autogenerated by pip-compile with Python 3.10
        # by the following command:
        #
        #    pip-compile
        
        absl-py==2.1.0
            # via rouge-score
        accelerate==0.34.2
            # via -r requirements.in
        annotated-types==0.7.0
            # via pydantic
        attrs==24.2.0
            # via jsonlines
        brotli==1.1.0
            # via geventhttpclient
        certifi==2024.8.30
            # via
            #   geventhttpclient
            #   requests
        cffi==1.17.1
            # via -r requirements.in
        charset-normalizer==3.3.2
            # via requests
        click==8.1.7
            # via nltk
        cloudpickle==3.0.0
            # via -r requirements.in
        colorama==0.4.6
            # via sacrebleu
        contourpy==1.3.0
            # via matplotlib
        cpm-kernels==1.0.11
            # via -r requirements.in
        cycler==0.12.1
            # via matplotlib
        decorator==5.1.1
            # via -r requirements.in
        et-xmlfile==1.1.0
            # via openpyxl
        filelock==3.16.1
            # via
            #   huggingface-hub
            #   icetk
            #   torch
            #   transformers
        fonttools==4.54.1
            # via matplotlib
        fsspec==2024.9.0
            # via
            #   huggingface-hub
            #   torch
        fuzzywuzzy==0.18.0
            # via -r requirements.in
        gevent==24.2.1
            # via geventhttpclient
        geventhttpclient==2.3.1
            # via -r requirements.in
        greenlet==3.1.1
            # via gevent
        grpcio==1.66.1
            # via tritonclient
        huggingface-hub==0.25.1
            # via
            #   accelerate
            #   tokenizers
            #   transformers
        icetk==0.0.4
            # via -r requirements.in
        idna==3.10
            # via requests
        jieba==0.42.1
            # via -r requirements.in
        jinja2==3.1.4
            # via torch
        joblib==1.4.2
            # via nltk
        jsonlines==4.0.0
            # via -r requirements.in
        kiwisolver==1.4.7
            # via matplotlib
        latex2mathml==3.77.0
            # via mdtex2html
        loguru==0.7.2
            # via -r requirements.in
        lxml==5.3.0
            # via sacrebleu
        markdown==3.7
            # via mdtex2html
        markupsafe==2.1.5
            # via jinja2
        matplotlib==3.9.2
            # via -r requirements.in
        mdtex2html==1.3.0
            # via -r requirements.in
        ml-dtypes==0.5.0
            # via -r requirements.in
        mpmath==1.3.0
            # via sympy
        networkx==3.3
            # via torch
        nltk==3.9.1
            # via rouge-score
        numpy==1.26.4
            # via
            #   -r requirements.in
            #   accelerate
            #   contourpy
            #   matplotlib
            #   ml-dtypes
            #   pandas
            #   pyarrow
            #   rouge-score
            #   sacrebleu
            #   scipy
            #   torchvision
            #   transformers
            #   tritonclient
        openpyxl==3.1.5
            # via -r requirements.in
        packaging==24.1
            # via
            #   accelerate
            #   huggingface-hub
            #   matplotlib
            #   transformers
            #   tritonclient
        pandas==2.2.3
            # via -r requirements.in
        pathlib2==2.3.7.post1
            # via -r requirements.in
        pillow==10.4.0
            # via
            #   matplotlib
            #   torchvision
        portalocker==2.10.1
            # via sacrebleu
        prettytable==3.11.0
            # via -r requirements.in
        protobuf==3.20.0
            # via
            #   -r requirements.in
            #   tritonclient
        psutil==6.0.0
            # via accelerate
        pyarrow==17.0.0
            # via -r requirements.in
        pycparser==2.22
            # via cffi
        pydantic==2.9.2
            # via -r requirements.in
        pydantic-core==2.23.4
            # via pydantic
        pyparsing==3.1.4
            # via matplotlib
        python-dateutil==2.9.0.post0
            # via
            #   matplotlib
            #   pandas
        python-rapidjson==1.20
            # via tritonclient
        pytz==2024.2
            # via pandas
        pyyaml==6.0.2
            # via
            #   accelerate
            #   huggingface-hub
            #   transformers
        rapidfuzz==3.10.0
            # via thefuzz
        regex==2024.9.11
            # via
            #   nltk
            #   sacrebleu
            #   tiktoken
            #   transformers
        requests==2.32.3
            # via
            #   huggingface-hub
            #   icetk
            #   tiktoken
            #   torchvision
            #   transformers
        rouge==1.0.1
            # via -r requirements.in
        rouge-score==0.1.2
            # via -r requirements.in
        sacrebleu==2.4.3
            # via -r requirements.in
        safetensors==0.4.5
            # via
            #   accelerate
            #   transformers
        scipy==1.14.1
            # via -r requirements.in
        sentencepiece==0.2.0
            # via icetk
        six==1.16.0
            # via
            #   pathlib2
            #   python-dateutil
            #   rouge
            #   rouge-score
        sympy==1.13.3
            # via torch
        tabulate==0.9.0
            # via sacrebleu
        termcolor==2.4.0
            # via -r requirements.in
        thefuzz==0.22.1
            # via -r requirements.in
        tiktoken==0.7.0
            # via -r requirements.in
        tokenizers==0.19.1
            # via transformers
        torch==2.1.0
            # via
            #   -r requirements.in
            #   accelerate
            #   torchvision
        torchvision==0.16.0
            # via icetk
        tornado==6.4.1
            # via -r requirements.in
        tqdm==4.66.5
            # via
            #   huggingface-hub
            #   icetk
            #   nltk
            #   transformers
        transformers==4.44.0
            # via -r requirements.in
        tritonclient[grpc]==2.49.0
            # via -r requirements.in
        typing-extensions==4.12.2
            # via
            #   huggingface-hub
            #   pydantic
            #   pydantic-core
            #   torch
        tzdata==2024.2
            # via pandas
        urllib3==2.2.3
            # via
            #   geventhttpclient
            #   requests
            #   tritonclient
        wcwidth==0.2.13
            # via prettytable
        wheel==0.44.0
            # via -r requirements.in
        zope-event==5.0
            # via gevent
        zope-interface==7.0.3
            # via gevent
        
        # The following packages are considered to be unsafe in a requirements file:
        # setuptools
        ```

    7. 编写server.js文件。

        ```json
        const http = require('http');
        const fs = require('fs');
        const path = require('path');
        const port = 3000;
        const directory = __dirname;
        const server = http.createServer((req, res) => {
            const filePath = path.join(directory, req.url); 
        
            if (req.url ==='/files') {
                // return all file names in current directory
                fs.readdir(directory, (err, files)=> {
                   if (err) {
                      res.writeHead(500, {'Content-Type': 'text/plain'});
                      res.end('Internal Server Error\n');
                      return;
                   }
                   res.writeHead(200, {'Content-Type': 'application/json'});
                   res.end(JSON.stringify(files));
                });
            }else {
                fs.stat(filePath, (err, stats) => {
                    if (err || !stats.isFile()) {
                        res.writeHead(404, { 'Content-Type': 'text/plain' });
                        res.end('Not Found\n');
                        return;
                    }
                    fs.createReadStream(filePath).pipe(res);
                });
            }
        });
        
        server.listen(port, () => {
            console.log(`Server is running at http://localhost:${port}`);
        });
        ```

    >[!NOTE]说明
    >- 镜像制作过程中，用户需自行关注所依赖的第三方软件的安全性，如有问题请及时更新并修复。
    >- Dockerfile文件关键信息说明：
        >   - 设置镜像源：在Dockerfile的开头，可以设置Ubuntu的apt源，请设置为需要的地址，上面提供的地址仅供参考。
        >   - 设置pip源：可以在Dockerfile中将pip源设置为合适地址，以加快镜像构建速度。
        >   - wget指令：通过wget命令替代COPY命令，实现将宿主机上文件移入容器中的功能，具体请看[4](#li143348723716)。其中`http://172.17.0.1:3000`为宿主机在Docker网络中的地址，无需修改，例如以下命令即可将宿主机上的toolkit软件包用于构建。
        >
        >```bash
        >       `wget -q http://172.17.0.1:3000/Ascend-cann-toolkit_${CANN_VERSION}_linux-${ARCH}.run -P /opt/package/`
        >```

4. <a id="li143348723716"></a>在宿主机上启动server.js。

    1. 首先请确保宿主机上已安装好Node.js，用于启动服务。

        如果宿主机是Ubuntu系统，可以使用以下命令安装Node.js：

        ```bash
        apt install nodejs
        ```

    2. 然后在构建环境中，执行以下命令。

        ```bash
        cd /home/package  # 进入构建目录
        node server.js
        ```

    3. 回显如下所示，说明服务已启动成功。

        ```linux
        Server is running at http://localhost:3000
        ```

        **此时请保留该命令行窗口，等待构建完成后再行关闭。**

        >[!NOTE]说明
        >- 启动服务是为了让镜像构建时可以获取到对应软件包，Dockerfile中的wget命令等价于COPY命令，详情请参见[3](#li818718573308)。
        >- 该方式主要有两个好处：缩减镜像体积（将镜像体积从20GB以上缩小至13GB左右），以及减少构建时间（减少加载构建上下文的时间）。如果不想使用这种方式，可以使用Docker原生的COPY命令，并将Dockerfile与所有依赖软件包放在同一目录组成构建上下文。

5. 新建一个命令行窗口（请勿关闭步骤4中的命令行窗口），进入docker目录，开始构建镜像。

    ```bash
    cd /home/package/docker  # 进入docker目录
    bash docker_build.sh
    ```

    当镜像构建完成时，会出现类似如下打印信息：

    ```linux
    [+] Building 798.2s (9/9) FINISHE                                                                                                               Ddocker:default
     => [internal] load .dockerignore                                                                                                                          0.0s
     => => transferring context: 2B                                                                                                                            0.0s
     => [internal] load build definition from Dockerfile                                                                                                       0.0s
     => => transferring dockerfile: 6.12kB                                                                                                                     0.0s
     => [internal] load metadata for docker.io/library/ubuntu:22.04                                                                                            0.0s
     => CACHED [base 1/2] FROM docker.io/library/ubuntu:22.04                                                                                                  0.0s
     => [base 2/2] RUN echo 'export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/driver:$LD_LIBRARY_PATH' >> /root/.bashrc &&     
     echo 'export LD_LIBRARY_PATH=usr/local/Ascend/drive                                                                                                       187.2s
     => [cann 1/1] RUN echo "source /usr/local/Ascend/cann/set_env.sh" >> /home/mindieuser/.bashrc &&     
     echo "source /usr/local/Ascend/nnal/atb/set_env.sh" >> /home/mindieuser/.bashrc &&     wget                                                               274.4s
     => [pta 1/1] RUN wget -q http://172.17.0.1:3000/torch_npu-2.1.0.post8-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl 
     -P /opt/package/ &&     wget -q http://172.17.0.                                                                                                          272.9s
     => [mindie 1/1] RUN echo "source /usr/local/Ascend/mindie/set_env.sh" >> /home/mindieuser/.bashrc  &&     
     echo "source /usr/local/Ascend/atb-models/set_env.sh" >> /home/mindieuser/.bashrc &&     wget -q                                                          31.6s 
     => exporting to image                                                                                                                                     31.9s 
     => => exporting layers                                                                                                                                    31.9s 
     => => writing image sha256:ddc1229a39be3e2b9f2d0d88e809a4dc2db17ac9ec67c4c178c21fe1359eb6d7                                                               0.0s
     => => naming to docker.io/library/mindie:2.3.0-aarch64-800I-A2                                                                                            0.0s
    ```

    然后执行以下命令：

    ```bash
    docker images
    ```

    当回显以下信息，则表示镜像构建成功：

    ```linux
    REPOSITORY                    TAG                            IMAGE ID       CREATED              SIZE
    mindie                        2.3.0-aarch64-800I-A2          ddc1229a39be   About a minute ago   12.3GB
    ```
