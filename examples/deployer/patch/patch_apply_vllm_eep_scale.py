# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


"""Forward Motor Elastic EP membership into vLLM ``/scale_elastic_ep``.

Motor identity-preserving recover fills holes (survivors keep original DP
ids). The engine must therefore accept ``existing_dp_ranks`` and
``data_parallel_master_rank`` from the HTTP body instead of assuming new
ranks are appended at the high end, or that the original master is still
alive.
"""

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
    import vllm
except ImportError:  # noqa: PERF203
    vllm = None

TARGET_VLLM_VERSIONS = ("0.30.0",)

PATCH_SPECS = [
    (
        "entrypoints/serve/elastic_ep/api_router.py",
        "vllm_eep_scale_api_router.patch",
        "# motor-patch: forward Motor surviving ranks / master into Elastic EP",
    ),
    (
        "engine/protocol.py",
        "vllm_eep_scale_protocol.patch",
        "# motor-patch: eep membership kwargs (do not remove)",
    ),
    (
        "v1/engine/async_llm.py",
        "vllm_eep_scale_async_llm.patch",
        "# motor-patch: honor Motor surviving master rank",
    ),
]


def normalize_vllm_version(version: str) -> str:
    """Strip local/build suffixes such as ``0.30.0+empty`` or ``0.30.0-rc1``."""
    return version.split("+")[0].split("-")[0]


def should_apply_patch() -> bool:
    """Return True when the installed vLLM base version should be patched."""
    if vllm is None:
        logger.info("Skip Elastic EP scale patch: vLLM is not installed")
        return False
    try:
        version = md.version("vllm")
    except md.PackageNotFoundError:
        logger.info("Skip Elastic EP scale patch: vLLM is not installed")
        return False
    if normalize_vllm_version(version) not in TARGET_VLLM_VERSIONS:
        logger.info(
            "Skip Elastic EP scale patch: vLLM %s is not in %s",
            version,
            TARGET_VLLM_VERSIONS,
        )
        return False
    logger.info("Applying Elastic EP scale patch for vLLM %s", version)
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
    """Apply a patch to a single vLLM source file; skip if already patched."""
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
    """Apply all Elastic EP scale patches; return 0 on success or skip, 1 on failure."""
    if not should_apply_patch():
        return 0

    script_dir = os.path.dirname(os.path.abspath(__file__))
    vllm_root = vllm.__path__[0]
    failed = 0
    for rel_path, patch_name, marker in PATCH_SPECS:
        patch_path = os.path.join(script_dir, patch_name)
        if not os.path.isfile(patch_path):
            logger.error("Patch file not found: %s", patch_path)
            failed += 1
            continue
        target = os.path.join(vllm_root, rel_path)
        if not os.path.isfile(target):
            logger.info("Skip Elastic EP scale patch: %s not found", target)
            continue
        if not apply_patch(target, patch_path, marker):
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
