<h1 align="center">MindIE Motor</h1>

<p align="center">昇腾上的推理调度层。请求从 Coordinator 进来，Prefill 和 Decode 可以拆开，也可以混部。<br>引擎对接 vLLM-Ascend 与 SGLang。</p>

<div align="center">

[![release](https://img.shields.io/badge/release-3.2.0-1B3A4B)](./docs/zh/release_note_motor.md)
[![license](https://img.shields.io/badge/license-MulanPSL__2-1B3A4B)](./LICENSE.md)
[![docs](https://img.shields.io/badge/docs-online-1B3A4B)](https://mindie-motor.readthedocs.io/)
[![Zread](https://img.shields.io/badge/Zread-Ask_AI-3E6B89)](https://zread.ai/Ascend/MindIE-Motor)
[![DeepWiki](https://img.shields.io/badge/DeepWiki-Ask_AI-3E6B89)](https://deepwiki.com/Ascend/MindIE-Motor)

</div>

## 这一版

3.2.0 于 2026-10-15 发布，配套和兼容性见 [版本配套说明](./docs/zh/release_note_motor.md)。仓库在 2026 年 3 月开源。

| 变化 | 说明 |
| --- | --- |
| SGLang | 对接 SGLang 原生引擎，提供 PD 分离推理 |
| 权重拉起 | D2D 权重加载，新实例可从同角色就绪实例拉权重 |
| 部署面 | Docker only 一键部署，以及 Slurm + Apptainer |
| 拓扑 | 跨超节点、PD 异构，大 EP 下 DP 域可缩容运行 |
| 调度 | 基于 HPA 的自动扩缩容；Prefill 与 Decode 可分别配置调度策略 |
| 数据 | 数据混淆推理（PMCC） |
| POC | 长短序列动态分桶；KV Cache 亲和调度增强，支持 HBM / CPU / Disk 命中 |

## 请求怎么走

客户端只打 Coordinator 的北向接口。Coordinator 负责路由和负载，Controller 管实例身份和故障动作，NodeManager 在计算节点拉起引擎进程。

<p align="center">
  <img alt="MindIE Motor 组件关系" src="./docs/zh/imgs/MindIE_Motor_Architecture.png" width="920">
</p>

图中仍画有 EngineServer。3.2.0 已去掉该模块，NodeManager 直接拉起 vLLM-Ascend 或 SGLang。组件说明见 [架构](./docs/zh/architecture.md)。

## 先选部署

| 场景 | 适合 | 从这里进入 |
| --- | --- | --- |
| K8s，PD 分离 | Prefill 与 Decode 分实例，要独立扩容 | [PD 分离服务部署](./docs/zh/user_guide/deployment/k8s/pd_disaggregation_deployment.md) |
| K8s，PD 混部 | 同一实例同时做 Prefill 和 Decode | [PD 混部服务部署](./docs/zh/user_guide/deployment/k8s/pd_aggregation_deployment.md) |
| K8s，SGLang | 引擎用 SGLang，流程与通用 PD 分离相同，文档只写必改项 | [SGLang PD 分离部署](./docs/zh/user_guide/deployment/k8s/pd_disaggregation_sglang.md) |
| 只有 Docker | 单机小模型、宿主机起容器，或云平台只给容器 | [部署方式](./docs/zh/user_guide/deployment/README.md) |
| Slurm | HPC 集群，用 Apptainer 提交作业 | [Slurm 部署](./docs/zh/user_guide/deployment/slurm/README.md) |
| 只要调度面 | 引擎已在跑，不部署 Controller 和 NodeManager | [Coordinator 独立部署](./docs/zh/user_guide/deployment/standalone.md) |

K8s 的模式对照见 [部署模式说明](./docs/zh/user_guide/deployment/k8s/README.md)。集群里用 Job 跑部署器时，用 [Helm Chart](./examples/cloud_native_deploy/README.zh.md)。

## 打开哪项能力

引擎和部署方式是否覆盖，以 [特性支持列表](./docs/zh/user_guide/features/supported_features.md) 为准。引擎参数见 [支持对接的推理引擎](./docs/zh/user_guide/features/supported_inference_engines.md)。

| 想做的事 | 文档 |
| --- | --- |
| 拆开 Prefill / Decode | [PD 分离](./docs/zh/user_guide/features/pd_disaggregation.md) |
| 编码、Prefill、Decode 三段拆开 | [EPD 分离](./docs/zh/user_guide/features/EPD_disaggregation.md) |
| 按前缀命中把请求送到已有缓存的实例 | [KV Cache 亲和](./docs/zh/user_guide/features/kvcache_affinity.md) |
| 长短请求分桶，减轻互相拖死 | [动态分桶](./docs/zh/user_guide/features/dynamic_bucket.md) |
| 把 KV 放到节点外的池子 | [KV 池化](./docs/zh/user_guide/features/kv_cache_store/README.md) |
| 按真实输入长度裁剪 max_tokens | [max_tokens 自适应](./docs/zh/user_guide/features/max_tokens_adaptation.md) |
| 流式中途换实例续写 | [请求重调度](./docs/zh/user_guide/features/fault_tolerance/rescheduler.md) |
| 整实例迁走 | [实例重调度](./docs/zh/user_guide/features/fault_tolerance/instance_rescheduling.md) |
| Decode 故障时用 Prefill 节点顶上 | [ScaleP2D](./docs/zh/user_guide/features/fault_tolerance/scale_p2d.md) |
| Controller / Coordinator 主备 | [主备倒换](./docs/zh/user_guide/features/fault_tolerance/standby.md) |
| 按指标增减实例 | [自动扩缩容](./docs/zh/user_guide/features/auto_scaling.md)、[手动扩缩容](./docs/zh/user_guide/features/manual_scaling.md) |
| 新实例少等权重加载 | [D2D 权重加载](./docs/zh/user_guide/features/startup_acceleration.md)、[容器快照](./docs/zh/user_guide/features/container_snapshot.md) |
| 权重和词表做了混淆 | [数据混淆推理](./docs/zh/user_guide/features/data_obfuscation.md) |
| 抽样看输出是否漂了 | [精度检测](./docs/zh/user_guide/features/precision_detection.md) |

接口、维护和错误码在 [用户指南目录](./docs/zh/user_guide/menu_user_manual.md)。

## 第一次部署

1. 核对软硬件：[环境准备](./docs/zh/user_guide/environment_preparation.md)。
2. 取镜像。官方包在 [昇腾镜像仓库](https://www.hiascend.com/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4) 的「镜像下载」。也可以在 vLLM-Ascend 或 SGLang 基座上安装，见 [准备 MindIE Motor 镜像](./docs/zh/user_guide/maintenance/build_motor_image_from_vllm_ascend.md)。
3. 走完启动、调用、精度与性能测试、停止：[快速部署](./docs/zh/user_guide/quick_start.md)。

在线文档：<https://mindie-motor.readthedocs.io/>

## 反馈和改代码

使用问题先查 [Issues](https://gitcode.com/Ascend/MindIE-Motor/issues)，没有相同的再 [新建](https://gitcode.com/Ascend/MindIE-Motor/issues/create/choose)，写上现象、步骤、日志和环境。安全问题不要发公开 Issue，按 [security.md](./security.md) 联系维护者。

提交代码见 [贡献者指南](./contributing.md)。提交前运行 [tests/run_tests.sh](./tests/run_tests.sh)。Pull Request 建好后，在评论里回复 `compile` 触发门禁。

会议在 [Ascend 会议中心](https://meeting.ascend.osinfra.cn/)，讨论在 [昇腾论坛](https://www.hiascend.com/forum/)。许可证是 [Mulan PSL v2](./LICENSE.md)。
