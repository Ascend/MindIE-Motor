# MindIE Motor

<p align="center">
    <img alt="MindIE Motor" src="./docs/en/imgs/mindie_motor_title.png">
</p>

<p align="center">
    <a href="./LICENSE_en.md">
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

English | [简体中文](README.md)

# Introduction

MindIE Motor provides one-click PD disaggregation and PD co-location deployment. Based on a cloud-native pluggable architecture, it flexibly adapts to multiple inference engines ([vLLM](https://github.com/vllm-project/vllm-ascend) and [SGLang](https://github.com/sgl-project/sglang)), and combines high-performance scheduling and load balancing capabilities to build highly available, scalable large-scale inference services.

# Quick Start

**Online documentation**: [MindIE Motor Documentation](https://mindie-motor.readthedocs.io/)

**The following are code repository agents. Click "Ask AI" to start an intelligent code learning and Q&A experience! They will help you gain a deeper understanding of how MindIE Motor works and assist you in resolving issues and errors encountered during use!**

<p align="center">
    <a href="https://zread.ai/Ascend/MindIE-Motor">
        <img alt="Zread Ask AI" src="https://img.shields.io/badge/Zread-Ask%20AI-2F66F6">
    </a>
    <a href="https://deepwiki.com/Ascend/MindIE-Motor">
        <img alt="DeepWiki Ask AI" src="https://img.shields.io/badge/DeepWiki-Ask%20AI-2F66F6">
    </a>
</p>

**Environment preparation**: For the software and hardware environment preparation and installation steps before installation, see [Environment Preparation](./docs/en/user_guide/environment_preparation.md).

**Quick deployment**: To quickly experience the full process of starting the service, invoking APIs, precision and performance testing, and stopping the service, see [Quick Deployment](./docs/en/user_guide/quick_start.md).

**Best practices**: For PD disaggregation deployment, see [detailed guide for PD disaggregation deployment](./docs/en/user_guide/deployment/k8s/pd_disaggregation_deployment.md). For PD aggregation deployment, see [detailed guide for PD co-location deployment](./docs/en/user_guide/deployment/k8s/pd_aggregation_deployment.md).

# Release Notes

For component version compatibility and matching information, see [Version Mapping Notes](./docs/en/release_note_motor.md).

# Latest News

[2026/03] 🚀 MindIE Motor is officially open-sourced, with a new code repository agent added.

# Community Activities

For the MindIE series TC/SIG meeting schedule, see [Ascend Meeting Center](https://meeting.ascend.osinfra.cn/).

For open-source community forums, technical exchanges, issue discussions, and experience sharing, visit the [Ascend Forum](https://www.hiascend.com/forum/).

# Issue Feedback

If you encounter any anomalies during use, check the repository's [Issues list](https://gitcode.com/Ascend/MindIE-Motor/issues) first to see whether an identical or similar issue already exists.

If no matching item is found in the existing issue list, you can directly [create a new Issue](https://gitcode.com/Ascend/MindIE-Motor/issues/create/choose) and provide as complete information as possible, including the problem symptoms, reproduction steps, log snippets, and environment details, to facilitate quick localization.

If the issue involves security risks, do not disclose it directly through a public Issue. Instead, contact the project maintainers as described in [security.md](./security_en.md).

# Contribution Guide

If you plan to submit code changes, follow the process below:

- Fork the repository of this project and clone it locally.

- Pass all unit tests before submission. For the complete test entry, see [tests/run_tests.sh](./tests/run_tests.sh).

- Commit the code and create a Pull Request. Reply `compile` in the Pull Request to trigger the gated pipeline (CI).

- Code review: You need to modify the code based on review comments and resubmit the update. This process may involve multiple iterations.

- After the review and tests pass, your Pull Request will be merged into the master branch of the project.

# License

This project is licensed under the [Mulan PSL v2](./LICENSE_en.md) open source license.
