# MindIE PyMotor

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

English | [简体中文](./README.md)

# What's New

[2026/03] 🚀 MindIE-PyMotor officially open sourced, with the addition of the code repository agents.

# Introduction

MindIE-PyMotor provides one-click PD-disaggregated deployment with a cloud-native plugin-based architecture that flexibly adapts to multiple inference engines ([vLLM](https://github.com/vllm-project/vllm-ascend) and [SGLang](https://github.com/sgl-project/sglang)). Combined with high-performance scheduling and load balancing, it delivers highly available, scalable large-scale inference services.

# Community Events

See the [Ascend Meeting Center](https://meeting.ascend.osinfra.cn/) for the schedule of MindIE TC and SIG meetings.

For open-source community forums, technical exchanges, issue discussions, and experience sharing, visit the [Ascend Forum](https://www.hiascend.com/forum/).

# Quick Start

The following are code repository agents. Click **Ask AI** to start an intelligent code learning and Q&A experience. They will help you gain a deeper understanding of MindIE-PyMotor's operational principles and assist in resolving issues and errors encountered during usage.

<p align="center">
    <a href="https://zread.ai/verylucky01/MindIE-PyMotor">
        <img alt="Zread Ask AI" src="https://img.shields.io/badge/Zread-Ask%20AI-2F66F6">
    </a>
    <a href="https://deepwiki.com/verylucky01/MindIE-PyMotor">
        <img alt="DeepWiki Ask AI" src="https://img.shields.io/badge/DeepWiki-Ask%20AI-2F66F6">
    </a>
</p>

**Environment Setup**: For details about how to prepare the software and hardware environment before installation and the installation procedure, see [Environment Setup](./docs/en/user_guide/environment_preparation.md).

**Quick Start**: For details about how to quickly experience the entire process of starting a service, calling APIs, testing precision and performance, and stopping a service, see [Quick Start](./docs/en/user_guide/quick_start.md).

# Issue Reporting

If you encounter any issues, check the [Issues list](https://gitcode.com/Ascend/MindIE-Motor/issues) of the repository to see if the same or similar issues have been reported.

If the existing issue list does not contain the corresponding item, you can directly [create an issue](https://gitcode.com/Ascend/MindIE-Motor/issues/create/choose) and provide as much information as possible, including the symptom, reproduction steps, log snippets, and environment information, to facilitate quick locating.

If the issue involves security risks, do not disclose it directly through a public issue. Instead, follow the process described in [security.md](./security.md) to contact the project maintainers.

# Contribution Guide

If you plan to submit code changes, you are advised to follow the process below:

- Fork the repository of the project and clone it to your local host.
- Before submitting the code, ensure that all unit tests are passed. For details about the complete test entry, see [tests/run_tests.sh](./tests/run_tests.sh).
- Submit the code and create a pull request (PR). Reply to the PR with `compile` to trigger the CI pipeline.
- Code review: Modify the code according to review comments and resubmit your changes. This process may involve multiple rounds of iterations.
- After your PR is approved and all tests pass, the system will merge it into the master branch of the project.

# License

This project is licensed under the [Mulan PSL v2](./LICENSE.md).
