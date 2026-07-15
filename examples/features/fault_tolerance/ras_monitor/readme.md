# ras_monitor MindIE Motor 中的部署指南

## 概述

出于PD实例可靠性增强的目的，**MindIE Motor** 提供一个参考脚本 **ras_monitor** 进行大EP服务的健康状态监控和快速重启，**ras_monitor** 启动后，当软件故障发生导致服务不可用时，该脚本20分钟左右可检测到并启动自动重拉。本文档提供快速部署 **ras_monitor** 的完整配置部署示例。

适用范围说明：

- 适用机器：Atlas 800I A2/A3，Atlas 900I A3 机器
- 适用场景：大EP出现挂死等服务不可用且不可自恢复的场景

## 1. 准备软件或数据

### 1.1 前提条件

- **硬件**：Atlas 800I A3 推理服务器
- **软件**:
  - NPU 驱动和固件已安装 (`npu-smi info` 可正常显示)
  - Kubernetes 集群就绪 (`kubectl get Node -A`)
  - Docker 已安装并运行 (`docker ps`)

### 1.2 获取ras_monitor脚本及其依赖文件

从[链接](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/fault_tolerance/ras_monitor/ras_monitor.py)获取最新的ras_monitor脚本

## 2. 部署步骤

2.1 登陆master节点，将 **准备软件或数据** 下载的 "ras_monitor.py" 脚本上传到 “examples/deployer” 路径下。

2.2 执行以下命令拉起ras_monitor脚本进行后台监控：
nohup python3 ras_monitor.py --config_dir ../infer_engines/vllm

若预期记录ras_monitor日志，可通过linux的重定向文件记录，例如：
nohup python3 ras_monitor.py --config_dir ../infer_engines/vllm > ras_monitor_result.txt 2>&1 &

## 3.说明

### 参数说明

由于故障发生一段时间后，ras_monitor 执行服务重拉时将调用 deploy.py，上述2.2中 ras_monitor 的输入参数建议与服务拉起时执行 deploy.py 脚本的输入参数保持一致，否则可能导致重拉失败。
具体 deploy.py 的参数介绍见[链接](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/deployer/README.md) 。

### 其他

1、由于 ras_monitor 的定位为大 EP 的健康伴侣，与大 EP 的启动执行脚本 deploy.py 解耦。若 ras_monitor 先启动，用户需在 `examples/deployer` 目录下手动执行 `python3 deploy.py --config_dir ../infer_engines/vllm`（或使用 `--user_config_path` 与 `--env_config_path` 指定配置文件）拉起服务后，ras_monitor 才进入监控流程，否则将一直等待服务拉起并 ready。

2、若在 ras_monitor 监控过程中客户有修改配置的诉求，若在执行 `bash delete` 删除服务后，客户未终止ras_monitor 进程，ras_monitor 作为自动化脚本可能会误判认为服务异常，执行重拉。因此，建议在 `bash delete` 执行后，手动停止ras_monitor 进程以防止误重启。
