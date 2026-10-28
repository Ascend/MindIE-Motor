<!-- markdownlint-disable-next-line MD041 -->
<p align="center">
    <img alt="MindIE Motor" src="./docs/zh/imgs/MindIE-Motor.png" width="560">
</p>

<p align="center">
    <a href="https://mindie-motor.readthedocs.io/">
        <img alt="Documentation" src="https://img.shields.io/badge/Docs-Read%20the%20Docs-8CA1AF">
    </a>
    <a href="./LICENSE.md">
        <img alt="License" src="https://img.shields.io/badge/License-Mulan-blue">
    </a>
    <a href="https://meeting.ascend.osinfra.cn/">
        <img alt="Meetings" src="https://img.shields.io/badge/Meetings-TC%2FSIG-0A7B83">
    </a>
    <a href="https://www.hiascend.com/forum/">
        <img alt="Forum" src="https://img.shields.io/badge/Forum-Ascend-F47B20">
    </a>
    <a href="https://zread.ai/Ascend/MindIE-Motor">
        <img alt="Zread" src="https://img.shields.io/badge/Zread-Ask_AI-_.svg?style=flat&color=0052D9&labelColor=000000&logo=data%3Aimage%2Fsvg%2Bxml%3Bbase64%2CPHN2ZyB3aWR0aD0iMTYiIGhlaWdodD0iMTYiIHZpZXdCb3g9IjAgMCAxNiAxNiIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTTQuOTYxNTYgMS42MDAxSDIuMjQxNTZDMS44ODgxIDEuNjAwMSAxLjYwMTU2IDEuODg2NjQgMS42MDE1NiAyLjI0MDFWNC45NjAxQzEuNjAxNTYgNS4zMTM1NiAxLjg4ODEgNS42MDAxIDIuMjQxNTYgNS42MDAxSDQuOTYxNTZDNS4zMTUwMiA1LjYwMDEgNS42MDE1NiA1LjMxMzU2IDUuNjAxNTYgNC45NjAxVjIuMjQwMUM1LjYwMTU2IDEuODg2NjQgNS4zMTUwMiAxLjYwMDEgNC45NjE1NiAxLjYwMDFaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00Ljk2MTU2IDEwLjM5OTlIMi4yNDE1NkMxLjg4ODEgMTAuMzk5OSAxLjYwMTU2IDEwLjY4NjQgMS42MDE1NiAxMS4wMzk5VjEzLjc1OTlDMS42MDE1NiAxNC4xMTM0IDEuODg4MSAxNC4zOTk5IDIuMjQxNTYgMTQuMzk5OUg0Ljk2MTU2QzUuMzE1MDIgMTQuMzk5OSA1LjYwMTU2IDE0LjExMzQgNS42MDE1NiAxMy43NTk5VjExLjAzOTlDNS42MDE1NiAxMC42ODY0IDUuMzE1MDIgMTAuMzk5OSA0Ljk2MTU2IDEwLjM5OTlaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik0xMy43NTg0IDEuNjAwMUgxMS4wMzg0QzEwLjY4NSAxLjYwMDEgMTAuMzk4NCAxLjg4NjY0IDEwLjM5ODQgMi4yNDAxVjQuOTYwMUMxMC4zOTg0IDUuMzEzNTYgMTAuNjg1IDUuNjAwMSAxMS4wMzg0IDUuNjAwMUgxMy43NTg0QzE0LjExMTkgNS42MDAxIDE0LjM5ODQgNS4zMTM1NiAxNC4zOTg0IDQuOTYwMVYyLjI0MDFDMTQuMzk4NCAxLjg4NjY0IDE0LjExMTkgMS42MDAxIDEzLjc1ODQgMS42MDAxWiIgZmlsbD0iI2ZmZiIvPgo8cGF0aCBkPSJNNCAxMkwxMiA0TDQgMTJaIiBmaWxsPSIjZmZmIi8%2BCjxwYXRoIGQ9Ik00IDEyTDEyIDQiIHN0cm9rZT0iI2ZmZiIgc3Ryb2tlLXdpZHRoPSIxLjUiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCIvPgo8L3N2Zz4K&logoColor=ffffff">
    </a>
    <a href="https://deepwiki.com/Ascend/MindIE-Motor">
        <img alt="DeepWiki" src="https://img.shields.io/badge/DeepWiki-Ask_AI-_.svg?style=flat&color=0052D9&labelColor=000000&logo=data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACwAAAAyCAYAAAAnWDnqAAAAAXNSR0IArs4c6QAAA05JREFUaEPtmUtyEzEQhtWTQyQLHNak2AB7ZnyXZMEjXMGeK/AIi+QuHrMnbChYY7MIh8g01fJoopFb0uhhEqqcbWTp06/uv1saEDv4O3n3dV60RfP947Mm9/SQc0ICFQgzfc4CYZoTPAswgSJCCUJUnAAoRHOAUOcATwbmVLWdGoH//PB8mnKqScAhsD0kYP3j/Yt5LPQe2KvcXmGvRHcDnpxfL2zOYJ1mFwrryWTz0advv1Ut4CJgf5uhDuDj5eUcAUoahrdY/56ebRWeraTjMt/00Sh3UDtjgHtQNHwcRGOC98BJEAEymycmYcWwOprTgcB6VZ5JK5TAJ+fXGLBm3FDAmn6oPPjR4rKCAoJCal2eAiQp2x0vxTPB3ALO2CRkwmDy5WohzBDwSEFKRwPbknEggCPB/imwrycgxX2NzoMCHhPkDwqYMr9tRcP5qNrMZHkVnOjRMWwLCcr8ohBVb1OMjxLwGCvjTikrsBOiA6fNyCrm8V1rP93iVPpwaE+gO0SsWmPiXB+jikdf6SizrT5qKasx5j8ABbHpFTx+vFXp9EnYQmLx02h1QTTrl6eDqxLnGjporxl3NL3agEvXdT0WmEost648sQOYAeJS9Q7bfUVoMGnjo4AZdUMQku50McDcMWcBPvr0SzbTAFDfvJqwLzgxwATnCgnp4wDl6Aa+Ax283gghmj+vj7feE2KBBRMW3FzOpLOADl0Isb5587h/U4gGvkt5v60Z1VLG8BhYjbzRwyQZemwAd6cCR5/XFWLYZRIMpX39AR0tjaGGiGzLVyhse5C9RKC6ai42ppWPKiBagOvaYk8lO7DajerabOZP46Lby5wKjw1HCRx7p9sVMOWGzb/vA1hwiWc6jm3MvQDTogQkiqIhJV0nBQBTU+3okKCFDy9WwferkHjtxib7t3xIUQtHxnIwtx4mpg26/HfwVNVDb4oI9RHmx5WGelRVlrtiw43zboCLaxv46AZeB3IlTkwouebTr1y2NjSpHz68WNFjHvupy3q8TFn3Hos2IAk4Ju5dCo8B3wP7VPr/FGaKiG+T+v+TQqIrOqMTL1VdWV1DdmcbO8KXBz6esmYWYKPwDL5b5FA1a0hwapHiom0r/cKaoqr+27/XcrS5UwSMbQAAAABJRU5ErkJggg==">
    </a>
</p>

## 简介

**MindIE Motor** 是昇腾上的大模型推理调度框架。它把外部请求接到 Prefill 与 Decode 上，两者可以分开部署，也可以混部；引擎侧对接 [vLLM-Ascend](https://github.com/vllm-project/vllm-ascend) 和 [SGLang](https://github.com/sgl-project/sglang)。

## 最新动态

- 10/15/2026：3.2.0 发布，对接 SGLang，提供 PD 分离推理 🚀
- 10/15/2026：D2D 权重加载，新实例可从同角色就绪实例拉权重 ⚡
- 10/15/2026：Docker only 一键部署，以及 Slurm + Apptainer 🧩
- 10/15/2026：跨超节点、PD 异构，大 EP 下 DP 域可缩容 ⚙️
- 10/15/2026：基于 HPA 的自动扩缩容，Prefill 与 Decode 可分别配置调度策略 📈
- 10/15/2026：数据混淆推理（PMCC）🔐
- 10/15/2026：长短序列动态分桶，KV Cache 亲和调度增强（POC）🧪
- 03/2026：MindIE Motor 开源 🎉

配套与兼容性见 [版本配套说明](./docs/zh/release_note_motor.md)。

## 架构

请求从 Coordinator 进入，Controller 管理实例和故障动作，NodeManager 在计算节点拉起引擎。组件说明见 [架构](./docs/zh/architecture.md)。

<p align="center">
    <img alt="MindIE Motor 架构" src="./docs/zh/imgs/MindIE_Motor_Architecture.png" width="920">
</p>

图中的 EngineServer 已在 3.2.0 删除，NodeManager 直接拉起 vLLM-Ascend 或 SGLang。

## 功能

引擎与部署方式是否覆盖，见 [特性支持列表](./docs/zh/user_guide/features/supported_features.md)。

**怎么部署**

| 方式 | 场景 | 文档 |
| --- | --- | --- |
| K8s PD 分离 | Prefill 与 Decode 分实例 | [PD 分离服务部署](./docs/zh/user_guide/deployment/k8s/pd_disaggregation_deployment.md) |
| K8s PD 混部 | 同一实例同时做 Prefill 和 Decode | [PD 混部服务部署](./docs/zh/user_guide/deployment/k8s/pd_aggregation_deployment.md) |
| K8s SGLang | 引擎用 SGLang，文档只写相对通用流程的必改项 | [SGLang PD 分离部署](./docs/zh/user_guide/deployment/k8s/pd_disaggregation_sglang.md) |
| Docker | 单容器、宿主机或容器内 | [部署方式](./docs/zh/user_guide/deployment/README.md) |
| Slurm | Apptainer 作业 | [Slurm 部署](./docs/zh/user_guide/deployment/slurm/README.md) |
| 只要调度面 | 引擎已在跑 | [Coordinator 独立部署](./docs/zh/user_guide/deployment/standalone.md) |

**调度**

| 能力 | 文档 |
| --- | --- |
| PD 分离 | [PD 分离](./docs/zh/user_guide/features/pd_disaggregation.md) |
| EPD 分离 | [EPD 分离](./docs/zh/user_guide/features/EPD_disaggregation.md) |
| KV Cache 亲和 | [KV Cache 亲和性调度](./docs/zh/user_guide/features/kvcache_affinity.md) |
| 动态分桶 | [动态分桶调度](./docs/zh/user_guide/features/dynamic_bucket.md) |
| KV 池化 | [KV 池化](./docs/zh/user_guide/features/kv_cache_store/README.md) |
| max_tokens 自适应 | [max_tokens 自适应](./docs/zh/user_guide/features/max_tokens_adaptation.md) |
| 推理引擎参数 | [支持对接的推理引擎](./docs/zh/user_guide/features/supported_inference_engines.md) |

**故障、扩缩容和数据**

| 能力 | 文档 |
| --- | --- |
| 请求重调度 | [请求重调度](./docs/zh/user_guide/features/fault_tolerance/rescheduler.md) |
| 实例重调度 | [实例重调度](./docs/zh/user_guide/features/fault_tolerance/instance_rescheduling.md) |
| ScaleP2D | [ScaleP2D 故障恢复](./docs/zh/user_guide/features/fault_tolerance/scale_p2d.md) |
| 主备倒换 | [主备倒换](./docs/zh/user_guide/features/fault_tolerance/standby.md) |
| 扩缩容 | [自动弹性扩缩容](./docs/zh/user_guide/features/auto_scaling.md)、[手动扩缩容](./docs/zh/user_guide/features/manual_scaling.md) |
| 容器快照 | [容器快照](./docs/zh/user_guide/features/container_snapshot.md) |
| D2D 权重加载 | [D2D 权重加载](./docs/zh/user_guide/features/startup_acceleration.md) |
| 数据混淆 | [数据混淆推理（PMCC）](./docs/zh/user_guide/features/data_obfuscation.md) |
| 精度检测 | [精度检测](./docs/zh/user_guide/features/precision_detection.md) |

## 快速入门

以一次完整调用为目标：准备环境，取镜像，拉起服务，打推理接口，再停掉。

1. [环境准备](./docs/zh/user_guide/environment_preparation.md)
2. 镜像从 [昇腾镜像仓库](https://www.hiascend.com/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4) 的「镜像下载」获取，或按 [准备 MindIE Motor 镜像](./docs/zh/user_guide/maintenance/build_motor_image_from_vllm_ascend.md) 装到 vLLM-Ascend / SGLang 基座上
3. [快速部署](./docs/zh/user_guide/quick_start.md)

在线文档：<https://mindie-motor.readthedocs.io/>

K8s 模式对照见 [部署模式说明](./docs/zh/user_guide/deployment/k8s/README.md)。要在集群里用 Helm 跑部署器，见 [云原生部署与 Helm Chart](./examples/cloud_native_deploy/README.zh.md)。

## 文档

- [用户指南目录](./docs/zh/user_guide/menu_user_manual.md)：部署、特性、API、维护、错误码
- [版本配套说明](./docs/zh/release_note_motor.md)：3.2.0 的组件版本和更新说明
- [贡献者指南](./contributing.md)：提交前运行 [tests/run_tests.sh](./tests/run_tests.sh)，Pull Request 评论里回复 `compile` 触发门禁

## 对接

运行时对接 [vLLM-Ascend](https://github.com/vllm-project/vllm-ascend) 与 [SGLang](https://github.com/sgl-project/sglang)。集群调度的配套版本写在 [版本配套说明](./docs/zh/release_note_motor.md) 里。

## 联系

使用问题先看 [Issues](https://gitcode.com/Ascend/MindIE-Motor/issues)，没有相同的再 [新建](https://gitcode.com/Ascend/MindIE-Motor/issues/create/choose)，写上现象、步骤、日志和环境。安全问题不要发到公开 Issue，按 [security.md](./security.md) 联系维护者。

会议在 [Ascend 会议中心](https://meeting.ascend.osinfra.cn/)，讨论在 [昇腾论坛](https://www.hiascend.com/forum/)。许可证是 [Mulan PSL v2](./LICENSE.md)。
