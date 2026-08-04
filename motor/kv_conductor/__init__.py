# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""KV Conductor — Radix-tree-based KV cache indexer for MindIE-PyMotor.

Provides binary discovery and a convenience :func:`start` function for
launching the ``kv-conductor`` service as a subprocess.

Usage::

    from motor.kv_conductor import is_available, start

    if is_available():
        proc = start(port=13333)
        # ... later
        proc.terminate()

Start via CLI::

    python -m motor.kv_conductor --port 13333
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_BIN_NAME = "kv-conductor"


def get_binary_path() -> Path | None:
    """Return the path to the kv-conductor binary, or *None* if not found.

    Resolution order:
    1. Bundled binary (``motor/kv_conductor/bin/kv-conductor`` in the whl)
    2. Cargo build output (``motor/kv_conductor/target/release/kv-conductor``)
    3. ``$PATH`` lookup
    """
    here = Path(__file__).resolve().parent

    # 1. Bundled binary (packaged by build.sh)
    bundled = here / "bin" / _BIN_NAME
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return bundled

    # 2. Cargo release build (development convenience)
    cargo_release = here / "target" / "release" / _BIN_NAME
    if cargo_release.is_file() and os.access(cargo_release, os.X_OK):
        return cargo_release

    # 3. On PATH
    which = shutil.which(_BIN_NAME)
    if which:
        return Path(which).resolve()

    return None


def is_available() -> bool:
    """Return *True* if the kv-conductor binary can be found."""
    return get_binary_path() is not None


def start(
    host: str = "::",
    port: int = 13333,
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    """Launch kv-conductor as a subprocess.

    Parameters
    ----------
    host:
        Bind address (default ``::`` — dual-stack IPv4/IPv6).
    port:
        HTTP listen port.
    extra_args:
        Additional CLI arguments forwarded to the binary.

    Returns
    -------
    subprocess.Popen
        Handle to the running process.

    Raises
    ------
    RuntimeError
        If the kv-conductor binary cannot be found.
    """
    binary = get_binary_path()
    if binary is None:
        raise RuntimeError(
            "kv-conductor binary not found. Rebuild with: cd motor/kv_conductor && cargo build --release"
        )

    cmd = [
        str(binary),
        "--host",
        host,
        "--port",
        str(port),
    ]
    if extra_args:
        cmd.extend(extra_args)

    env = os.environ.copy()
    env.setdefault("RUST_LOG", "info")

    logger.info("Starting kv-conductor: %s", " ".join(cmd))
    return subprocess.Popen(cmd, env=env)
