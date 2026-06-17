# syntax=docker/dockerfile:1.4
#
# Self-contained Dockerfile for mindie-motor-vllm:3.0.0-800I-A2-py3.11-openEuler24.03-lts-aarch64
#
# Base image: quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-openeuler
# Target platform: linux/arm64
# Source repo: https://gitcode.com/Ascend/MindIE-PyMotor.git
# Source branch: v3.0.0
# Source commit: 383d1787ed3fc27aaad2db9cc5506d40c258c279
#
# Build (no local source required):
#   docker build --network=host \
#       --platform=linux/arm64 \
#       -t mindie-motor-vllm:3.0.0-800I-A2-py3.11-openEuler24.03-lts-aarch64 \
#       -f Dockerfile \
#       .

FROM quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0-openeuler

ARG IMAGE_VERSION=3.0.0
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
ARG PIP_TRUSTED_HOST=repo.huaweicloud.com

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN set -eux; \
    if ! command -v git >/dev/null 2>&1; then \
        if command -v apt-get >/dev/null 2>&1; then \
            apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git; \
        elif command -v yum >/dev/null 2>&1; then \
            yum install -y git; \
        elif command -v dnf >/dev/null 2>&1; then \
            dnf install -y git; \
        else \
            echo "git not found and no supported package manager" >&2; exit 1; \
        fi; \
    fi; \
    git clone --branch v3.0.0 https://gitcode.com/Ascend/MindIE-PyMotor.git /opt/MindIE-PyMotor; \
    cd /opt/MindIE-PyMotor; \
    git checkout 383d1787ed3fc27aaad2db9cc5506d40c258c279; \
    rm -rf .git

WORKDIR /opt/MindIE-PyMotor

RUN set -eux; \
    pip install --no-cache-dir -r requirements.txt; \
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
    mv /opt/MindIE-PyMotor/examples /tmp/motor/; \
    ls -l /tmp/motor/examples; \
    rm -rf /tmp/motor/examples/features/fault_tolerance/ras_monitor; \
    ls -l /tmp/motor/examples/features/fault_tolerance; \
    find /tmp -mindepth 1 -maxdepth 1 ! -name "motor" -exec rm -rf {} +

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

# Ascend env scripts exist only inside the runtime image; use source=/dev/null
# so ShellCheck does not require those paths at lint time.
load_env_script() {
    local script="$1"
    if [ -f "$script" ]; then
        # shellcheck source=/dev/null
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
