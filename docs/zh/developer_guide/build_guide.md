# 编译指导

## 编译说明

本文档介绍如何从源码编译MindIE-Motor，生成 `.whl` 包，安装与运行。

## 环境准备

## 镜像安装方式

MindIE镜像获取请参见[镜像安装方式](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/install/source/image_usage_guide.md#%E8%8E%B7%E5%8F%96mindie%E9%95%9C%E5%83%8F)。

## 容器/物理机安装方式

1. 容器/物理机安装方式，需要准备的软件包和依赖请参见[准备软件包和依赖](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/install/source/preparing_software_and_dependencies.md)。
2. 容器/物理机安装方式，软件包和依赖的安装请参见[安装软件包和依赖](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/install/source/installing_software_and_dependencies.md)。

## 编译安装

1. 安装Python工具.MindIE-Motor 支持 **Python == 3.11**。

    ```bash
    pip install --upgrade pip
    pip install wheel setuptools
    ```

2. 克隆源码仓库。

    ```bash
    git clone https://gitcode.com/Ascend/MindIE-Motor.git
    cd MindIE-Motor
    ```

3. 编译第三方依赖。

    ```bash
    bash build/build.sh -d 3rd -b 3rd
    ```

4. 设置环境变量。
    获取 Python site-packages 路径（建议不要强行编码 torch 路径），并配置动态库搜索路径：

    ```bash
    TORCH_PATH=$(python3 -c "import torch, os; print(os.path.dirname(torch.__file__))")
    export LD_LIBRARY_PATH=${TORCH_PATH}/lib:${TORCH_PATH}/../torch.libs:$LD_LIBRARY_PATH
    ```

    可选：指定生成 `.whl` 包的版本号：

    ```bash
    export MINDIE_MOTOR_VERSION_OVERRIDE=3.0.0
    ```

5. 编译生成 `.whl` 包。

    ```bash
    cd mindie_motor/python
    pip wheel . --no-build-isolation -v
    ```

    * 编译完成后，会在当前目录生成 `mindie_motor-<version>-*.whl` 文件。
    * 编译时，`setup.py` 会自动调用 `build.sh` 编译C++代码，并拷贝第三方依赖到包内。
    * 编译后，生成临时目录 `build`、存放二进制的目录 `output` 和debug符号表`motor_debug_symbols` 目录。

6. 安装 MindIE-Motor。
   
    ```bash
    pip install mindie_motor*.whl
    ```

7. 权限配置。
    由于whl包安装后是按照系统设定的权限，如下权限可能需要适配（可根据实际权限报错处理），以大EP为例，在boot.sh中添加如下命令：

    ```bash
    chmod 500 /usr/local/lib/python3.11/site-packages/mindie_motor/scripts/http_client_ctl/*;
    chmod 550 /usr/local/lib/python3.11/site-packages/mindie_motor/examples/kubernetes_deploy_scripts/boot_helper/*; 
    chmod 640 /usr/local/lib/python3.11/site-packages/mindie_motor/examples/kubernetes_deploy_scripts/boot_helper/boot.sh; 
    chmod 500 /usr/local/lib/python3.11/site-packages/mindie_motor/scripts/http_client_ctl/*; 
    chmod 700 /root; 
    chmod 640 /usr/local/lib/python3.11/site-packages/mindie_motor/conf/model_config/*.json;
    chmod 640 /usr/local/lib/python3.11/site-packages/mindie_motor/conf/machine_config/*.json;
    ```

## 升级

详情请参见[升级](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/install/source/upgrade.md)章节。

## 卸载

详情请参见[卸载](https://gitcode.com/Ascend/MindIE-LLM/blob/dev/docs/zh/user_guide/install/source/uninstallation.md)章节。
