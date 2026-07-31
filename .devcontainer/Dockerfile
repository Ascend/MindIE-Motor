# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm

USER root

# Install the native toolchain needed by the optional Rust component and the
# utilities commonly used while developing and diagnosing MindIE-Motor.
# The base image includes Yarn already, so its extra APT source is unnecessary
# here and can block apt updates when the external repository rotates its key.
RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y --no-install-recommends \
        build-essential \
        iproute2 \
        iputils-ping \
        jq \
        libssl-dev \
        libzmq3-dev \
        pkg-config \
        shellcheck \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

USER vscode
