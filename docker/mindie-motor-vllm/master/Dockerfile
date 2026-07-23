# syntax=docker/dockerfile:1.4
#
# Development image for the current MindIE-Motor master working tree.
# Build from the repository root so local changes are included.

ARG BASE_IMAGE=quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0
FROM ${BASE_IMAGE}

ARG IMAGE_VERSION=master
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
ARG PIP_TRUSTED_HOST=repo.huaweicloud.com

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

USER root

RUN set -eux; \
    if command -v lspci >/dev/null 2>&1; then \
        exit 0; \
    elif command -v apt-get >/dev/null 2>&1; then \
        apt-get update; \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pciutils; \
        rm -rf /var/lib/apt/lists/*; \
    elif command -v dnf >/dev/null 2>&1; then \
        dnf install -y pciutils; \
        dnf clean all; \
    elif command -v yum >/dev/null 2>&1; then \
        yum install -y pciutils; \
        yum clean all; \
    else \
        echo "pciutils is missing and no supported package manager was found" >&2; \
        exit 1; \
    fi

WORKDIR /opt/MindIE-PyMotor

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY setup.py build.sh ./
COPY scripts ./scripts
COPY motor ./motor
COPY examples ./examples

RUN set -eux; \
    bash build.sh; \
    pip install --no-cache-dir --force-reinstall dist/motor*.whl; \
    rm -rf build/ motor.egg-info/ dist/

RUN set -eux; \
    cd examples/features/observability; \
    pip install --no-cache-dir -r requirements.txt; \
    bash build.sh; \
    pip install --no-cache-dir --force-reinstall dist/ccae_reporter-*.whl; \
    rm -rf build/ dist/ ccae_reporter.egg-info/

RUN set -eux; \
    mkdir -p /tmp/motor; \
    cp -a examples /tmp/motor/; \
    rm -rf /tmp/motor/examples/features/fault_tolerance/ras_monitor

WORKDIR /workspace

RUN printf '%s\n' \
    "Version: ${IMAGE_VERSION}" \
    "Container image Copyright (c) 2026, Huawei Technologies Co., Ltd. All rights reserved." \
    "This container image and its contents are governed by the Huawei Container License Agreement (\"License\"). By pulling and using the container, you accept the terms and conditions of this License." \
    "A copy of this License is made available in this container at: https://www.hiascend.com/en/legal/ascendhub-download" \
    "Note: You agree and undertake that when using Huawei or third-party software in this image, you will comply with the license agreement of the corresponding Huawei or third-party software." \
    > /workspace/agreement.txt

RUN <<'EOF' install -m 755 /dev/stdin /usr/local/bin/docker-entrypoint.sh
#!/bin/bash
set -e

load_env_script() {
    local script="$1"
    if [ -f "$script" ]; then
        . "$script"
    fi
}

cat /workspace/agreement.txt

load_env_script /usr/local/Ascend/ascend-toolkit/set_env.sh

shopt -s nullglob
for set_env in /usr/local/Ascend/cann-*/share/info/ascendnpu-ir/bin/set_env.sh; do
    load_env_script "$set_env"
    break
done

load_env_script /usr/local/Ascend/nnal/atb/set_env.sh

exec "$@"
EOF

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["/bin/bash"]
