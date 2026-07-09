#!/usr/bin/env bash
set -euo pipefail

NAME="${1:-}"
if [[ -z "${NAME}" ]]; then
    echo "Usage: sudo bash $0 <container_name>"
    exit 1
fi

CERT_URL="https://download.huawei.com/dl/download.do?actionFlag=download&nid=PKI1000000002&partNo=3001&mid=SUP_PKI"
ROOT_CERT_PATH="/var/ascend/npu-root-certs"
CERT_FILE="${ROOT_CERT_PATH}/Huawei Equipment Root CA.pem"
KMS_USER="HwHiAiUser"
HOST_DOWNLOAD_DIR="${HOST_DOWNLOAD_DIR:-/tmp/secure_h2d_deploy}"
OP_PLUGIN_REPO="${OP_PLUGIN_REPO:-https://gitcode.com/Ascend/op-plugin.git}"
MOTOR_REPO="${MOTOR_REPO:-https://gitcode.com/Ascend/MindIE-PyMotor.git}"

log() {
    echo "[secure_h2d_deploy] $*"
}

run() {
    log "RUN: $*"
    "$@"
}

install_haveged_if_needed() {
    log "1. check haveged"

    if systemctl status -l haveged >/dev/null 2>&1; then
        log "haveged already exists and status is available"
        return
    fi

    log "haveged not found or inactive, try to install"

    if command -v apt >/dev/null 2>&1; then
        run apt update
        run apt install -y haveged
    elif command -v yum >/dev/null 2>&1; then
        run yum install -y haveged
    elif command -v dnf >/dev/null 2>&1; then
        run dnf install -y haveged
    else
        echo "No supported package manager found: apt/yum/dnf"
        exit 1
    fi

    run systemctl enable haveged
    run systemctl restart haveged
    run systemctl status -l haveged
}

download_and_install_cert() {
    log "2. prepare and install Huawei Equipment Root CA certificate"

    local local_cert="${CERT_SRC:-./Huawei Equipment Root CA.pem}"
    local tmp_cert="${CERT_FILE}.tmp"

    run mkdir -p /var/ascend
    run mkdir -p "${ROOT_CERT_PATH}"

    # 优先使用用户提前准备好的本地证书
    if [[ -s "${local_cert}" ]]; then
        log "use local certificate: ${local_cert}"
        run cp -f "${local_cert}" "${CERT_FILE}"

    # 目标目录中已经存在证书时直接复用
    elif [[ -s "${CERT_FILE}" ]]; then
        log "certificate already exists: ${CERT_FILE}"

    # 本地没有证书时才尝试在线下载
    else
        log "local certificate not found, try downloading from Huawei"

        rm -f "${tmp_cert}"

        if command -v curl >/dev/null 2>&1; then
            if ! curl \
                -L \
                --fail \
                --connect-timeout 15 \
                --max-time 120 \
                --retry 5 \
                --retry-delay 5 \
                --retry-all-errors \
                -o "${tmp_cert}" \
                "${CERT_URL}"; then
                rm -f "${tmp_cert}"
                echo
                echo "ERROR: certificate download failed."
                echo "Please manually download:"
                echo "  Huawei Equipment Root CA.pem"
                echo "from:"
                echo "  ${CERT_URL}"
                echo "and place it at:"
                echo "  ${local_cert}"
                exit 1
            fi
        elif command -v wget >/dev/null 2>&1; then
            if ! wget \
                --timeout=30 \
                --tries=5 \
                -O "${tmp_cert}" \
                "${CERT_URL}"; then
                rm -f "${tmp_cert}"
                echo
                echo "ERROR: certificate download failed."
                echo "Please manually place the certificate at:"
                echo "  ${local_cert}"
                exit 1
            fi
        else
            echo "ERROR: curl and wget are not installed"
            exit 1
        fi

        [[ -s "${tmp_cert}" ]] || {
            rm -f "${tmp_cert}"
            echo "ERROR: downloaded certificate is empty"
            exit 1
        }

        run mv -f "${tmp_cert}" "${CERT_FILE}"
    fi

    [[ -s "${CERT_FILE}" ]] || {
        echo "ERROR: certificate file is missing or empty: ${CERT_FILE}"
        exit 1
    }

    run chown -R "${KMS_USER}:${KMS_USER}" /var/ascend
    run chown -R "${KMS_USER}:${KMS_USER}" "/home/${KMS_USER}"
    run chmod 750 /var/ascend
    run chmod 750 "${ROOT_CERT_PATH}"
    run chmod 640 "${CERT_FILE}"

    log "certificate installed: ${CERT_FILE}"
}

configure_kmsagent_cert() {
    log "3. configure KMSAgent DevRootCert"

    export ROOT_CERT_PATH

    su - "${KMS_USER}" -w ROOT_CERT_PATH -s /bin/bash <<'EOF'
set -euo pipefail

INSTALL_DIR=/usr/local/Ascend
lib_dirs=(
   ${INSTALL_DIR}/driver/lib64/driver
   ${INSTALL_DIR}/driver/lib64/common
   ${INSTALL_DIR}/driver/lib64/inner
)

asdrv_library_path=$(IFS=:; echo "${lib_dirs[*]}")
export LD_LIBRARY_PATH=${asdrv_library_path}

set_kmsagent_cfg() {
   local cmd=${INSTALL_DIR}/driver/tools/kmsagent
   local cfg=/var/kmsagentd/kmsagent.conf
   local ksf=/var/kmsagentd/kmsconf.ksf
   echo "[secure_h2d_deploy] RUN: ${cmd} -c ${cfg} -k ${ksf} -s $1 -n $2 -v $3"
   ${cmd} -c ${cfg} -k ${ksf} -s "$1" -n "$2" -v "$3"
}

set_kmsagent_cfg SERVER DevRootCert "${ROOT_CERT_PATH}/Huawei Equipment Root CA.pem"
EOF
}

start_h2d_key_manage() {
    log "4. start KMSAgent h2d key-manage"
    run npu-smi set -t key-manage -s start=h2d
}

container_exec() {
    docker exec "${NAME}" bash -lc "$*"
}

container_deploy() {
    log "5. enter container: ${NAME}"

log "6. download repositories on host and copy them into container"

run mkdir -p "${HOST_DOWNLOAD_DIR}"

# 在宿主机下载 op-plugin
if [[ ! -d "${HOST_DOWNLOAD_DIR}/op-plugin/.git" ]]; then
    run rm -rf "${HOST_DOWNLOAD_DIR}/op-plugin"
    run git clone -b 26.0.0 \
        "${OP_PLUGIN_REPO}" \
        "${HOST_DOWNLOAD_DIR}/op-plugin"
else
    log "host op-plugin already exists, skip clone"
fi

# 在宿主机下载 MindIE-PyMotor
if [[ ! -d "${HOST_DOWNLOAD_DIR}/MindIE-PyMotor/.git" ]]; then
    run rm -rf "${HOST_DOWNLOAD_DIR}/MindIE-PyMotor"
    run git clone \
        "${MOTOR_REPO}" \
        "${HOST_DOWNLOAD_DIR}/MindIE-PyMotor"
else
    log "host MindIE-PyMotor already exists, skip clone"
fi

# 在容器中创建工作目录
run docker exec "${NAME}" mkdir -p /workspace

# 删除容器中的旧目录，避免 docker cp 形成嵌套目录
run docker exec "${NAME}" rm -rf \
    /workspace/op-plugin \
    /workspace/MindIE-PyMotor

# 从宿主机复制到容器
run docker cp \
    "${HOST_DOWNLOAD_DIR}/op-plugin" \
    "${NAME}:/workspace/op-plugin"

run docker cp \
    "${HOST_DOWNLOAD_DIR}/MindIE-PyMotor" \
    "${NAME}:/workspace/MindIE-PyMotor"

# 检查复制结果
container_exec '
set -euo pipefail

test -d /workspace/op-plugin
test -d /workspace/MindIE-PyMotor

echo "[secure_h2d_deploy] op-plugin copied to /workspace/op-plugin"
echo "[secure_h2d_deploy] MindIE-PyMotor copied to /workspace/MindIE-PyMotor"
'

    log "7~8. check python version and backup torch_npu/dynamo"
    container_exec '
set -euo pipefail

PY_SITE=$(python - <<'"'"'PY'"'"'
import site
print(site.getsitepackages()[0])
PY
)

echo "[secure_h2d_deploy] python site-packages=${PY_SITE}"

rm -rf /workspace/dynamo_backup
mkdir -p /workspace/dynamo_backup

if [[ -d "${PY_SITE}/torch_npu/dynamo/torchair" ]]; then
    cp -rf "${PY_SITE}/torch_npu/dynamo/torchair" /workspace/dynamo_backup/
    echo "[secure_h2d_deploy] backed up torchair"
else
    echo "[secure_h2d_deploy] WARN: torchair not found"
fi

if [[ -d "${PY_SITE}/torch_npu/dynamo/npugraph_ex" ]]; then
    cp -rf "${PY_SITE}/torch_npu/dynamo/npugraph_ex" /workspace/dynamo_backup/
    echo "[secure_h2d_deploy] backed up npugraph_ex"
else
    echo "[secure_h2d_deploy] WARN: npugraph_ex not found"
fi
'

    log "9. patch op_plugin_functions.yaml"
    container_exec '
set -euo pipefail

YAML=/workspace/op-plugin/op_plugin/config/op_plugin_functions.yaml

if grep -q "func: crypto(Tensor key, Tensor input" "${YAML}"; then
    echo "[secure_h2d_deploy] crypto op declaration already exists"
else
python - "${YAML}" <<'"'"'PY'"'"'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()

insert = [
"  - func: crypto(Tensor key, Tensor input, Tensor(a!) output, Tensor iv, Tensor opConfig, Tensor(b!) tagRefOptional, Tensor(c!) aadRefOptional) -> Tensor",
"    op_api: all_version",
]

out = []
done = False
for line in lines:
    out.append(line)
    if not done and line.strip() == "custom:":
        out.extend(insert)
        done = True

if not done:
    raise SystemExit("custom: not found in yaml")

path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
fi
'

    log "10. copy CryptoKernelNpuOpApi.cpp"
    container_exec '
set -euo pipefail

SRC=/workspace/MindIE-PyMotor/examples/features/security/secure_h2d/CryptoKernelNpuOpApi.cpp
DST=/workspace/op-plugin/op_plugin/ops/opapi/CryptoKernelNpuOpApi.cpp

if [[ ! -f "${SRC}" ]]; then
    echo "[secure_h2d_deploy] ERROR: file not found: ${SRC}"
    exit 1
fi

mkdir -p "$(dirname "${DST}")"
cp -f "${SRC}" "${DST}"

echo "[secure_h2d_deploy] copied ${SRC} -> ${DST}"
'

    log "11. build and install torch_npu"
    container_exec '
set -euo pipefail

if [[ -f /workspace/cann-env-9.1.0/cann/set_env.sh ]]; then
    source /workspace/cann-env-9.1.0/cann/set_env.sh
fi

cd /workspace/op-plugin

bash ci/build.sh --python=3.11 --pytorch=v2.10.0-26.0.0
pip install --force-reinstall ./dist/torch_npu-*.whl

PY_SITE=$(python - <<'"'"'PY'"'"'
import site
print(site.getsitepackages()[0])
PY
)

if [[ -d /workspace/dynamo_backup/torchair ]]; then
    rm -rf "${PY_SITE}/torch_npu/dynamo/torchair"
    cp -rf /workspace/dynamo_backup/torchair "${PY_SITE}/torch_npu/dynamo/"
    echo "[secure_h2d_deploy] restored torchair"
fi

if [[ -d /workspace/dynamo_backup/npugraph_ex ]]; then
    rm -rf "${PY_SITE}/torch_npu/dynamo/npugraph_ex"
    cp -rf /workspace/dynamo_backup/npugraph_ex "${PY_SITE}/torch_npu/dynamo/"
    echo "[secure_h2d_deploy] restored npugraph_ex"
fi
'

    log "12. build pybind wrapper"
    container_exec '
set -euo pipefail

SECURE_DIR=/workspace/MindIE-PyMotor/examples/features/security/secure_h2d

if [[ -f /workspace/cann-env-9.1.0/cann/set_env.sh ]]; then
    source /workspace/cann-env-9.1.0/cann/set_env.sh
fi

cd "${SECURE_DIR}/pybind_wrapper"
bash ./build.sh
'

    log "13. install secure_patch, sitecustomize.py and pybind so"
    container_exec '
set -euo pipefail

SECURE_DIR=/workspace/MindIE-PyMotor/examples/features/security/secure_h2d

PY_SITE=$(python - <<'"'"'PY'"'"'
import site
print(site.getsitepackages()[0])
PY
)

rm -rf "${PY_SITE}/secure_patch"
rm -f "${PY_SITE}/sitecustomize.py"
rm -f "${PY_SITE}"/aes_ctr_crypt*.so
rm -f "${PY_SITE}"/aes_gcm_crypt*.so

cp -rf "${SECURE_DIR}/secure_patch" "${PY_SITE}/"
cp -f "${SECURE_DIR}/sitecustomize.py" "${PY_SITE}/"

cp -f "${SECURE_DIR}"/pybind_wrapper/lib_output/aes_ctr_crypt*.so "${PY_SITE}/"
cp -f "${SECURE_DIR}"/pybind_wrapper/lib_output/aes_gcm_crypt*.so "${PY_SITE}/"

echo "[secure_h2d_deploy] installed to ${PY_SITE}"
'

    log "verify container installation"
    container_exec '
set -euo pipefail

python - <<'"'"'PY'"'"'
import sys
import torch
import torch_npu
import aes_ctr_crypt
import aes_gcm_crypt

print("[secure_h2d_deploy] python =", sys.version)
print("[secure_h2d_deploy] torch_npu =", torch_npu.__file__)
print("[secure_h2d_deploy] has torch.ops.npu.crypto =", hasattr(torch.ops.npu, "crypto"))
print("[secure_h2d_deploy] aes_ctr_crypt ok =", aes_ctr_crypt)
print("[secure_h2d_deploy] aes_gcm_crypt ok =", aes_gcm_crypt)

if not hasattr(torch.ops.npu, "crypto"):
    raise SystemExit("torch.ops.npu.crypto not found")
PY
'
}

main() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "Please run as root: sudo bash $0 <container_name>"
        exit 1
    fi

    install_haveged_if_needed
    download_and_install_cert
    configure_kmsagent_cert
    start_h2d_key_manage
    container_deploy

    log "all secure_h2d deployment steps finished"
    log "next: enter container and set SECURE_PATCH_* env before starting vLLM"
}

main "$@"
