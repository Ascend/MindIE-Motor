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

# Native toolchain helpers sourced by build.sh (same shell so PATH/CXX persist):
#   motor_ensure_cargo — find a *usable* rustup cargo or install it (rsproxy by default)
#   motor_ensure_cxx   — find c++/g++ or apt/dnf install g++ (kv-conductor zmq-sys)
#   motor_zmq_available — libzmq headers via pkg-config or zmq.h (missing pkg-config
#                         is not fatal; build.sh then auto-skips kv-conductor)
#   motor_try_kv_conductor_cargo_build — conductor-only compile; g++/zmq-sys
#                         failure returns 1 (do not treat missing cargo as a skip)
# A rustup proxy at ~/.cargo/bin/cargo is not enough: after a 5xx component download
# rustup rolls back the toolchain but leaves the shim, and `cargo --version` fails
# with "no default is configured". Treat that as missing cargo.
# Override rustup with RUSTUP_DIST_SERVER / RUSTUP_UPDATE_ROOT / RUSTUP_INIT_URL.
# SKIP_RUST_INSTALL=1 / SKIP_CXX_INSTALL=1 disable the matching network install.
# CARGO_HOME, when set, is the only cargo prefix (no HOME / /root fallback).

motor_source_cargo_env() {
    if command -v cargo >/dev/null 2>&1; then
        return 0
    fi

    local env_file bin_dir
    # rustup: CARGO_HOME is the sole prefix. Honor that and do not also probe
    # HOME or /root (CI-as-root unit tests point CARGO_HOME at an empty tmp dir).
    if [[ -n "${CARGO_HOME:-}" ]]; then
        env_file="${CARGO_HOME}/env"
        if [[ -f "${env_file}" && -r "${env_file}" ]]; then
            # shellcheck disable=SC1090
            source "${env_file}"
            if command -v cargo >/dev/null 2>&1; then
                return 0
            fi
        fi
        bin_dir="${CARGO_HOME}/bin"
        if [[ -x "${bin_dir}/cargo" ]]; then
            export PATH="${bin_dir}:${PATH}"
            return 0
        fi
        return 1
    fi

    for env_file in \
        "${HOME}/.cargo/env" \
        "/root/.cargo/env"; do
        if [[ -n "${env_file}" && -f "${env_file}" && -r "${env_file}" ]]; then
            # shellcheck disable=SC1090
            source "${env_file}"
            if command -v cargo >/dev/null 2>&1; then
                return 0
            fi
        fi
    done

    for bin_dir in \
        "${HOME}/.cargo/bin" \
        "/root/.cargo/bin"; do
        if [[ -n "${bin_dir}" && -x "${bin_dir}/cargo" ]]; then
            export PATH="${bin_dir}:${PATH}"
            return 0
        fi
    done
    return 1
}

# rustup's cargo/rustc shims exist after a failed install; only a working
# `cargo --version` means a default toolchain was actually downloaded.
motor_cargo_usable() {
    command -v cargo >/dev/null 2>&1 || return 1
    cargo --version >/dev/null 2>&1
}

motor_report_cargo() {
    echo "cargo ready: $(command -v cargo) ($(cargo --version 2>/dev/null || echo unknown))"
}

_motor_run_rustup_init() {
    local init_url="$1"
    local toolchain="$2"
    if command -v curl >/dev/null 2>&1; then
        curl --proto '=https' --tlsv1.2 -sSf "${init_url}" | sh -s -- -y --default-toolchain "${toolchain}" --no-modify-path
        return "${PIPESTATUS[1]:-1}"
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -qO- "${init_url}" | sh -s -- -y --default-toolchain "${toolchain}" --no-modify-path
        return "${PIPESTATUS[1]:-1}"
    fi
    echo "[ERROR] curl or wget is required to install rustup." >&2
    return 1
}

_motor_try_rustup_default() {
    local toolchain="${RUSTUP_TOOLCHAIN:-stable}"
    command -v rustup >/dev/null 2>&1 || return 1
    echo "rustup is present but cargo has no default toolchain; running rustup default ${toolchain}..."
    rustup default "${toolchain}"
    motor_source_cargo_env || true
    motor_cargo_usable
}

motor_install_rustup() {
    if [[ "${SKIP_RUST_INSTALL:-0}" == "1" ]]; then
        echo "[ERROR] cargo not found and SKIP_RUST_INSTALL=1; not installing rustup." >&2
        return 1
    fi

    export RUSTUP_DIST_SERVER="${RUSTUP_DIST_SERVER:-https://rsproxy.cn}"
    export RUSTUP_UPDATE_ROOT="${RUSTUP_UPDATE_ROOT:-https://rsproxy.cn/rustup}"
    local init_url="${RUSTUP_INIT_URL:-https://rsproxy.cn/rustup-init.sh}"
    local toolchain="${RUSTUP_TOOLCHAIN:-stable}"
    local attempts="${RUSTUP_INSTALL_ATTEMPTS:-3}"
    local sleep_sec="${RUSTUP_RETRY_SLEEP_SEC:-3}"
    local attempt rc

    echo "Installing Rust toolchain via rustup (needed to compile libmindie_workload_shm.so)..."
    echo "  RUSTUP_DIST_SERVER=${RUSTUP_DIST_SERVER}"
    echo "  RUSTUP_INIT_URL=${init_url}"

    attempt=1
    while [[ "${attempt}" -le "${attempts}" ]]; do
        echo "  rustup-init attempt ${attempt}/${attempts}"
        if _motor_run_rustup_init "${init_url}" "${toolchain}"; then
            motor_source_cargo_env || true
            if motor_cargo_usable; then
                return 0
            fi
            echo "[WARNING] rustup-init exited 0 but cargo --version failed (incomplete toolchain)." >&2
        else
            echo "[WARNING] rustup-init failed (often a 5xx from ${RUSTUP_DIST_SERVER})." >&2
        fi
        if _motor_try_rustup_default; then
            return 0
        fi
        if [[ "${attempt}" -lt "${attempts}" ]]; then
            echo "  retrying rustup in ${sleep_sec}s..."
            sleep "${sleep_sec}"
        fi
        attempt=$((attempt + 1))
    done

    if [[ "${RUSTUP_FALLBACK_OFFICIAL:-1}" == "1" && "${RUSTUP_DIST_SERVER}" == "https://rsproxy.cn" ]]; then
        echo "[WARNING] rsproxy rustup failed; falling back to official static.rust-lang.org." >&2
        export RUSTUP_DIST_SERVER="https://static.rust-lang.org"
        export RUSTUP_UPDATE_ROOT="https://static.rust-lang.org/rustup"
        init_url="https://static.rust-lang.org/rustup/rustup-init.sh"
        if _motor_run_rustup_init "${init_url}" "${toolchain}"; then
            motor_source_cargo_env || true
            if motor_cargo_usable; then
                return 0
            fi
        fi
        _motor_try_rustup_default && return 0
    fi

    echo "[ERROR] rustup did not produce a working cargo (cargo --version failed)." >&2
    echo "  A rustup 503/rollback leaves ~/.cargo/bin/cargo as a proxy with no default toolchain." >&2
    echo "  Retry the job, or set RUSTUP_DIST_SERVER / RUSTUP_INIT_URL to a reachable mirror." >&2
    return 1
}

motor_ensure_cargo() {
    motor_source_cargo_env || true
    if motor_cargo_usable; then
        motor_report_cargo
        return 0
    fi
    if _motor_try_rustup_default; then
        motor_report_cargo
        return 0
    fi
    if ! motor_install_rustup; then
        return 1
    fi
    if ! motor_cargo_usable; then
        echo "[ERROR] rustup finished but cargo --version still fails." >&2
        return 1
    fi
    motor_report_cargo
    return 0
}

motor_cxx_on_path() {
    command -v c++ >/dev/null 2>&1 \
        || command -v g++ >/dev/null 2>&1 \
        || command -v clang++ >/dev/null 2>&1
}

motor_export_cxx() {
    if [[ -n "${CXX:-}" ]] && command -v "${CXX}" >/dev/null 2>&1; then
        export CXX
        return 0
    fi
    if command -v c++ >/dev/null 2>&1; then
        export CXX=c++
    elif command -v g++ >/dev/null 2>&1; then
        CXX="$(command -v g++)"
        export CXX
    elif command -v clang++ >/dev/null 2>&1; then
        CXX="$(command -v clang++)"
        export CXX
    fi
}

motor_report_cxx() {
    motor_export_cxx
    local ver
    ver="$(${CXX} --version 2>/dev/null || true)"
    echo "c++ ready: ${CXX} (${ver%%$'\n'*})"
}

motor_install_gxx() {
    if [[ "${SKIP_CXX_INSTALL:-0}" == "1" ]]; then
        echo "[ERROR] C++ compiler not found and SKIP_CXX_INSTALL=1; not installing g++." >&2
        return 1
    fi

    echo "Installing g++ (kv-conductor zmq-sys needs the c++ tool)..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends g++
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y gcc-c++
    elif command -v yum >/dev/null 2>&1; then
        yum install -y gcc-c++
    else
        echo "[ERROR] no supported package manager was found to install g++." >&2
        echo "  Ubuntu: apt-get install -y g++   (or build-essential)" >&2
        echo "  openEuler: dnf/yum install -y gcc-c++" >&2
        return 1
    fi
}

motor_ensure_cxx() {
    if motor_cxx_on_path; then
        motor_report_cxx
        return 0
    fi
    if ! motor_install_gxx; then
        return 1
    fi
    hash -r 2>/dev/null || true
    if ! motor_cxx_on_path; then
        echo "[ERROR] g++ install finished but c++/g++ is still not on PATH." >&2
        return 1
    fi
    motor_report_cxx
}

# kv-conductor's zmq-sys needs libzmq. Probe before cargo so a CI image without
# headers can skip conductor instead of failing the whole wheel. Do not apt-install
# libzmq here. Missing pkg-config is not fatal: fall through to well-known zmq.h
# paths. MOTOR_ZMQ_HEADER_PATHS (space-separated) overrides the default list
# (tests / custom prefixes).
motor_zmq_available() {
    if command -v pkg-config >/dev/null 2>&1; then
        if pkg-config --exists libzmq >/dev/null 2>&1; then
            return 0
        fi
    fi
    local hdr
    # shellcheck disable=SC2086
    for hdr in ${MOTOR_ZMQ_HEADER_PATHS:-/usr/include/zmq.h /usr/local/include/zmq.h}; do
        if [[ -f "${hdr}" ]]; then
            return 0
        fi
    done
    return 1
}

# Conductor-only compile attempt. Caller must already have a usable cargo
# (workload-shm uses the same cargo and is not optional). Return 1 on g++ /
# zmq-sys / link failure so build.sh can omit this crate only. Never exit 1
# for "cargo missing" — that is handled as a hard SHM/toolchain failure.
motor_try_kv_conductor_cargo_build() {
    local crate_dir="${1:?}"
    local dest="${2:?}"
    if ! motor_ensure_cxx; then
        echo "[WARNING] kv-conductor needs a C++ compiler for zmq-sys (g++ / c++); skipping conductor." >&2
        echo "  Ubuntu: apt-get install -y g++   (or build-essential)" >&2
        echo "  openEuler: dnf/yum install -y gcc-c++" >&2
        return 1
    fi
    if ! (
        cd "${crate_dir}" || exit 1
        cargo build --release
    ); then
        echo "[WARNING] kv-conductor cargo build --release failed (typically libzmq / zmq-sys); skipping conductor." >&2
        return 1
    fi
    local built="${crate_dir}/target/release/kv-conductor"
    if [[ ! -x "${built}" ]]; then
        echo "[WARNING] kv-conductor cargo build produced no executable; skipping conductor." >&2
        return 1
    fi
    mkdir -p "$(dirname "${dest}")"
    cp "${built}" "${dest}"
    chmod +x "${dest}"
    return 0
}

# True only when NAME is set to 0 in the environment (not unset). build.sh reuses
# existing lib/*.so and bin/kv-conductor by default; SKIP_*=0 forces cargo rebuild.
motor_var_is_explicit_zero() {
    local name="${1:?}"
    [[ "${!name+set}" == "set" && "${!name}" == "0" ]]
}

# Convenience single knob: SKIP_RUST_BUILD=1 means "no Rust source changed since
# the last successful build.sh run; skip cargo entirely for both crates and reuse
# whatever is already in workload_shm_rs/lib/ and kv_conductor/bin/". It only sets
# the per-crate flags when they were not explicitly set, so an explicit
# SKIP_WORKLOAD_SHM_BUILD=0 / SKIP_KV_CONDUCTOR_BUILD=0 still forces a rebuild of
# that one crate. It does NOT authorize shipping a wheel without workload-shm: if
# lib/libmindie_workload_shm.so is missing, build.sh ignores the skip and rebuilds
# (see the workload-shm section), so a first-time build still works unattended.
# Default bash build.sh already reuses present artifacts; this flag is for
# callers that want to skip cargo even when they also pass other SKIP_*=0 later.
motor_apply_skip_rust_build_shorthand() {
    if [[ "${SKIP_RUST_BUILD:-0}" == "1" ]]; then
        : "${SKIP_WORKLOAD_SHM_BUILD:=1}"
        : "${SKIP_KV_CONDUCTOR_BUILD:=1}"
        export SKIP_WORKLOAD_SHM_BUILD
        export SKIP_KV_CONDUCTOR_BUILD
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    set -euo pipefail
    motor_ensure_cargo
fi
