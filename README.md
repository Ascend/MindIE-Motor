<!-- The complete wordmark below serves as the README title. -->
<!-- markdownlint-disable-file MD041 -->

<p align="center">
    <img alt="MindIE Motor" src="./docs/zh/assets/mindie-motor-logo.svg" width="100%">
</p>

<div align="center">

<a href="https://www.hiascend.com/forum/"><img alt="Community: MindIE Motor" src="./docs/zh/assets/badges/community.svg" width="218" height="28"></a>
<a href="./LICENSE.md"><img alt="License: MulanPSL 2" src="./docs/zh/assets/badges/license.svg" width="174" height="28"></a>
<a href="https://mindie-motor.readthedocs.io/"><img alt="Docs: Read the Docs" src="./docs/zh/assets/badges/docs.svg" width="176" height="28"></a>
<a href="https://zread.ai/Ascend/MindIE-Motor"><img alt="Zread: Ask AI" src="./docs/zh/assets/badges/zread.svg" width="147" height="28"></a>
<a href="https://deepwiki.com/Ascend/MindIE-Motor"><img alt="DeepWiki: Ask AI" src="./docs/zh/assets/badges/deepwiki.svg" width="172" height="28"></a>

</div>

## ✨ 最新消息

- [2026.11.11]: 🚀 MindIE Motor 3.3.0b1 发布，配套 vLLM-Ascend 0.32 RC 版本
- [2026.10.28]: 🚀 MindIE Motor 3.2.0 发布，配套 vLLM-Ascend 0.30.0 版本
- [2026.10.28]: 🚀 优化 Docker only 部署方案，部署流程大幅简化
- [2026.10.28]: 🚀 支持950系列服务器部署跨超节点的推理服务
- [2026.10.28]: 🚀 支持故障场景下的卡级缩容，故障卡退出服务，其余卡继续推理
- [2026.10.28]: 🚀 支持基于 HPA 的自动扩缩容，按业务流量调整 Prefill 与 Decode 实例配比
- [2026.08.19]: 🚀 MindIE Motor 3.1.0 发布，配套 vLLM-Ascend 0.23.0 版本
- [2026.08.19]: 🚀 代码仓由 MindIE PyMotor 更名为 MindIE Motor
- [2026.08.19]: 🚀 支持 SGLang 推理引擎
- [2026.08.19]: 🚀 支持对接 MemCache 池化后端，提高KV Cache缓存利用率
- [2026.08.19]: 🚀 支持在 Ascend 950 系列产品上部署推理服务
- [2026.08.19]: 🚀 支持 D2D 权重直传，重启引擎从内存加载权重，缩短服务启动时间
- [2026.03]: 🚀 MindIE Motor 正式开源

## ℹ️ 简介

MindIE Motor 是面向大语言模型分布式推理的请求调度框架，提供一键式 PD 分离与 PD 混部部署。它基于云原生插件化架构，对接 [vLLM-Ascend](https://github.com/vllm-project/vllm-ascend) 与 [SGLang](https://github.com/sgl-project/sglang) 原生引擎，结合高性能调度与负载均衡，构建高可用、可扩展的大规模推理服务。

<a href="./docs/zh/imgs/MindIE-Motor-architecture-zh.png" title="点击查看大图">
<img src="./docs/zh/imgs/MindIE-Motor-architecture-zh.png" alt="MindIE Motor 架构" width="100%"/>
</a>

## 🗺️ 路线图 (roadmap)

- 2026 Q3：[Issue链接](https://gitcode.com/Ascend/MindIE-Motor/issues/350) ✅（已完成）
- 2026 Q4：[Issue链接](https://gitcode.com/Ascend/MindIE-Motor/issues/849) 🚧（开发中）

## 🖥️ 服务部署

<table>
  <tr>
    <td valign="top">
      <table>
        <tr><th style="font-size:18px" align="center" colspan="2">🎯 部署指导</th></tr>
        <tr><th align="left" colspan="2">K8s 部署</th></tr>
        <tr><td align="left" width="320" style="background:#F0EDFF">【推荐】PD 分离（vLLM）</td><td align="center" width="72" style="background:#F0EDFF"><a href="./docs/zh/user_guide/deployment/README.md#k8s-pd-disaggregation">链接</a></td></tr>
        <tr><td align="left" width="320" style="background:#F0EDFF">【推荐】PD 分离（SGLang）</td><td align="center" width="72" style="background:#F0EDFF"><a href="./docs/zh/user_guide/deployment/README.md#k8s-pd-disaggregation-sglang">链接</a></td></tr>
        <tr><td align="left" width="320">PD 混部</td><td align="center" width="72"><a href="./docs/zh/user_guide/deployment/k8s/pd_aggregation_deployment.md">链接</a></td></tr>
        <tr><th align="left" colspan="2">docker 部署</th></tr>
        <tr><td align="left" width="320" style="background:#F0EDFF">【推荐】PD分离部署</td><td align="center" width="72" style="background:#F0EDFF"><a href="./docs/zh/user_guide/deployment/docker/host/pd_disaggregation_deployment.md">链接</a></td></tr>
        <tr><td align="left" width="320">PD混部部署</td><td align="center" width="72"><a href="./docs/zh/user_guide/deployment/docker/host/pd_aggregation_deployment.md">链接</a></td></tr>
        <tr><td align="left" width="320">云平台（例如：ModelArts）PD分离部署</td><td align="center" width="72"><a href="./docs/zh/user_guide/deployment/docker/container/pd_disaggregation_deployment.md">链接</a></td></tr>
        <tr><td align="left" width="320">云平台（例如：ModelArts）PD混部部署</td><td align="center" width="72"><a href="./docs/zh/user_guide/deployment/docker/container/pd_aggregation_deployment.md">链接</a></td></tr>
        <tr><th align="left" colspan="2">Slurm 部署</th></tr>
        <tr><td align="left" width="320">Slurm</td><td align="center" width="72"><a href="./docs/zh/user_guide/deployment/slurm/README.md">链接</a></td></tr>
        <tr><th align="left" colspan="2">调度器独立部署</th></tr>
        <tr><td align="left" width="320">Coordinator 独立部署</td><td align="center" width="72"><a href="./docs/zh/user_guide/deployment/standalone.md">链接</a></td></tr>
      </table>
    </td>
    <td class="gap" width="24" style="border:none;padding:0;background:transparent"><img alt="" src="./docs/zh/imgs/gap.png" width="24" height="1"></td>
    <td valign="top">
      <table>
         <tr><th style="font-size:18px" align="center" colspan="5">🤖 常用模型</th></tr>
        <tr>
          <th align="left">模型</th>
          <th align="center">Atlas 800I A2</th>
          <th align="center">Atlas 800I A3 超节点</th>
          <th align="center">Atlas 950系列服务器</th>
          <th align="center">适配状态</th>
        </tr>
        <tr><td>DeepSeek-V4.1-Flash</td><td align="center"><a href="./model_configs/vllm/models/deepseek_v4.1_flash/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/deepseek_v4.1_flash/A3">A3</a></td><td align="center"><a href="./model_configs/vllm/models/deepseek_v4.1_flash/A5">A5</a></td><td align="center">已完成</td></tr>
        <tr><td>DeepSeek-V4-Pro</td><td align="center"><a href="./model_configs/vllm/models/deepseek_v4_pro/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/deepseek_v4_pro/A3">A3</a></td><td align="center"><a href="./model_configs/vllm/models/deepseek_v4_pro/A5">A5</a></td><td align="center">已完成</td></tr>
        <tr><td>DeepSeek-V4-Flash</td><td align="center"><a href="./model_configs/vllm/models/deepseek_v4_flash/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/deepseek_v4_flash/A3">A3</a></td><td align="center"><a href="./model_configs/vllm/models/deepseek_v4_flash/A5">A5</a></td><td align="center">已完成</td></tr>
        <tr><td>DeepSeek-V3.1</td><td align="center"><a href="./model_configs/vllm/models/deepseek_v3.1/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/deepseek_v3.1/A3">A3</a></td><td align="center"><a href="./model_configs/vllm/models/deepseek_v3.1/A5">A5</a></td><td align="center">已完成</td></tr>
        <tr><td>GLM-5.3-Flash</td><td align="center"><a href="./model_configs/vllm/models/glm_5.3_flash/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/glm_5.3_flash/A3">A3</a></td><td align="center"><a href="./model_configs/vllm/models/glm_5.3_flash/A5">A5</a></td><td align="center">已完成</td></tr>
        <tr><td>GLM-5.3</td><td align="center"><a href="./model_configs/vllm/models/glm_5.3/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/glm_5.3/A3">A3</a></td><td align="center"><a href="./model_configs/vllm/models/glm_5.3/A5">A5</a></td><td align="center">已完成</td></tr>
        <tr><td>GLM-5.2</td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td><td align="center">待适配</td></tr>
        <tr><td>GLM-5.1</td><td align="center"><a href="./model_configs/vllm/models/glm_5.1/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/glm_5.1/A3">A3</a></td><td align="center"><a href="./model_configs/vllm/models/glm_5.1/A5">A5</a></td><td align="center">已完成</td></tr>
        <tr><td>GLM-5</td><td align="center"><a href="./model_configs/vllm/models/glm_5/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/glm_5/A3">A3</a></td><td align="center"><a href="./model_configs/vllm/models/glm_5/A5">A5</a></td><td align="center">已完成</td></tr>
        <tr><td>Kimi K3.1 PRO</td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td><td align="center">待适配（预计12月份）</td></tr>
        <tr><td>MiniMax-M3.1-Flash</td><td align="center"><a href="./model_configs/vllm/models/minimax_m3.1_flash/A2">A2</a></td><td align="center">A3</td><td align="center">A5</td><td align="center">A2已完成</td></tr>
        <tr><td>Qwen3.8-Flash-Next</td><td align="center"><a href="./model_configs/vllm/models/qwen3.8_flash_next/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/qwen3.8_flash_next/A3">A3</a></td><td align="center"><a href="./model_configs/vllm/models/qwen3.8_flash_next/A5">A5</a></td><td align="center">已完成</td></tr>
       <tr><td>Qwen3-235B</td><td align="center"><a href="./model_configs/vllm/models/qwen_235b/A2">A2</a></td><td align="center"><a href="./model_configs/vllm/models/qwen_235b/A3">A3</a></td><td align="center">—</td><td align="center">已完成</td></tr>
        <tr><td colspan="5">适配状态以本表中的 MindIE Motor 配置链接为准。</td></tr>
      </table>
    </td>
  </tr>
</table>

## ⚙️ 功能介绍

引擎与部署方式的覆盖范围见 [特性支持列表](./docs/zh/user_guide/features/supported_features.md)。下表只列主要能力入口。

<table>
  <tr>
    <td valign="top">
      <table>
        <tr><th style="font-size:18px" align="center" colspan="2">⚡ 极致性能</th></tr>
        <tr><td align="left" width="320">PD 分离</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/pd_disaggregation.md">链接</a></td></tr>
        <tr><td align="left" width="320">EPD 分离（多模态）</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/EPD_disaggregation.md">链接</a></td></tr>
        <tr><td align="left" width="320">KV Cache 亲和性调度</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/kvcache_affinity.md">链接</a></td></tr>
        <tr><td align="left" width="320">Agent Hint 会话亲和与调度</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/agent_hint.md">链接</a></td></tr>
        <tr><td align="left" width="320">动态分桶调度</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/dynamic_bucket.md">链接</a></td></tr>
        <tr><td align="left" width="320">Mooncake KV Cache 池化后端</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/kv_cache_store/mooncake.md">链接</a></td></tr>
        <tr><td align="left" width="320">MemCache KV Cache 池化后端</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/kv_cache_store/memcache.md">链接</a></td></tr>
        <tr><td align="left" width="320">Yuanrong KV Cache 池化后端</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/kv_cache_store/yuanrong.md">链接</a></td></tr>
        <tr><td align="left" width="320">UCM KV Cache 池化后端</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/kv_cache_store/ucm.md">链接</a></td></tr>
        <tr><td align="left" width="320">max_tokens 自适应</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/inference_scheduling/max_tokens_adaptation.md">链接</a></td></tr>
      </table>
    </td>
    <td class="gap" width="24" style="border:none;padding:0;background:transparent"><img alt="" src="./docs/zh/imgs/gap.png" width="24" height="1"></td>
    <td valign="top">
      <table>
        <tr><th style="font-size:18px" align="center" colspan="2">🛡️ 稳定可靠</th></tr>
        <tr><td align="left" width="320">请求重调度</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/fault_tolerance/request_rescheduler.md">链接</a></td></tr>
        <tr><td align="left" width="320">缩P保D</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/fault_tolerance/scale_p2d.md">链接</a></td></tr>
        <tr><td align="left" width="320">主备倒换</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/fault_tolerance/standby.md">链接</a></td></tr>
        <tr><td align="left" width="320">异常实例重启</td><td align="center" width="72"><a href="./docs/zh/design/fault_tolerance/overview.md">链接</a></td></tr>
        <tr><td align="left" width="320">虚推健康探测</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/fault_tolerance/sim_inference.md">链接</a></td></tr>
        <tr><td align="left" width="320">服务限流</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/fault_tolerance/rate_limiting.md">链接</a></td></tr>
        <tr><td align="left" width="320">RAS monitor 服务健康监控</td><td align="center" width="72"><a href="./examples/features/fault_tolerance/ras_monitor/readme.md">链接</a></td></tr>
        <tr><td align="left" width="320">自动弹性扩缩容</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/scaling_deployment/auto_scaling.md">链接</a></td></tr>
        <tr><td align="left" width="320">手动扩缩容</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/scaling_deployment/manual_scaling.md">链接</a></td></tr>
        <tr><td align="left" width="320">容器快照</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/scaling_deployment/container_snapshot.md">链接</a></td></tr>
        <tr><td align="left" width="320">D2D 权重加载</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/scaling_deployment/startup_acceleration.md">链接</a></td></tr>
        <tr><td align="left" width="320">数据混淆推理（PMCC）</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/maintenance_testing_security/data_obfuscation.md">链接</a></td></tr>
        <tr><td align="left" width="320">精度检测</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/maintenance_testing_security/precision_detection.md">链接</a></td></tr>
        <tr><td align="left" width="320">请求耗时追踪</td><td align="center" width="72"><a href="./docs/zh/user_guide/features/maintenance_testing_security/tracing.md">链接</a></td></tr>
      </table>
    </td>
  </tr>
</table>

## 🚀 快速入门

- **镜像获取**：单击 [链接](https://www.hiascend.com/developer/ascendhub/detail/f1690465f39847a8b0a1f9e5b36a03c4)，切换至“镜像下载”页签，即可获取官方完整的 MindIE Motor 镜像。
- **环境准备**：安装前的软硬件准备见 [环境准备](./docs/zh/user_guide/environment_preparation.md)。
- **快速部署**：启动服务、接口调用、精度与性能测试、停止服务见 [快速部署](./docs/zh/user_guide/quick_start.md)。

## 📦 安装指南

### 硬件配置

**节点说明**

- **通算节点**：以 CPU 为核心，通用性强，负责实例管理、请求调度等常规逻辑任务。
- **智算节点**：以 NPU / GPU 等 AI 芯片为核心，擅长大规模并行计算，负责模型推理等重负载 AI 任务。
- **协作方式**：通算节点负责实例管理与请求调度，智算节点负责模型推理，二者分工协作。通算节点为可选项；未配置时，Controller、Coordinator 和推理引擎均运行在智算节点。

> **部署建议**：推理引擎（如 vLLM）部署在智算节点；Controller 和 Coordinator 优先部署在通算节点。

**配置要求**

| 节点类型 | 配置项 | 推荐配置 | 最低要求 |
| --- | --- | --- | --- |
| 通算 | CPU 核心数与主频 | 2 × 48 核，2.6 GHz | 20 核 |
| 通算 | 内存（DRAM） | 8 × 32 GB（总计 256 GB） | 8 GB |
| 智算 | CPU 核心数与主频 | 128 核 | 64 核 |
| 智算 | 内存（DRAM） | 1,000 GB | 256 GB |
| 通算、智算 | 硬盘 | 12 × 1,920 GB SATA SSD（6 Gb/s） | 无明确限制 |

> 通算 CPU 最低要求按实例数计算。每个 Controller 实例需 4 核，每个 Coordinator 实例需 16 核，各部署 1 个实例时合计 20 核。

### 版本说明

| 组件 | 版本要求 | 说明 |
| --- | --- | --- |
| Docker | 24.x.x 或更高版本 | 宿主机容器 |
| Python | 3.11 或更高版本 | 宿主机运行环境 |
| Kubernetes | 1.17.x 或更高版本 | 集群编排平台 |

> 宿主机需安装 Docker 与 Python；集群需运行 Kubernetes。

### 镜像安装

基于 vLLM-Ascend 或 SGLang 基座镜像的构建步骤见 [准备 MindIE Motor 镜像](./docs/zh/user_guide/maintenance/build_motor_image_from_vllm_ascend.md)。

## 🔀 版本说明

当前稳定版本为 3.2.0。组件配套、兼容性和更新说明见 [版本配套说明](./docs/zh/release_note_motor.md)。

## 🛠️ 贡献指南

贡献流程见 [贡献者指南](./contributing.md)。

安全问题请不要写进公开 Issue，按 [security.md](./security.md) 联系维护者。

## 🤝 联系我们

如有任何社区贡献想法（代码合入、Bug 反馈、issue进展跟踪、议题讨论等），欢迎加入社区交流群，或私聊社区管理员，我们将全程协助您完成贡献。

<table>
  <tr>
    <td align="center" width="33%">
      <img src="./docs/zh/imgs/community-repo-owner.jpg" alt="社区代码仓仓主" width="220"><br>
      <b>社区代码仓仓主</b>
    </td>
    <td align="center" width="33%">
      <img src="./docs/zh/imgs/community-wechat-group.jpg" alt="社区开源交流群" width="220"><br>
      <b>社区开源交流群</b>
    </td>
    <td align="center" width="33%">
      <img src="./docs/zh/imgs/community-admin.jpg" alt="社区管理员" width="220"><br>
      <b>社区管理员</b>
    </td>
  </tr>
</table>

MindIE 系列 TC/SIG 会议见 [Ascend 会议中心](https://meeting.ascend.osinfra.cn/)。技术交流见 [昇腾论坛](https://www.hiascend.com/forum/)。

## ⚖️ 许可说明

🔹 《[许可证声明](./LICENSE.md)》<br>
🔹 《[安全问题反馈](./security.md)》<br>
