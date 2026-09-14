# syntax=docker/dockerfile:1.4
#
# Self-contained Dockerfile for mindie-motor:3.2.0b1-vllm_ascend0.26.0rc1-a2-openeuler24.03-py3.12
#
# Base image: quay.nju.edu.cn/ascend/vllm-ascend:v0.26.0rc1-openeuler
# Target platform: linux/arm64, linux/amd64
# Source repo: https://gitcode.com/Ascend/MindIE-Motor.git
# Source branch: v3.2.0.beta.1
# Source commit: d6554728c45f06208b1992ba1f1411e1371c3f82
#
# Build (no local source required):
#   docker build --network=host \
#       --platform=linux/arm64 \
#       -t mindie-motor:3.2.0b1-vllm_ascend0.26.0rc1-a2-openeuler24.03-py3.12 \
#       -f Dockerfile \
#       .

FROM quay.nju.edu.cn/ascend/vllm-ascend:v0.26.0rc1-openeuler

ARG IMAGE_VERSION=3.2.0b1
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
ARG PIP_TRUSTED_HOST=repo.huaweicloud.com

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# pciutils: NPU discovery. git: fetch the pinned source. gcc/curl: build
# workload-shm. zeromq-devel + pkgconfig: build kv-conductor.
RUN set -eux; \
    yum install -y \
        pciutils git gcc gcc-c++ make ca-certificates curl zeromq-devel pkgconfig; \
    yum clean all

# Pre-install the Rust toolchain; build.sh detects an available cargo and skips
# its own rustup step.
ENV RUSTUP_DIST_SERVER=https://rsproxy.cn \
    RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup \
    PATH=/root/.cargo/bin:${PATH}

RUN set -eux; \
    curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh | sh -s -- -y; \
    rustup --version; \
    rustc --version; \
    cargo --version

RUN set -eux; \
    git clone --branch v3.2.0.beta.1 https://gitcode.com/Ascend/MindIE-Motor.git /opt/MindIE-Motor; \
    cd /opt/MindIE-Motor; \
    git checkout d6554728c45f06208b1992ba1f1411e1371c3f82; \
    rm -rf .git

WORKDIR /opt/MindIE-Motor

RUN set -eux; \
    pip install --no-cache-dir -r requirements.txt; \
    bash build.sh; \
    pip install --no-cache-dir --force-reinstall dist/motor*.whl; \
    # -P: do not prepend WORKDIR to sys.path, so this asserts the site-packages
    # install rather than motor/ from the cloned source tree.
    python -P -c "from pathlib import Path; import motor.coordinator.workload_shm_rs as p; lib=Path(p.__file__).resolve().parent/'lib'/'libmindie_workload_shm.so'; assert lib.is_file() and lib.stat().st_size>0, lib"; \
    python -P -c "from pathlib import Path; import motor; bin=Path(motor.__file__).resolve().parent/'kv_conductor'/'bin'/'kv-conductor'; assert bin.is_file() and bin.stat().st_size>0, bin"; \
    rm -rf build/ motor.egg-info/ dist/ \
        motor/coordinator/workload_shm_rs/target motor/kv_conductor/target

RUN set -eux; \
    cd examples/features/observability; \
    pip install --no-cache-dir -r requirements.txt; \
    bash build.sh; \
    pip install --no-cache-dir --force-reinstall dist/ccae_reporter-*.whl; \
    rm -rf build/ dist/ ccae_reporter.egg-info/

# msprobe (MindStudio precision debugging toolkit) from PyPI. Pinned so rebuilds
# stay reproducible; bump deliberately when a newer release is needed.
RUN set -eux; \
    pip install --no-cache-dir mindstudio-probe==26.1.0.post1; \
    python -P -c "from importlib.metadata import version; print(version('mindstudio-probe'))"; \
    command -v msprobe

RUN set -eux; \
    mkdir -p /tmp/motor; \
    mv /opt/MindIE-Motor/examples /tmp/motor/; \
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
