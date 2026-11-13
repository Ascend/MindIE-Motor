# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Script-level checks for ``gen_kv_store_config`` in ``startup/common.sh``.

The single-container Pod runs the engine and ``mooncake_master`` together, so the legacy
path must rewrite ``KVS_MASTER_SERVICE`` to ``POD_IP``; with an external master
(``KV_STORE_EXTERNAL=1``) that rewrite must not happen.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_COMMON_SH = Path(__file__).resolve().parents[3] / "examples" / "deployer" / "startup" / "common.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required to exercise the shell function")


def _function_text() -> str:
    lines = _COMMON_SH.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("gen_kv_store_config()"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "}")
    return "".join(lines[start : end + 1])


@pytest.mark.parametrize(
    ("master_env", "external", "expected"),
    [("mindie-motor-kvs-master", "0", "10.0.0.1"), ("", "1", "")],
)
def test_gen_kv_store_config_master_env(tmp_path, master_env, external, expected):
    """Single-container rewrites to POD_IP; an external master keeps the env untouched."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    record = tmp_path / "record.txt"
    fake_python = fake_bin / "python3"
    fake_python.write_text('#!/bin/bash\nprintf "%s" "${KVS_MASTER_SERVICE:-}" > "$RECORD"\n', encoding="utf-8")
    fake_python.chmod(0o755)

    script = tmp_path / "run.sh"
    script.write_text(
        "set -e\n"
        f"{_function_text()}\n"
        "export ROLE=SINGLE_CONTAINER POD_IP=10.0.0.1 LD_LIBRARY_PATH=''\n"
        f'export KVS_MASTER_SERVICE="{master_env}" KV_STORE_EXTERNAL={external}\n'
        f'export CONFIG_PATH="{tmp_path}" CONFIGMAP_PATH="{tmp_path}" USER_CONFIG_PATH="{tmp_path}/user_config.json"\n'
        "gen_kv_store_config\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["RECORD"] = str(record)
    subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True, text=True)

    assert record.read_text(encoding="utf-8") == expected
