# ras_monitor MindIE Motor 中的部署指南

## 概述

出于PD实例可靠性增强的目的，**MindIE Motor** 提供一个参考脚本 **ras_monitor** 进行大EP服务的健康状态监控和快速重启，**ras_monitor** 启动后，当软件故障发生导致服务不可用时，该脚本20分钟左右可检测到并启动自动重拉。本文档提供快速部署 **ras_monitor** 的完整配置部署示例。

适用范围说明：

- 适用机器：Atlas 800I A2推理服务器、Atlas 800I A3超节点服务器、Ascend 950PR&950DT系列产品
- 适用场景：大EP出现挂死等服务不可用且不可自恢复的场景
- 部署形态：Kubernetes 或 **Docker-only（无 K8s）**。Docker-only 不能依赖 `kubectl get pods`，需在启动命令中增加 `--coordinator-ip`；端口仍从 `user_config.json` 读取。

## 准备软件或数据

### 前提条件

- **硬件**：Atlas 800I A3推理服务器
- **软件**:
  - NPU 驱动和固件已安装 (`npu-smi info` 可正常显示)
  - Kubernetes 集群就绪 (`kubectl get Node -A`)
  - Docker 已安装并运行 (`docker ps`)

### 获取ras_monitor脚本及其依赖文件

从[链接](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/features/fault_tolerance/ras_monitor/ras_monitor.py)获取最新的ras_monitor脚本

## 部署步骤

2.1 登录master节点，将 **准备软件或数据** 下载的 "ras_monitor.py" 脚本上传到 “examples/deployer” 路径下。

2.2 执行以下命令拉起ras_monitor脚本进行后台监控：

Kubernetes部署场景：

```bash
nohup python3 ras_monitor.py --config_dir ../../model_configs/vllm > ras_monitor_result.txt 2>&1 &
```

Docker only部署场景：

```bash
nohup python3 ras_monitor.py --config_dir ../../model_configs/vllm --coordinator-ip <Coordinator IP> > ras_monitor_result.txt 2>&1 &
```

`--coordinator-ip` 请填写部署 coordinator 容器的宿主机合法 IP（IPv4 或 IPv6）。非法地址会立即报错退出，不会进入探活等待。Docker 场景确认服务异常后，ras_monitor 会把观察结果写入日志并退出，不会调用 K8s 的 delete/deploy 重拉。

RBG 部署场景（`deploy_mode: rbg`）下，ras_monitor 判定故障后的重拉仍执行旧 `delete.sh`，再调用
`deploy.py`。旧脚本按本地 YAML 删除资源，依赖本地部署产物；它不使用 RBG 专用清理流程，也不具有
该流程的清理顺序和数据 PVC 保留语义，因此不作为 RBG 的推荐恢复入口。

RBG 整组重新部署应先通过 TUI 删除，或执行
`MOTOR_DEPLOY_MODE=rbg bash examples/cloud_native_deploy/cleanup.sh <namespace>` 完成清理，再按原配置
部署，见 [RBG 配置更新与清理](../../../deployer/lib/rbg/README.md#配置更新与清理)。
`kubectl delete pod` 只删除并补建目标 Pod，不能保证重启整个多 Pod 引擎实例；若需整实例重建，应针对
对应的 `RoleInstance` 操作。默认 `RecreateRoleInstanceOnPodRestart` 用于 Pod 失败或容器异常重启时的
实例联动重建，满足 DP 降级条件的 Decode/Union 默认使用 `None`。

## 说明

### 参数说明

由于故障发生一段时间后，ras_monitor 执行服务重拉时将调用 deploy.py，上述2.2中 ras_monitor 的输入参数建议与服务拉起时执行 deploy.py 脚本的输入参数保持一致，否则可能导致重拉失败。
具体 deploy.py 的参数介绍见[链接](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/README.md) 。

自动重拉时，ras_monitor 会检查 `deploy.py` 是否支持 `--skip-check`，并在支持时自动追加该参数。首次手动启动仍应按[部署前安全预检](../../../../docs/zh/user_guide/deployment/k8s/README.md#deployment-security-precheck)准备配置。

### 其他

1、由于 ras_monitor 的定位为大 EP 的健康伴侣，与大 EP 的启动执行脚本 deploy.py 解耦。若 ras_monitor 先启动，用户需在 `examples/deployer` 目录下手动执行 `python3 deploy.py --config_dir ../../model_configs/vllm`（或使用 `--user_config_path` 与 `--env_config_path` 指定配置文件）拉起服务后，ras_monitor 才进入监控流程，否则将一直等待服务拉起并 ready。

首次启动时若遇到安全预检提示，请参考[部署前安全预检](../../../../docs/zh/user_guide/deployment/k8s/README.md#deployment-security-precheck)。仅在隔离测试环境需要沿用现有配置时，可在手动执行的 `deploy.py` 命令末尾追加 `--skip-check`。

2、若在 ras_monitor 监控过程中客户有修改配置的诉求，若在执行 `bash delete` 删除服务后，客户未终止ras_monitor 进程，ras_monitor 作为自动化脚本可能会误判认为服务异常，执行重拉。因此，建议在 `bash delete` 执行后，手动停止ras_monitor 进程以防止误重启。

### 实例状态故障判定

ras_monitor 通过 Coordinator Observability 端口的 `GET /instances` 查询实例状态。容器内或
Docker-only 部署使用 `coordinator_obs_port`（默认 `1027`）；Kubernetes 集群外的自研工具使用
`mindie-motor-coordinator-obs` 的 NodePort（默认 `31017`）。启用 `mgmt_api_key_config` 时，
ras_monitor 会从 `api_key_file` 读取密钥并携带 `X-Motor-Management-Key` 请求头。

查询结果包含至少一个实例，且所有实例的 `healthy` 均严格为 `false` 时，ras_monitor 会间隔一个
轮询周期（默认 10 秒）再次确认。只有连续两次查询结果均为全部不健康，才进入既有故障处理流程：
Kubernetes 部署执行服务重拉，Docker-only 部署记录故障后退出。任意一次查询出现健康实例、空实例
列表、请求失败、响应格式错误或缺少 `healthy` 字段，都会中断连续计数，不会按“全部不健康”处理。
