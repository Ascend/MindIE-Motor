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

# --- Conditional kv-conductor build ---
# Priority:
#   1. KV_CONDUCTOR_PREBUILT env var — path to a pre-built binary
#   2. Build from source via cargo (if available) — always rebuilds
#   3. motor/kv_conductor/bin/kv-conductor already exists (no cargo; manual copy)
#   4. Skip — wheel built without kv-conductor (optional component)
#
# Set SKIP_KV_CONDUCTOR_BUILD=1 to skip cargo build even when cargo is
# available (use the existing bin/kv-conductor, or skip if none).

KV_CONDUCTOR_DIR="./motor/kv_conductor"
KV_CONDUCTOR_BIN_DIR="$KV_CONDUCTOR_DIR/bin"
KV_CONDUCTOR_BIN="$KV_CONDUCTOR_BIN_DIR/kv-conductor"

echo "=== kv-conductor ==="

if [[ -n "${KV_CONDUCTOR_PREBUILT:-}" ]]; then
    # Mode 1: use user-supplied pre-built binary.
    if [[ ! -f "$KV_CONDUCTOR_PREBUILT" ]]; then
        echo "[ERROR] KV_CONDUCTOR_PREBUILT='$KV_CONDUCTOR_PREBUILT' does not exist."
        exit 1
    fi
    mkdir -p "$KV_CONDUCTOR_BIN_DIR"
    cp "$KV_CONDUCTOR_PREBUILT" "$KV_CONDUCTOR_BIN"
    chmod +x "$KV_CONDUCTOR_BIN"
    echo "kv-conductor binary ready (pre-built): $KV_CONDUCTOR_BIN"

elif command -v cargo >/dev/null 2>&1 && [[ "${SKIP_KV_CONDUCTOR_BUILD:-0}" != "1" ]]; then
    # Mode 2: build from source (always rebuilds when cargo is available).
    echo "Building kv-conductor from source (cargo build --release)..."
    (
        cd "$KV_CONDUCTOR_DIR" || exit 1
        cargo build --release
    )
    mkdir -p "$KV_CONDUCTOR_BIN_DIR"
    cp "$KV_CONDUCTOR_DIR/target/release/kv-conductor" "$KV_CONDUCTOR_BIN"
    chmod +x "$KV_CONDUCTOR_BIN"
    echo "kv-conductor binary ready (cargo-built): $KV_CONDUCTOR_BIN"

elif [[ -f "$KV_CONDUCTOR_BIN" ]]; then
    # Mode 3: binary already in place (no cargo; e.g. manual copy or CI artifact).
    echo "kv-conductor binary ready (existing, no rebuild): $KV_CONDUCTOR_BIN"

else
    # Mode 4: no binary available — skip.
    rm -rf "$KV_CONDUCTOR_BIN_DIR"
    echo "[WARNING] kv-conductor binary not found and cargo unavailable."
    echo "  Options:"
    echo "    1. KV_CONDUCTOR_PREBUILT=/path/to/kv-conductor bash build.sh"
    echo "    2. cp /path/to/kv-conductor motor/kv_conductor/bin/ && bash build.sh"
    echo "    3. Install Rust: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
fi

echo ""

# --- Conditional workload-shm (coordinator) build ---
# Same priority chain as kv-conductor:
#   1. WORKLOAD_SHM_PREBUILT env var — path to a pre-built .so
#   2. build from source via cargo (unless SKIP_WORKLOAD_SHM_BUILD=1)
#   3. motor/coordinator/workload_shm_rs/lib/libmindie_workload_shm.so already present
#   4. skip — wheel without the .so; the Python runtime raises a clear error (scheduling unavailable),
#      it never silently falls back to a wrong ledger.

WORKLOAD_SHM_DIR="./motor/coordinator/workload_shm_rs"
WORKLOAD_SHM_LIB_DIR="$WORKLOAD_SHM_DIR/lib"
WORKLOAD_SHM_LIB="$WORKLOAD_SHM_LIB_DIR/libmindie_workload_shm.so"

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

elif command -v cargo >/dev/null 2>&1 && [[ "${SKIP_WORKLOAD_SHM_BUILD:-0}" != "1" ]]; then
    echo "Building workload-shm from source (cargo build --release)..."
    (
        cd "$WORKLOAD_SHM_DIR" || exit 1
        cargo build --release
    )
    mkdir -p "$WORKLOAD_SHM_LIB_DIR"
    cp "$WORKLOAD_SHM_DIR/target/release/libmindie_workload_shm.so" "$WORKLOAD_SHM_LIB"
    chmod +x "$WORKLOAD_SHM_LIB"
    echo "workload-shm library ready (cargo-built): $WORKLOAD_SHM_LIB"

elif [[ -f "$WORKLOAD_SHM_LIB" ]]; then
    echo "workload-shm library ready (existing, no rebuild): $WORKLOAD_SHM_LIB"

else
    rm -rf "$WORKLOAD_SHM_LIB_DIR"
    echo "[WARNING] workload-shm .so not found and cargo unavailable."
    echo "  The coordinator scheduler will raise a clear error at runtime (no silent fallback)."
    echo "  Options:"
    echo "    1. WORKLOAD_SHM_PREBUILT=/path/to/libmindie_workload_shm.so bash build.sh"
    echo "    2. cp /path/to/libmindie_workload_shm.so $WORKLOAD_SHM_LIB_DIR/ && bash build.sh"
    echo "    3. Install Rust: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
fi

echo ""

echo "Building wheel package with pip wheel (PEP517)... (VERBOSE=${VERBOSE})"

# Use pep517 build interface to avoid legacy setup.py warning. if no network, need add "--no-build-isolation"
cmd=(python -m pip wheel . --no-deps --use-pep517 -w dist -i https://pypi.tuna.tsinghua.edu.cn/simple)
if [[ "${VERBOSE}" -eq 0 ]]; then
  cmd+=(-q) # quiet output by default
fi

"${cmd[@]}"
