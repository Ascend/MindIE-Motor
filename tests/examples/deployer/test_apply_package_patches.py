# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "examples/deployer/patch/0.30.0/apply_patches.sh"


@pytest.fixture(params=["standalone", "nested", "editable"])
def package_installation(tmp_path, request):
    """Use real git apply with fake pip metadata, without importing either engine."""
    root = tmp_path / "installation"
    root.mkdir()
    if request.param == "nested":
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    location = root / "site-packages"
    location.mkdir()
    patches = []
    targets = []
    for module in ("vllm", "vllm_ascend"):
        target = location / module / "example.py"
        target.parent.mkdir()
        target.write_text("before\n")
        targets.append(target)
        patch = tmp_path / f"{module}.patch"
        patch.write_text(
            f"diff --git a/{module}/example.py b/{module}/example.py\n"
            f"--- a/{module}/example.py\n+++ b/{module}/example.py\n"
            "@@ -1 +1 @@\n-before\n+after\n"
        )
        patches.append(patch)
    if request.param == "editable":
        subprocess.run(["git", "init", "-q", str(location)], check=True)
    python = tmp_path / "python"
    python.write_text(
        '#!/bin/sh\n'
        'printf "Version: 0.30.0\\nLocation: %s\\n" "$PATCH_TEST_LOCATION"\n'
        'if [ "$PATCH_TEST_EDITABLE" = 1 ]; then\n'
        '    printf "Editable project location: %s\\n" "$PATCH_TEST_LOCATION"\n'
        'fi\n'
    )
    python.chmod(0o755)
    env = os.environ.copy()
    # Do not inherit repository discovery settings from the test runner.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CEILING_DIRECTORIES"):
        env.pop(name, None)
    env.update(
        PYTHON=str(python),
        PATCH_TEST_LOCATION=str(location),
        PATCH_TEST_EDITABLE=str(int(request.param == "editable")),
    )
    return patches, targets, env


def test_package_patches_apply_and_repeated_application_preserves_files(package_installation):
    patches, targets, env = package_installation
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(SCRIPT), *map(str, patches)], env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert [target.read_text() for target in targets] == ["after\n", "after\n"]


def test_second_package_conflict_leaves_first_package_unchanged(package_installation):
    patches, targets, env = package_installation
    targets[1].write_text("conflict\n")
    result = subprocess.run(
        ["bash", str(SCRIPT), *map(str, patches)], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert [target.read_text() for target in targets] == ["before\n", "conflict\n"]
