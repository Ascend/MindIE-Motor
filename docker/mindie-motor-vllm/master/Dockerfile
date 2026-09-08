# syntax=docker/dockerfile:1.4
#
# Development image for the current MindIE-Motor master working tree.
# Build from the repository root so local changes are included.

ARG BASE_IMAGE=quay.nju.edu.cn/ascend/vllm-ascend:v0.18.0
FROM ${BASE_IMAGE}

ARG IMAGE_VERSION=master
ARG PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
ARG PIP_TRUSTED_HOST=repo.huaweicloud.com
# Default: pack both Rust crates (workload-shm required; kv-conductor when cargo
# + libzmq are present). Force cargo in-image even if the build context already
# has a host-built lib/*.so or bin/kv-conductor (glibc/ABI mismatch).
# Override with --build-arg SKIP_KV_CONDUCTOR_BUILD=1 only when the base image
# cannot install libzmq headers.
ARG SKIP_KV_CONDUCTOR_BUILD=0
ARG SKIP_WORKLOAD_SHM_BUILD=0

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

USER root

# pciutils: NPU discovery. gcc/curl: rustup + cargo-build workload-shm.
# libzmq + pkg-config: cargo-build kv-conductor. Rust itself is installed by
# build.sh and removed after pip install.
RUN set -eux; \
    if command -v apt-get >/dev/null 2>&1; then \
        apt-get update; \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            pciutils build-essential g++ ca-certificates curl libzmq3-dev pkg-config; \
        rm -rf /var/lib/apt/lists/*; \
    elif command -v dnf >/dev/null 2>&1; then \
        dnf install -y pciutils gcc gcc-c++ make ca-certificates curl zeromq-devel pkgconf; \
        dnf clean all; \
    elif command -v yum >/dev/null 2>&1; then \
        yum install -y pciutils gcc gcc-c++ make ca-certificates curl zeromq-devel pkgconfig; \
        yum clean all; \
    else \
        echo "no supported package manager was found" >&2; \
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
    SKIP_WORKLOAD_SHM_BUILD="${SKIP_WORKLOAD_SHM_BUILD}" \
    SKIP_KV_CONDUCTOR_BUILD="${SKIP_KV_CONDUCTOR_BUILD}" bash build.sh; \
    pip install --no-cache-dir --force-reinstall dist/motor*.whl; \
    # -P: do not prepend WORKDIR (the copied source tree) to sys.path, so this
    # asserts the site-packages install rather than motor/ from the build context.
    python -P -c "from pathlib import Path; import motor.coordinator.workload_shm_rs as p; lib=Path(p.__file__).resolve().parent/'lib'/'libmindie_workload_shm.so'; assert lib.is_file() and lib.stat().st_size>0, lib"; \
    if [ "${SKIP_KV_CONDUCTOR_BUILD}" != "1" ]; then \
        python -P -c "from pathlib import Path; import motor; bin=Path(motor.__file__).resolve().parent/'kv_conductor'/'bin'/'kv-conductor'; assert bin.is_file() and bin.stat().st_size>0, bin"; \
    fi; \
    rm -rf build/ motor.egg-info/ dist/ \
        /root/.cargo /root/.rustup "${HOME}/.cargo" "${HOME}/.rustup" \
        motor/coordinator/workload_shm_rs/target motor/kv_conductor/target

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
