# MindIE Motor

<p align="center">
    <img alt="MindIE Motor" src="./docs/en/imgs/MindIE-Motor.png">
</p>

<p align="center">
    <a href="./LICENSE.md">
        <img alt="License" src="https://img.shields.io/badge/License-Mulan-blue">
    </a>
    <a href="https://mindie-motor.readthedocs.io/">
        <img alt="Documentation" src="https://img.shields.io/badge/Docs-Read%20the%20Docs-8CA1AF">
    </a>
    <a href="https://meeting.ascend.osinfra.cn/">
        <img alt="TC and SIG Meetings" src="https://img.shields.io/badge/Meetings-TC%2FSIG-0A7B83">
    </a>
    <a href="https://www.hiascend.com/forum/">
        <img alt="Ascend Forum" src="https://img.shields.io/badge/Forum-Ascend-F47B20">
    </a>
</p>

# Introduction

Provides one-click PD disaggregated and PD aggregated deployment, flexibly adapting to multiple inference engines ([vLLM](https://github.com/vllm-project/vllm-ascend), [SGLang](https://github.com/sgl-project/sglang)) based on a cloud-native plug-in architecture, combined with high-performance scheduling and load balancing capabilities to build highly available and scalable large-scale inference services.

# Quick Start

**Online documentation**: [MindIE Motor Documentation](https://mindie-motor.readthedocs.io/)

**The following are code repository AI agents. Click "Ask AI" to start an intelligent code learning and Q&A experience! They will help you gain a deeper understanding of how MindIE Motor works and assist in resolving issues and errors encountered during use!**

<p align="center">
    <a href="https://zread.ai/Ascend/MindIE-Motor">
        <img alt="Zread Ask AI" src="https://img.shields.io/badge/Zread-Ask%20AI-2F66F6">
    </a>
    <a href="https://deepwiki.com/Ascend/MindIE-Motor">
        <img alt="DeepWiki Ask AI" src="https://img.shields.io/badge/DeepWiki-Ask%20AI-2F66F6">
    </a>
</p>

**Environment preparation**: For the software and hardware environment preparation and installation steps before installation, see [Environment Preparation](./docs/en/user_guide/environment_preparation.md).

**Quick deployment**: To quickly experience the full process of starting the service, calling APIs, accuracy and performance testing, and stopping the service, see [Quick Deployment](./docs/en/user_guide/quick_start.md).

**Best practices**: For PD disaggregation deployment, see [Detailed Guide for PD Disaggregation Service Deployment](./docs/en/user_guide/deployment/k8s/pd_disaggregation_deployment.md). For PD aggregation deployment, see [Detailed Guide for PD Aggregation Service Deployment](./docs/en/user_guide/deployment/k8s/pd_aggregation_deployment.md).

# Release Notes

For component version matching and compatibility information, see [Version Matching Notes](./docs/en/release_note_motor.md).

# Latest News

[2026/03] 🚀 MindIE Motor is officially open-sourced, with a new code repository agent added.

# Community Activities

For the MindIE series TC/SIG meeting schedule, see the [Ascend Meeting Center](https://meeting.ascend.osinfra.cn/).

For open source community forums, technical exchanges, issue discussions, and experience sharing, visit the [Ascend Forum](https://www.hiascend.com/forum/).

# Issue Feedback

If you encounter any exceptions during use, it is recommended that you first check the repository's [Issues list](https://gitcode.com/Ascend/MindIE-Motor/issues) to confirm whether the same or a similar issue already exists.

If no corresponding item exists in the current issue list, you can directly [create a new Issue](https://gitcode.com/Ascend/MindIE-Motor/issues/create/choose), and provide as complete information as possible, including the problem symptoms, reproduction steps, log snippets, and environment details, to facilitate quick localization.

If the issue involves security risks, do not disclose it directly through a public Issue. It is recommended that you contact the project maintainers in the manner described in [security.md](./security_en.md).

# Contribution Guide

If you plan to submit code changes, it is recommended that you follow the process below:

- Fork the repository of this project and clone it to your local machine.
- Pass all unit tests before submission. For the complete test entry, see [tests/run_tests.sh](./tests/run_tests.sh).
- Submit the code and create a new Pull Request. You need to reply `compile` in the Pull Request to trigger the gated pipeline (CI).
- Code review: You need to modify the code according to the review comments and resubmit the update. This process may involve multiple rounds of iteration.
- After the review and tests pass, your Pull Request will be merged into the master branch of the project.

# License

This project is released under the [Mulan PSL v2](./LICENSE.md) open source license.
