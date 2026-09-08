# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Guards native toolchain discovery: rustup/cargo must be usable, not just present."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENSURE_RUST = _REPO_ROOT / "scripts" / "ensure_rust.sh"
_SUBPROCESS_TIMEOUT_SEC = 10


def _isolated_env(home: Path) -> dict[str, str]:
    """PATH without the developer rustup; HOME/CARGO_HOME are the fake prefix.

    CI often runs as root with a real toolchain at /root/.cargo. Setting
    CARGO_HOME makes motor_source_cargo_env honor only that prefix (rustup
    semantics) instead of falling back to /root/.cargo.
    """
    return {
        "HOME": str(home),
        "CARGO_HOME": str(home / ".cargo"),
        "PATH": "/usr/bin:/bin",
        "TERM": "dumb",
        "LC_ALL": "C",
    }


def _run_ensure(home: Path, snippet: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", "-c", f"source '{_ENSURE_RUST}' && {snippet}"],
        check=False,
        capture_output=True,
        text=True,
        env=_isolated_env(home),
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )


def _write_fake_cargo(home: Path) -> Path:
    bin_dir = home / ".cargo" / "bin"
    bin_dir.mkdir(parents=True)
    cargo = bin_dir / "cargo"
    cargo.write_text("#!/bin/sh\necho fake-cargo\n", encoding="utf-8")
    cargo.chmod(cargo.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return cargo


def test_ensure_rust_script_is_present():
    """build.sh sources this file; a missing path would skip native compile on CI."""
    assert _ENSURE_RUST.is_file()


def test_motor_source_cargo_env_finds_home_cargo_when_not_on_path(tmp_path: Path):
    """Jenkins-style: rustup installed, job PATH omitted ~/.cargo/bin."""
    cargo = _write_fake_cargo(tmp_path)
    result = _run_ensure(tmp_path, "motor_source_cargo_env && command -v cargo")
    assert result.returncode == 0, result.stderr
    assert str(cargo) in result.stdout


def test_motor_source_cargo_env_fails_when_toolchain_absent(tmp_path: Path):
    """No silent success: missing cargo must be a failed lookup, not a skipped compile.

    CARGO_HOME in _isolated_env must hide /root/.cargo (CI-as-root rustup).
    """
    result = _run_ensure(tmp_path, "motor_source_cargo_env")
    assert result.returncode != 0


def test_motor_source_cargo_env_honors_cargo_home_without_home_or_root_fallback(tmp_path: Path):
    """Explicit CARGO_HOME is rustup's only prefix; HOME/.cargo must not leak in."""
    _write_fake_cargo(tmp_path)
    empty_home = tmp_path / "empty-cargo-home"
    empty_home.mkdir()
    env = _isolated_env(tmp_path)
    env["CARGO_HOME"] = str(empty_home)
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", f"source '{_ENSURE_RUST}' && motor_source_cargo_env"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode != 0


def test_skip_rust_install_does_not_attempt_network_install(tmp_path: Path):
    """Offline PREBUILT packing must be able to refuse rustup without curling mirrors."""
    env = _isolated_env(tmp_path)
    env["SKIP_RUST_INSTALL"] = "1"
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", f"source '{_ENSURE_RUST}' && motor_install_rustup"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode != 0
    assert "SKIP_RUST_INSTALL=1" in result.stderr


def test_skip_rust_build_shorthand_sets_both_per_crate_flags(tmp_path: Path):
    """No Rust changes since the last build: one knob reuses both native artifacts."""
    env = _isolated_env(tmp_path)
    env["SKIP_RUST_BUILD"] = "1"
    result = subprocess.run(  # noqa: S603
        [
            "bash",
            "-c",
            f"source '{_ENSURE_RUST}' && motor_apply_skip_rust_build_shorthand && "
            'echo "shm=$SKIP_WORKLOAD_SHM_BUILD kv=$SKIP_KV_CONDUCTOR_BUILD"',
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode == 0, result.stderr
    assert "shm=1 kv=1" in result.stdout


def test_motor_var_is_explicit_zero_false_when_unset(tmp_path: Path):
    """Unset SKIP_* must reuse existing artifacts, not treat missing as force-0."""
    result = _run_ensure(tmp_path, "motor_var_is_explicit_zero SKIP_WORKLOAD_SHM_BUILD")
    assert result.returncode != 0


def test_motor_var_is_explicit_zero_true_when_set_to_zero(tmp_path: Path):
    """SKIP_WORKLOAD_SHM_BUILD=0 is the only way to force cargo rebuild of an existing .so."""
    env = _isolated_env(tmp_path)
    env["SKIP_WORKLOAD_SHM_BUILD"] = "0"
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", f"source '{_ENSURE_RUST}' && motor_var_is_explicit_zero SKIP_WORKLOAD_SHM_BUILD"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode == 0


def test_motor_var_is_explicit_zero_false_when_set_to_one(tmp_path: Path):
    env = _isolated_env(tmp_path)
    env["SKIP_WORKLOAD_SHM_BUILD"] = "1"
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", f"source '{_ENSURE_RUST}' && motor_var_is_explicit_zero SKIP_WORKLOAD_SHM_BUILD"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode != 0


def test_skip_rust_build_shorthand_does_not_clobber_explicit_per_crate_override(tmp_path: Path):
    """An explicit SKIP_KV_CONDUCTOR_BUILD=0 must still force a kv-conductor rebuild."""
    env = _isolated_env(tmp_path)
    env["SKIP_RUST_BUILD"] = "1"
    env["SKIP_KV_CONDUCTOR_BUILD"] = "0"
    result = subprocess.run(  # noqa: S603
        [
            "bash",
            "-c",
            f"source '{_ENSURE_RUST}' && motor_apply_skip_rust_build_shorthand && "
            'echo "shm=$SKIP_WORKLOAD_SHM_BUILD kv=$SKIP_KV_CONDUCTOR_BUILD"',
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode == 0, result.stderr
    assert "shm=1 kv=0" in result.stdout


def test_skip_rust_build_shorthand_is_noop_when_unset(tmp_path: Path):
    """SKIP_RUST_BUILD unset must not fill SKIP_* flags; reuse vs compile is file-based."""
    env = _isolated_env(tmp_path)
    result = subprocess.run(  # noqa: S603
        [
            "bash",
            "-c",
            f"source '{_ENSURE_RUST}' && motor_apply_skip_rust_build_shorthand && "
            'echo "shm=${SKIP_WORKLOAD_SHM_BUILD:-unset} kv=${SKIP_KV_CONDUCTOR_BUILD:-unset}"',
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode == 0, result.stderr
    assert "shm=unset kv=unset" in result.stdout


def _write_fake_gxx(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    gxx = bin_dir / "g++"
    gxx.write_text("#!/bin/sh\necho fake-g++ 1.0\n", encoding="utf-8")
    gxx.chmod(gxx.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return gxx


def test_motor_ensure_cxx_exports_gxx_when_c_plusplus_missing(tmp_path: Path):
    """cc-rs looks up 'c++'; CI may only have g++. Export CXX so zmq-sys can compile."""
    gxx = _write_fake_gxx(tmp_path / "bin")
    env = _isolated_env(tmp_path)
    env["PATH"] = str(gxx.parent)
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f"source '{_ENSURE_RUST}' && motor_ensure_cxx && printf '%s\\n' \"$CXX\""],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode == 0, result.stderr
    assert str(gxx) in result.stdout


def _write_broken_rustup_cargo_shim(home: Path) -> Path:
    """Mimic rustup after a 503 rollback: proxy exists, cargo --version fails."""
    bin_dir = home / ".cargo" / "bin"
    bin_dir.mkdir(parents=True)
    cargo = bin_dir / "cargo"
    cargo.write_text(
        "#!/bin/sh\n"
        "echo \"error: rustup could not choose a version of cargo to run, "
        "because one wasn't specified explicitly, and no default is configured.\" >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    cargo.chmod(cargo.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return cargo


def test_motor_cargo_usable_rejects_rustup_shim_without_default_toolchain(tmp_path: Path):
    """CI 503 rollback must not be treated as a working cargo."""
    _write_broken_rustup_cargo_shim(tmp_path)
    result = _run_ensure(tmp_path, "motor_source_cargo_env && motor_cargo_usable")
    assert result.returncode != 0


def test_motor_ensure_cargo_fails_closed_when_shim_unusable_and_install_skipped(tmp_path: Path):
    """Do not print cargo-ready and continue into kv-conductor after a failed rustup."""
    _write_broken_rustup_cargo_shim(tmp_path)
    env = _isolated_env(tmp_path)
    env["SKIP_RUST_INSTALL"] = "1"
    env["PATH"] = f"{tmp_path / '.cargo' / 'bin'}:/usr/bin:/bin"
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", f"source '{_ENSURE_RUST}' && motor_ensure_cargo"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode != 0
    assert "cargo ready:" not in result.stdout


def _write_fake_pkg_config(bin_dir: Path, *, libzmq_exists: bool) -> Path:
    """pkg-config --exists libzmq probe used by motor_zmq_available."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    pkg_config = bin_dir / "pkg-config"
    if libzmq_exists:
        body = "#!/bin/sh\n[ \"$1\" = --exists ] && [ \"$2\" = libzmq ] && exit 0\nexit 1\n"
    else:
        body = "#!/bin/sh\nexit 1\n"
    pkg_config.write_text(body, encoding="utf-8")
    pkg_config.chmod(pkg_config.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return pkg_config


def test_motor_zmq_available_accepts_pkg_config_libzmq(tmp_path: Path):
    """cargo + libzmq must still take Mode 2 (pack kv-conductor) on official images."""
    pkg_config = _write_fake_pkg_config(tmp_path / "bin", libzmq_exists=True)
    env = _isolated_env(tmp_path)
    env["PATH"] = str(pkg_config.parent)
    env["MOTOR_ZMQ_HEADER_PATHS"] = "/no-such-zmq.h"
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f"source '{_ENSURE_RUST}' && motor_zmq_available"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode == 0, result.stderr


def test_motor_zmq_available_fails_when_pkg_config_and_headers_missing(tmp_path: Path):
    """Missing libzmq must not be treated as a hard build.sh failure."""
    pkg_config = _write_fake_pkg_config(tmp_path / "bin", libzmq_exists=False)
    env = _isolated_env(tmp_path)
    env["PATH"] = str(pkg_config.parent)
    env["MOTOR_ZMQ_HEADER_PATHS"] = "/no-such-zmq.h"
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f"source '{_ENSURE_RUST}' && motor_zmq_available"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode != 0


def test_motor_zmq_available_accepts_zmq_h_without_pkg_config(tmp_path: Path):
    """Missing pkg-config is not fatal when a well-known zmq.h is present."""
    header = tmp_path / "include" / "zmq.h"
    header.parent.mkdir(parents=True)
    header.write_text("/* fake zmq.h */\n", encoding="utf-8")
    env = _isolated_env(tmp_path)
    env["PATH"] = str(tmp_path / "empty-bin")
    env["MOTOR_ZMQ_HEADER_PATHS"] = str(header)
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f"source '{_ENSURE_RUST}' && motor_zmq_available"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode == 0, result.stderr


def test_failed_kv_conductor_cargo_does_not_abort_caller(tmp_path: Path):
    """Conductor-only: zmq-sys/link cargo failure returns 1; caller must keep going (SHM already decided)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo = bin_dir / "cargo"
    cargo.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    cargo.chmod(cargo.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    gxx = bin_dir / "g++"
    gxx.write_text("#!/bin/sh\necho 'g++ (fake) 1.0'\n", encoding="utf-8")
    gxx.chmod(gxx.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    crate = tmp_path / "kv_conductor"
    crate.mkdir()
    dest = tmp_path / "out" / "kv-conductor"
    env = _isolated_env(tmp_path)
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["SKIP_CXX_INSTALL"] = "1"
    result = subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            "-c",
            (
                f"source '{_ENSURE_RUST}' && "
                f"motor_try_kv_conductor_cargo_build '{crate}' '{dest}'; "
                "rc=$?; echo PARENT_CONTINUED rc=$rc"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode == 0, result.stderr
    assert "PARENT_CONTINUED rc=1" in result.stdout
    assert "skipping conductor" in result.stderr
    assert not dest.exists()


def test_skip_cxx_install_does_not_attempt_package_install(tmp_path: Path):
    """Locked CI images must be able to refuse apt/dnf without touching the system."""
    env = _isolated_env(tmp_path)
    env["PATH"] = "/empty-path-no-compiler"
    env["SKIP_CXX_INSTALL"] = "1"
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f"source '{_ENSURE_RUST}' && motor_ensure_cxx"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
    )
    assert result.returncode != 0
    assert "SKIP_CXX_INSTALL=1" in result.stderr
