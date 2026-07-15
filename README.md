# MindIE Motor

<p align="center">
    <img alt="MindIE PyMotor" src="./docs/zh/imgs/mindie_pymotor_title.png">
</p>

<p align="center">
    <a href="./LICENSE.md">
        <img alt="License" src="https://img.shields.io/badge/License-Mulan-blue">
    </a>
    <a href="https://meeting.ascend.osinfra.cn/">
        <img alt="TC and SIG Meetings" src="https://img.shields.io/badge/Meetings-TC%2FSIG-0A7B83">
    </a>
    <a href="https://www.hiascend.com/forum/">
        <img alt="Ascend Forum" src="https://img.shields.io/badge/Forum-Ascend-F47B20">
    </a>
</p>

# 简介

提供一键式 PD 分离与 PD 混部部署，基于云原生插件化架构灵活适配多种推理引擎（[vLLM](https://github.com/vllm-project/vllm-ascend)、[SGLang](https://github.com/sgl-project/sglang)），结合高性能调度与负载均衡能力，构建高可用、可扩展的大规模推理服务。

# 快速开始

**以下是代码仓库智能体，点击 "Ask AI" ，即可开启智能代码学习与问答体验！它们将帮助您更深入地理解 MindIE Motor 的运行原理，并协助解决使用过程中遇到的问题与错误！**

<p align="center">
    <a href="https://zread.ai/verylucky01/MindIE-PyMotor">
        <img alt="Zread Ask AI" src="https://img.shields.io/badge/Zread-Ask%20AI-2F66F6">
    </a>
    <a href="https://deepwiki.com/verylucky01/MindIE-PyMotor">
        <img alt="DeepWiki Ask AI" src="https://img.shields.io/badge/DeepWiki-Ask%20AI-2F66F6">
    </a>
</p>

**环境准备**：安装前的相关软硬件环境准备，以及安装步骤，请参见[环境准备](./docs/zh/user_guide/environment_preparation.md)。

**快速部署**：快速体验启动服务、接口调用、精度&性能测试和停止服务全流程，请参见[快速部署](./docs/zh/user_guide/quick_start.md)。

**最佳实践**：PD 分离部署请参见[PD 分离服务部署详细指导](./docs/zh/user_guide/deployment/k8s/pd_disaggregation_deployment.md)，PD 混部部署请参见[PD 混部服务部署详细指导](./docs/zh/user_guide/deployment/k8s/pd_aggregation_deployment.md)。

# 最新消息

[2026/03] 🚀 MindIE Motor正式开源，新增代码仓智能体。

# 社区活动

MindIE 系列 TC/SIG 会议安排，请查看 [Ascend会议中心](https://meeting.ascend.osinfra.cn/)。

开源社区论坛与技术交流、问题讨论及经验分享，请访问 [昇腾论坛](https://www.hiascend.com/forum/)。

# 问题反馈

如果您在使用过程中发现异常，建议先查看仓库的 [Issues 列表](https://gitcode.com/wumingjing/MindIE-PyMotor-README/issues)，确认是否已有相同或相近的问题。

如果现有问题列表中没有对应项，可以直接 [创建新的 Issue](https://gitcode.com/wumingjing/MindIE-PyMotor-README/issues/create/choose)，并尽量补充完整的问题现象、复现步骤、日志片段和环境信息，便于快速定位。

如果问题涉及安全风险，请不要通过公开 Issue 直接披露，建议按照 [security.md](./security.md) 中的方式联系项目维护者。

# 贡献指南

如果您计划提交代码修改，建议按下面的流程进行：

- Fork 本项目的仓库，并 Clone 到本地。
- 提交前请通过所有单元测试，完整测试入口见 [tests/run_tests.sh](./tests/run_tests.sh)。
- 提交代码，并新建 Pull Request，需在 Pull Request 回复 `compile` 触发门禁流水线（CI）。
- 代码检视：您需要根据评审意见修改代码，并重新提交更新。此流程可能涉及多轮迭代。
- 审核和测试通过后，会将您的 Pull Request 合并到项目的 master 分支。

# 许可证

本项目使用 [Mulan PSL v2](./LICENSE.md) 开源许可证。
