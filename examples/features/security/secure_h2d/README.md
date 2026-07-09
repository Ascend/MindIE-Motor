# 部署流程文档

## 一、环境准备

### 1.1前置环境配置

适配此功能的前置环境配置： Ascend Inference Inference 26.1.0 HDK、vLLM-Ascend 26.1.0

容器拉起(示例)

```bash
docker_setup.sh [image_id] [docker_name]
```

注：以下示例中共享目录请替换为实际模型权重所在目录

重要：socket路径一定要挂载，否则secure_h2d功能无法正常开启

```bash
#docker_setup.sh bash
IMAGES_ID=$1
NAME=$2
if [ $# -ne 2 ]; then
    echo "error: need one argument describing your container name."
    exit 1
fi
docker run --name ${NAME} -it -d --net=host --shm-size=500g \
    --privileged=true \
    -w /home \
    --device=/dev/davinci_manager \
    --device=/dev/hisi_hdc \
    --device=/dev/devmm_svm \
    --entrypoint=bash \
    -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
    -v /usr/local/dcmi:/usr/local/dcmi \
    -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
    -v /etc/ascend_install.info:/etc/ascend_install.info \
    -v /usr/local/sbin:/usr/local/sbin \
    -v /home/data/model:/home/data/model \
    -v /data:/data \
    -v /run/kmsagent/socket:/run/kmsagent/socket \
    -v /etc/hccn.conf:/etc/hccn.conf \
    -v /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime \
    -e http_proxy=$http_proxy \
    -e https_proxy=$https_proxy \
    ${IMAGES_ID}
```

### 1.2配置软件环境

一键化部署脚本(注：需要容器内联通网络，否则需要将第11、12步编译好的依赖传入容器中安装，流程详见setup.sh)

```bash
#执行/MindIE-PyMotor/examples/features/security/secure_h2d/setup.sh
bash ./setup.sh {已拉起的容器名称}
```
