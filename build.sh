#!/bin/bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

set -euo pipefail

# This script builds the motor wheel package.

# Allow verbosity control: set VERBOSE=1 to see full logs.
VERBOSE=${VERBOSE:-0}

# Clean up any existing build artifacts that might cause import issues.
rm -rf build/
rm -rf motor.egg-info/
rm -rf dist/

echo "Generating protobuf files..."
./scripts/generate_proto.sh

# Keep motor_version in sync with motor/__init__.py::__version__ (single source of truth).
# Support both double- and single-quoted __version__ assignments (aligned with setup.py).
MOTOR_VERSION="$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' ./motor/__init__.py | head -n1)"
if [[ -z "${MOTOR_VERSION}" ]]; then
  MOTOR_VERSION="$(sed -n "s/^__version__[[:space:]]*=[[:space:]]*'\([^']*\)'.*/\1/p" ./motor/__init__.py | head -n1)"
fi
if [[ -z "${MOTOR_VERSION}" ]]; then
  echo "Error: failed to read __version__ from ./motor/__init__.py" >&2
  exit 1
fi

touch ./motor/version.info
cat>./motor/version.info<<EOF
motor_version : ${MOTOR_VERSION}
EOF
echo "Using motor_version=${MOTOR_VERSION}"

# --- Rust toolchain (CI / Docker / host) ---
# Jenkins often has rustup under $HOME/.cargo or /root/.cargo while the job PATH
# does not. Nightly previously skipped native crates and shipped an empty wheel.
# Source cargo env first. rustup only if a crate actually needs compiling:
# missing lib/*.so or bin/kv-conductor, or an explicit SKIP_*=0 force-rebuild.
# Existing artifacts skip cargo (and rustup). Offline: SKIP_RUST_INSTALL=1 plus
# WORKLOAD_SHM_PREBUILT or an already-copied .so.
#
# Default: reuse lib/*.so and bin/kv-conductor when those files already exist.
# SKIP_WORKLOAD_SHM_BUILD=0 / SKIP_KV_CONDUCTOR_BUILD=0 force a rebuild after
# .rs changes. SKIP_RUST_BUILD=1 fills both SKIP flags if they were unset.

KV_CONDUCTOR_DIR="./motor/kv_conductor"
KV_CONDUCTOR_BIN_DIR="$KV_CONDUCTOR_DIR/bin"
KV_CONDUCTOR_BIN="$KV_CONDUCTOR_BIN_DIR/kv-conductor"
WORKLOAD_SHM_DIR="./motor/coordinator/workload_shm_rs"
WORKLOAD_SHM_LIB_DIR="$WORKLOAD_SHM_DIR/lib"
WORKLOAD_SHM_LIB="$WORKLOAD_SHM_LIB_DIR/libmindie_workload_shm.so"

# shellcheck disable=SC1091
source ./scripts/ensure_rust.sh
motor_apply_skip_rust_build_shorthand
motor_source_cargo_env || true

_shm_needs_cargo="0"
if [[ -z "${WORKLOAD_SHM_PREBUILT:-}" ]]; then
    if [[ ! -f "$WORKLOAD_SHM_LIB" ]] || motor_var_is_explicit_zero SKIP_WORKLOAD_SHM_BUILD; then
        _shm_needs_cargo="1"
    fi
fi
_kv_needs_cargo="0"
if [[ -z "${KV_CONDUCTOR_PREBUILT:-}" && "${SKIP_KV_CONDUCTOR_BUILD:-0}" != "1" ]]; then
    if [[ ! -f "$KV_CONDUCTOR_BIN" ]] || motor_var_is_explicit_zero SKIP_KV_CONDUCTOR_BUILD; then
        _kv_needs_cargo="1"
    fi
fi

if ! motor_cargo_usable; then
    if [[ "$_shm_needs_cargo" != "1" && "$_kv_needs_cargo" != "1" ]]; then
        echo "native artifacts already present; skip rustup."
    elif [[ "$_shm_needs_cargo" == "1" && ! -f "$WORKLOAD_SHM_LIB" && -z "${WORKLOAD_SHM_PREBUILT:-}" ]]; then
        echo "=== rust toolchain ==="
        if ! motor_ensure_cargo; then
            echo "[ERROR] refusing to emit dist/motor-*.whl: cargo is required to compile libmindie_workload_shm.so." >&2
            echo "  Coordinator cannot start without this library (no Python ledger fallback)." >&2
            echo "  Install Rust, or set WORKLOAD_SHM_PREBUILT, or copy the .so into $WORKLOAD_SHM_LIB_DIR/." >&2
            echo "  Offline: SKIP_RUST_INSTALL=1 plus a prebuilt library." >&2
            exit 1
        fi
    else
        echo "=== rust toolchain ==="
        if ! motor_ensure_cargo; then
            echo "[WARNING] cargo unavailable; will reuse existing workload-shm if present and skip kv-conductor compile."
        fi
    fi
fi

# No usable cargo means no Rust crate can compile. That is a hard failure for
# workload-shm (unless PREBUILT / existing lib/*.so). It is not a conductor skip.
# kv-conductor WARNING-skip is only for conductor-specific deps (libzmq / g++ for
# zmq-sys) or a failed conductor cargo build after cargo itself is usable.

# --- Required workload-shm (coordinator) build ---
# Default: reuse motor/coordinator/workload_shm_rs/lib/libmindie_workload_shm.so
# if it already exists. Compile only when the file is missing (or when
# SKIP_WORKLOAD_SHM_BUILD=0 is set explicitly to force a rebuild after .rs changes).
# PREBUILT always copies over lib/. Missing .so after this step is a hard error.

echo "=== workload-shm ==="

if [[ -n "${WORKLOAD_SHM_PREBUILT:-}" ]]; then
    if [[ ! -f "$WORKLOAD_SHM_PREBUILT" ]]; then
        echo "[ERROR] WORKLOAD_SHM_PREBUILT='$WORKLOAD_SHM_PREBUILT' does not exist."
        exit 1
    fi
    mkdir -p "$WORKLOAD_SHM_LIB_DIR"
    cp "$WORKLOAD_SHM_PREBUILT" "$WORKLOAD_SHM_LIB"
    chmod +x "$WORKLOAD_SHM_LIB"
    echo "workload-shm library ready (pre-built): $WORKLOAD_SHM_LIB"

elif [[ -f "$WORKLOAD_SHM_LIB" ]] && ! motor_var_is_explicit_zero SKIP_WORKLOAD_SHM_BUILD; then
    echo "workload-shm library ready (existing, skip cargo): $WORKLOAD_SHM_LIB"

elif motor_cargo_usable; then
    if [[ "${SKIP_WORKLOAD_SHM_BUILD:-0}" == "1" ]]; then
        echo "[WARNING] SKIP_WORKLOAD_SHM_BUILD=1 ignored because $WORKLOAD_SHM_LIB is missing."
    fi
    echo "Building workload-shm from source (cargo build --release)..."
    (
        cd "$WORKLOAD_SHM_DIR" || exit 1
        cargo build --release
    )
    _shm_built="$WORKLOAD_SHM_DIR/target/release/libmindie_workload_shm.so"
    if [[ ! -f "$_shm_built" ]]; then
        echo "[ERROR] refusing to emit dist/motor-*.whl: cargo build --release did not produce $_shm_built." >&2
        echo "  Coordinator cannot start without libmindie_workload_shm.so (no Python ledger fallback)." >&2
        exit 1
    fi
    mkdir -p "$WORKLOAD_SHM_LIB_DIR"
    cp "$_shm_built" "$WORKLOAD_SHM_LIB"
    chmod +x "$WORKLOAD_SHM_LIB"
    echo "workload-shm library ready (cargo-built): $WORKLOAD_SHM_LIB"

elif [[ -f "$WORKLOAD_SHM_LIB" ]]; then
    echo "workload-shm library ready (existing, no cargo): $WORKLOAD_SHM_LIB"

else
    echo "[ERROR] refusing to emit dist/motor-*.whl: libmindie_workload_shm.so is missing and cargo is unavailable." >&2
    echo "  Coordinator cannot start without this library (no Python ledger fallback)." >&2
    echo "  Options:" >&2
    echo "    1. WORKLOAD_SHM_PREBUILT=/path/to/libmindie_workload_shm.so bash build.sh" >&2
    echo "    2. cp /path/to/libmindie_workload_shm.so $WORKLOAD_SHM_LIB_DIR/ && bash build.sh" >&2
    echo "    3. Unset SKIP_RUST_INSTALL and retry (build.sh installs rustup), or install cargo on PATH" >&2
    echo "  SKIP_RUST_BUILD=1 / SKIP_WORKLOAD_SHM_BUILD=1 only reuse an existing .so; they do not" >&2
    echo "  authorize a first-time build without one." >&2
    exit 1
fi

if [[ ! -f "$WORKLOAD_SHM_LIB" ]]; then
    echo "[ERROR] refusing to emit dist/motor-*.whl: $WORKLOAD_SHM_LIB is missing after the workload-shm build step." >&2
    echo "  cargo/prebuilt must produce libmindie_workload_shm.so before pip wheel." >&2
    echo "  Coordinator cannot start without this library (no Python ledger fallback)." >&2
    exit 1
fi

echo ""

# --- Optional kv-conductor ---
# Default: reuse motor/kv_conductor/bin/kv-conductor if present.
# Compile only when the binary is missing (conductor-specific skip if no zmq/g++).
# SKIP_KV_CONDUCTOR_BUILD=0 forces cargo even when bin/ exists.
# SKIP_KV_CONDUCTOR_BUILD=1 never compiles; omits the crate if bin/ is also missing.
# This script never apt-installs libzmq.

echo "=== kv-conductor ==="

_kv_ready="0"

if [[ -n "${KV_CONDUCTOR_PREBUILT:-}" ]]; then
    if [[ ! -f "$KV_CONDUCTOR_PREBUILT" ]]; then
        echo "[ERROR] KV_CONDUCTOR_PREBUILT='$KV_CONDUCTOR_PREBUILT' does not exist."
        exit 1
    fi
    mkdir -p "$KV_CONDUCTOR_BIN_DIR"
    cp "$KV_CONDUCTOR_PREBUILT" "$KV_CONDUCTOR_BIN"
    chmod +x "$KV_CONDUCTOR_BIN"
    echo "kv-conductor binary ready (pre-built): $KV_CONDUCTOR_BIN"
    _kv_ready="1"

elif [[ -f "$KV_CONDUCTOR_BIN" ]] && ! motor_var_is_explicit_zero SKIP_KV_CONDUCTOR_BUILD; then
    echo "kv-conductor binary ready (existing, skip cargo): $KV_CONDUCTOR_BIN"
    _kv_ready="1"

elif [[ "${SKIP_KV_CONDUCTOR_BUILD:-0}" == "1" ]]; then
    echo "SKIP_KV_CONDUCTOR_BUILD=1: skipping kv-conductor cargo build."

elif motor_cargo_usable && motor_zmq_available; then
    echo "Building kv-conductor from source (cargo build --release)..."
    if motor_try_kv_conductor_cargo_build "$KV_CONDUCTOR_DIR" "$KV_CONDUCTOR_BIN"; then
        echo "kv-conductor binary ready (cargo-built): $KV_CONDUCTOR_BIN"
        _kv_ready="1"
    else
        echo "[WARNING] kv-conductor compile failed (libzmq / g++ / zmq-sys); omitting this optional crate."
    fi

elif motor_cargo_usable; then
    echo "[WARNING] libzmq headers / pkg-config libzmq not found; skipping kv-conductor (conductor-only dep)."
    echo "  Official Motor images install libzmq3-dev (or zeromq-devel) + pkg-config."
    echo "  Explicit skip remains: SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh"
fi

if [[ "$_kv_ready" != "1" ]]; then
    if [[ -f "$KV_CONDUCTOR_BIN" ]]; then
        echo "kv-conductor binary ready (existing, no rebuild): $KV_CONDUCTOR_BIN"
    else
        rm -rf "$KV_CONDUCTOR_BIN_DIR"
        echo "kv-conductor omitted from the wheel (optional)."
        echo "  To pack it: install libzmq headers + pkg-config, or set KV_CONDUCTOR_PREBUILT."
    fi
fi

echo ""

echo "Building wheel package with pip wheel (PEP517)... (VERBOSE=${VERBOSE})"

# Default index stays tuna (master). Override when that host returns 403, e.g.
#   PIP_INDEX_URL=https://repo.huaweicloud.com/repository/pypi/simple
# Isolation still downloads setuptools from the index; if that fails, retry
# --no-build-isolation (needs setuptools/wheel already installed).
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
if [[ -z "${PIP_TRUSTED_HOST:-}" ]]; then
    _pip_host="${PIP_INDEX_URL#*://}"
    PIP_TRUSTED_HOST="${_pip_host%%/*}"
fi
echo "pip wheel index: ${PIP_INDEX_URL} (trusted-host=${PIP_TRUSTED_HOST})"

motor_pip_wheel() {
    local cmd=(python -m pip wheel . --no-deps --use-pep517 -w dist
        -i "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}")
    if [[ "${1:-}" == "no-isolation" ]]; then
        cmd+=(--no-build-isolation)
    fi
    if [[ "${VERBOSE}" -eq 0 ]]; then
        cmd+=(-q)
    fi
    "${cmd[@]}"
}

rm -rf dist/
mkdir -p dist
if ! motor_pip_wheel isolation; then
    echo "[WARNING] pep517 isolation failed (mirror 403/unreachable). Retrying with --no-build-isolation."
    # Isolation usually dies before writing a wheel; wipe anyway so a partial
    # motor-*.whl cannot sit next to the retry output. Dockerfile installs
    # dist/motor*.whl as a glob and must see exactly one file.
    rm -f dist/motor-*.whl
    motor_pip_wheel no-isolation
fi

WHEEL_PATH="$(ls -t dist/motor-*.whl 2>/dev/null | head -n1 || true)"
if [[ -z "${WHEEL_PATH}" || ! -f "${WHEEL_PATH}" ]]; then
    echo "[ERROR] pip wheel did not produce dist/motor-*.whl" >&2
    exit 1
fi
# Drop any extra motor-*.whl (same-name overwrite is the common case; keep a
# single gated file so pip install dist/motor*.whl cannot pick two archives).
for _extra in dist/motor-*.whl; do
    if [[ "${_extra}" != "${WHEEL_PATH}" ]]; then
        echo "[WARNING] removing extra wheel ${_extra}; keeping gated ${WHEEL_PATH}" >&2
        rm -f "${_extra}"
    fi
done
if ! PYTHONPATH="$(pwd)${PYTHONPATH:+:${PYTHONPATH}}" python -c \
    "from motor.coordinator.workload_shm_rs.wheel_gate import assert_motor_wheel_has_workload_shm; assert_motor_wheel_has_workload_shm(r'''${WHEEL_PATH}''')"
then
    echo "[ERROR] refusing to keep ${WHEEL_PATH}: archive is missing libmindie_workload_shm.so." >&2
    echo "  Coordinator cannot start without this library (no Python ledger fallback)." >&2
    rm -f "${WHEEL_PATH}"
    exit 1
fi
if [[ -f "$KV_CONDUCTOR_BIN" ]]; then
    if ! PYTHONPATH="$(pwd)${PYTHONPATH:+:${PYTHONPATH}}" python -c \
        "from motor.coordinator.workload_shm_rs.wheel_gate import assert_motor_wheel_has_kv_conductor; assert_motor_wheel_has_kv_conductor(r'''${WHEEL_PATH}''')"
    then
        echo "[ERROR] refusing to keep ${WHEEL_PATH}: kv-conductor was built but is missing from the archive." >&2
        rm -f "${WHEEL_PATH}"
        exit 1
    fi
    echo "wheel kv-conductor verified: ${WHEEL_PATH}"
fi
echo "wheel native lib verified: ${WHEEL_PATH}"

# pep517 emits motor-*-py3-none-any.whl (filename + WHEEL Tag). Native
# .so/binaries are arch-specific, so retag both to linux_x86_64 / linux_aarch64.
WHEEL_PATH="$(PYTHONPATH="$(pwd)${PYTHONPATH:+:${PYTHONPATH}}" python -c \
    "from motor.coordinator.workload_shm_rs.wheel_gate import retag_motor_wheel_filename; print(retag_motor_wheel_filename(r'''${WHEEL_PATH}''', r'''${MOTOR_VERSION}'''))")"
if [[ -z "${WHEEL_PATH}" || ! -f "${WHEEL_PATH}" ]]; then
    echo "[ERROR] failed to retag motor wheel with the host architecture" >&2
    exit 1
fi
echo "wheel package: ${WHEEL_PATH}"
