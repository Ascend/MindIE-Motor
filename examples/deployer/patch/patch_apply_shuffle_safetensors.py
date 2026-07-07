# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


import ast
import importlib.metadata as md
import logging
import os
import shutil
import subprocess
import sys

import vllm

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Patch only when the installed vLLM base version matches
TARGET_VLLM_VERSIONS = ("0.20.2", "0.21.0", "0.22.1", "0.23.0")

# Patch list
PATCH_SPECS = [
    ("config/load.py", "vllm_shuffle_load_config.patch"),
    ("model_executor/model_loader/default_loader.py", "vllm_shuffle_default_loader.patch"),
    ("model_executor/model_loader/weight_utils.py", "vllm_shuffle_weight_utils.patch"),
]


def should_apply_patch() -> bool:
    """Return True when the installed vLLM version should be patched."""
    version = md.version("vllm")
    if version.split("+")[0].split("-")[0] not in TARGET_VLLM_VERSIONS:
        logger.info("Skip shuffle safetensors patch: vLLM %s is not in %s", version, TARGET_VLLM_VERSIONS)
        return False
    logger.info("Applying shuffle safetensors patch for vLLM %s", version)
    return True


def is_shuffle_patched(path: str) -> bool:
    """Return True if the target file is already patched and remains valid Python."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if "shuffle_safetensors_files" not in content:
            return False
        ast.parse(content)
        return True
    except (OSError, SyntaxError):
        return False


def apply_patch(target_file: str, patch_file: str) -> bool:
    """Apply a patch to a single vLLM source file; skip if already patched."""
    patch_bin = shutil.which("patch")
    if not patch_bin:
        logger.error("patch command not found in PATH")
        return False

    result = subprocess.run(
        [patch_bin, "-p0", "--fuzz=500", "--ignore-whitespace", target_file, patch_file],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        logger.info("Patch applied successfully to %s", target_file)
        return True
    if is_shuffle_patched(target_file):
        logger.info("Already patched: %s", target_file)
        return True
    logger.error("Failed to apply patch to %s\n%s", target_file, result.stderr.strip())
    return False


def main() -> int:
    """Apply all patches in PATCH_SPECS; return 0 on success or skip, 1 on failure."""
    if not should_apply_patch():
        return 0

    script_dir = os.path.dirname(os.path.abspath(__file__))
    version = md.version("vllm").split("+")[0].split("-")[0]
    patch_dir = os.path.join(script_dir, version)
    vllm_root = vllm.__path__[0]
    failed = 0
    for rel_path, patch_name in PATCH_SPECS:
        if not apply_patch(os.path.join(vllm_root, rel_path), os.path.join(patch_dir, patch_name)):
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
