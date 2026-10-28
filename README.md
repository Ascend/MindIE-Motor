# MindIE Motor

<p align="center">
    <img alt="MindIE Motor" src="./docs/zh/imgs/MindIE-Motor.png">
</p>

<p align="center">
    <a href="https://mindie-motor.readthedocs.io/">在线文档</a>
    &nbsp;·&nbsp;
    <a href="./docs/zh/user_guide/quick_start.md">快速部署</a>
    &nbsp;·&nbsp;
    <a href="./docs/zh/release_note_motor.md">3.2.0</a>
    &nbsp;·&nbsp;
    <a href="./LICENSE.md">Mulan PSL v2</a>
    &nbsp;·&nbsp;
    <a href="https://zread.ai/Ascend/MindIE-Motor">Zread</a>
    &nbsp;·&nbsp;
    <a href="https://deepwiki.com/Ascend/MindIE-Motor">DeepWiki</a>
</p>

MindIE Motor 是昇腾上的推理服务调度层。外部请求进 Coordinator，按负载分到 Prefill 和 Decode；两者也可以混部在同一实例上。引擎侧对接 [vLLM-Ascend](https://github.com/vllm-project/vllm-ascend) 和 [SGLang](https://github.com/sgl-project/sglang)。K8s、Docker、Slurm 都能把这套服务拉起来。

<p align="center">
    <img alt="请求从 Coordinator 进入，Controller 管理实例，NodeManager 拉起 vLLM 或 SGLang" src="./docs/zh/imgs/MindIE_Motor_Architecture.png" width="920">
</p>

## 从这里开始

1. 看机器和软件是否齐：[环境准备](./docs/zh/user_guide/environment_preparation.md)。
2. 取镜像。官方包在 [昇腾镜像仓库](https://www.hiascend.com/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4) 的「镜像下载」。也可以在 vLLM-Ascend 或 SGLang 基座镜像上自己装，见 [准备 MindIE Motor 镜像](./docs/zh/user_guide/maintenance/build_motor_image_from_vllm_ascend.md)。
3. 走一遍启动、调用、精度和性能测试、停服：[快速部署](./docs/zh/user_guide/quick_start.md)。

已有 Kubernetes 时，可以用 [Helm Chart](./examples/cloud_native_deploy/README.zh.md) 把部署器放进集群。

## 按环境往下看

| 你现在的环境 | 文档 |
| --- | --- |
| K8s，vLLM，Prefill / Decode 分开 | [PD 分离服务部署](./docs/zh/user_guide/deployment/k8s/pd_disaggregation_deployment.md) |
| K8s，vLLM，Prefill / Decode 混部 | [PD 混部服务部署](./docs/zh/user_guide/deployment/k8s/pd_aggregation_deployment.md) |
| K8s，SGLang | [SGLang PD 分离服务部署](./docs/zh/user_guide/deployment/k8s/pd_disaggregation_sglang.md) |
| 只有 Docker | [部署方式](./docs/zh/user_guide/deployment/README.md) 里的单容器、宿主机、容器内三条路径 |
| Slurm + Apptainer | [Slurm 部署](./docs/zh/user_guide/deployment/slurm/README.md) |
| 引擎已经在跑，只要调度面 | [Coordinator 独立部署](./docs/zh/user_guide/deployment/standalone.md) |

K8s 的模式怎么选，见 [部署模式说明](./docs/zh/user_guide/deployment/k8s/README.md)。

## 请求进来之后

调度、亲和、池化和扩缩容的入口都在用户指南里。某项能力是否覆盖 vLLM-Ascend、SGLang，以及 K8s 还是 Docker，以 [特性支持列表](./docs/zh/user_guide/features/supported_features.md) 为准。引擎参数怎么填，见 [支持对接的推理引擎](./docs/zh/user_guide/features/supported_inference_engines.md)。

常用的几条：

- 流量怎么拆：[PD 分离](./docs/zh/user_guide/features/pd_disaggregation.md)、[EPD 分离](./docs/zh/user_guide/features/EPD_disaggregation.md)、[KV Cache 亲和](./docs/zh/user_guide/features/kvcache_affinity.md)、[动态分桶](./docs/zh/user_guide/features/dynamic_bucket.md)、[KV 池化](./docs/zh/user_guide/features/kv_cache_store/README.md)、[max_tokens 自适应](./docs/zh/user_guide/features/max_tokens_adaptation.md)
- 实例挂了怎么办：[请求重调度](./docs/zh/user_guide/features/fault_tolerance/rescheduler.md)、[实例重调度](./docs/zh/user_guide/features/fault_tolerance/instance_rescheduling.md)、[ScaleP2D](./docs/zh/user_guide/features/fault_tolerance/scale_p2d.md)、[主备倒换](./docs/zh/user_guide/features/fault_tolerance/standby.md)、[自动扩缩容](./docs/zh/user_guide/features/auto_scaling.md)、[手动扩缩容](./docs/zh/user_guide/features/manual_scaling.md)
- 启动和数据：[D2D 权重加载](./docs/zh/user_guide/features/startup_acceleration.md)、[容器快照](./docs/zh/user_guide/features/container_snapshot.md)、[数据混淆推理](./docs/zh/user_guide/features/data_obfuscation.md)、[精度检测](./docs/zh/user_guide/features/precision_detection.md)

组件各自干什么，见 [架构说明](./docs/zh/architecture.md)。接口、维护和错误码在 [用户指南目录](./docs/zh/user_guide/menu_user_manual.md)。

## 3.2.0

2026-10-15 发布。组件配套和兼容性见 [版本配套说明](./docs/zh/release_note_motor.md)。这一版主要加上：

- SGLang 上的 PD 分离
- D2D 权重加载，新实例可以从同角色就绪实例拉权重
- Docker only 一键部署，以及 Slurm + Apptainer
- 跨超节点、PD 异构，以及大 EP 下的 DP 域缩容
- 基于 HPA 的自动扩缩容；Prefill 和 Decode 可以各用各的调度策略
- 数据混淆推理（PMCC）
- 长短序列动态分桶，以及 KV Cache 亲和调度增强（这两项是 POC）

仓库在 2026 年 3 月开源。

## 问题和改动

使用问题先搜 [Issues](https://gitcode.com/Ascend/MindIE-Motor/issues)。没有相同的，再 [新建一条](https://gitcode.com/Ascend/MindIE-Motor/issues/create/choose)，写上现象、复现步骤、日志和环境。安全问题不要发到公开 Issue，按 [security.md](./security.md) 联系维护者。

改代码的流程见 [贡献者指南](./contributing.md)。提交前跑 [tests/run_tests.sh](./tests/run_tests.sh)。Pull Request 开出来之后，在评论里回复 `compile` 才会跑门禁。

会议安排在 [Ascend 会议中心](https://meeting.ascend.osinfra.cn/)，讨论在 [昇腾论坛](https://www.hiascend.com/forum/)。许可证是 [Mulan PSL v2](./LICENSE.md)。
