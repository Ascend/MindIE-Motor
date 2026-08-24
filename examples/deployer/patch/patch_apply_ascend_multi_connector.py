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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

try:
    import vllm_ascend
except ImportError:  # noqa: PERF203
    vllm_ascend = None

# AscendMultiConnector kv-event proxy: one version-agnostic patch applies to
# every supported vLLM base version (the vllm-ascend class structure is
# stable across v0.20.2rc ~ v0.26.0rc).
TARGET_VLLM_VERSIONS = ("0.20.2", "0.21.0", "0.22.1", "0.23.0", "0.24.0", "0.25.0", "0.25.1", "0.26.0")

# Patch list: (rel_path_in_vllm_ascend, patch_filename, marker)
# marker must be unique to this patch (not just the method name, which may
# appear in upstream TODO comments) so is_patched never false-positives.
PATCH_SPECS = [
    (
        "distributed/kv_transfer/ascend_multi_connector.py",
        "vllm_ascend_multi_connector_kv_events.patch",
        "# motor-patch: kv-events proxy (do not remove)",
    ),
]


def should_apply_patch() -> bool:
    """Return True when the installed vLLM base version should be patched."""
    version = md.version("vllm")
    if version.split("+")[0].split("-")[0] not in TARGET_VLLM_VERSIONS:
        logger.info("Skip AscendMultiConnector patch: vLLM %s is not in %s", version, TARGET_VLLM_VERSIONS)
        return False
    logger.info("Applying AscendMultiConnector patch for vLLM %s", version)
    return True


def is_patched(path: str, marker: str) -> bool:
    """Return True if the target file is already patched and remains valid Python."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if marker not in content:
            return False
        ast.parse(content)
        return True
    except (OSError, SyntaxError):
        return False


def apply_patch(target_file: str, patch_file: str, marker: str) -> bool:
    """Apply a patch to a single vllm-ascend source file; skip if already patched."""
    patch_bin = shutil.which("patch")
    if not patch_bin:
        logger.error("patch command not found in PATH")
        return False

    result = subprocess.run(
        [patch_bin, "-p0", "--ignore-whitespace", target_file, patch_file],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        logger.info("Patch applied successfully to %s", target_file)
        return True
    if is_patched(target_file, marker):
        logger.info("Already patched: %s", target_file)
        return True
    logger.error("Failed to apply patch to %s\n%s", target_file, result.stderr.strip())
    return False


def main() -> int:
    """Apply all patches in PATCH_SPECS; return 0 on success or skip, 1 on failure."""
    if not should_apply_patch():
        return 0
    if vllm_ascend is None:
        logger.info("Skip AscendMultiConnector patch: vllm_ascend is not installed")
        return 0

    script_dir = os.path.dirname(os.path.abspath(__file__))
    ascend_root = vllm_ascend.__path__[0]
    failed = 0
    for rel_path, patch_name, marker in PATCH_SPECS:
        patch_path = os.path.join(script_dir, patch_name)
        if not os.path.isfile(patch_path):
            continue
        if not apply_patch(os.path.join(ascend_root, rel_path), patch_path, marker):
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
