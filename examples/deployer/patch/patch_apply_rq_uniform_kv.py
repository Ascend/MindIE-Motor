# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


"""Apply the rq_uniform KV-cache patch for vllm-ascend 0.26.0rc1.

Applies ``<VLLM_TARGET_VERSION>/vllm_ascend_rq_uniform_kv.patch`` to the installed
vllm-ascend source tree (core Python only; no ``tools/``). The patch is a
git format-patch, so the ``From``/``Subject``/diffstat envelope is stripped
before handing the ``diff --git`` payload to ``patch -p1 --forward``.

Idempotency is checked via stable markers that land with the patch (new
modules plus hang-points in shared files). Upstream dtype string
``turboquant_4bit_nc`` is intentionally retained for SFA 386-byte slots.
"""

import ast
import importlib.metadata as md
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

try:
    import vllm_ascend
except ImportError:  # noqa: PERF203
    vllm_ascend = None

VLLM_TARGET_VERSION = "0.26.0"
VLLM_ASCEND_TARGET_PREFIX = "0.26.0rc1"

# Files that must carry their hook once the patch is applied. A single new
# module is not enough: ``patch`` can stop mid-file and leave a half tree.
MARKERS = (
    ("vllm_ascend/patch/platform/patch_rq_uniform_cache_dtype.py", "is_ascend_rq_uniform_kv_dtype"),
    ("vllm_ascend/quantization/methods/kv_cache/rq_uniform/latent.py", "RqUniformLatent"),
    ("vllm_ascend/ascend_config.py", "enable_sparse_sfa_rq_uniform"),
    ("vllm_ascend/hybrid_kv_page.py", "class HybridK8v4Page"),
)

PATCH_RELPATH = os.path.join(VLLM_TARGET_VERSION, "vllm_ascend_rq_uniform_kv.patch")


def _base_version(version: str) -> str:
    """Strip local (``+``) and pre-release (``-``) suffixes from a version."""
    return version.split("+")[0].split("-")[0]


def should_apply_patch() -> bool:
    """Return True only for the vLLM base release this patch targets."""
    try:
        version = md.version("vllm")
    except md.PackageNotFoundError:
        logger.info("Skip rq_uniform KV patch: vLLM is not installed")
        return False
    if _base_version(version) != VLLM_TARGET_VERSION:
        logger.info("Skip rq_uniform KV patch: vLLM %s is not %s", version, VLLM_TARGET_VERSION)
        return False
    return True


def ascend_patch_supported() -> bool:
    """Return True only when vllm-ascend is installed at the targeted release."""
    if vllm_ascend is None:
        logger.info("Skip rq_uniform KV patch: vllm_ascend is not installed")
        return False
    try:
        ascend_version = md.version("vllm_ascend")
    except md.PackageNotFoundError:
        ascend_version = ""
    if not _base_version(ascend_version).startswith(VLLM_ASCEND_TARGET_PREFIX):
        logger.info(
            "Skip rq_uniform KV patch: vllm-ascend %s is not %s*",
            ascend_version,
            VLLM_ASCEND_TARGET_PREFIX,
        )
        return False
    return True


def missing_markers(root: str, markers: tuple[tuple[str, str], ...] = MARKERS) -> list[str]:
    """Return marker files that are absent, lack the marker, or carry invalid Python."""
    missing = []
    for rel_path, marker in markers:
        try:
            with open(os.path.join(root, rel_path), encoding="utf-8") as f:
                content = f.read()
        except OSError:
            missing.append(rel_path)
            continue
        if marker not in content:
            missing.append(rel_path)
            continue
        try:
            ast.parse(content)
        except SyntaxError:
            missing.append(rel_path)
    return missing


def is_patched(root: str, markers: tuple[tuple[str, str], ...] = MARKERS) -> bool:
    """Return True only when every marker is present and each file stays valid Python."""
    return not missing_markers(root, markers)


def _normalize_patch(text: str) -> str:
    """Strip the format-patch envelope; keep every ``diff --git`` hunk."""
    marker = "diff --git "
    idx = text.find(marker)
    if idx == -1:
        return text
    return text[idx:]


def apply_patch(root: str, patch_path: str) -> bool:
    """Apply the rq_uniform patch under ``root``; idempotent via ``MARKERS``."""
    patch_bin = shutil.which("patch")
    if not patch_bin:
        logger.error("patch command not found in PATH")
        return False
    if is_patched(root):
        logger.info("Already patched: %s", os.path.join(root, MARKERS[0][0]))
        return True
    try:
        raw = pathlib.Path(patch_path).read_text(encoding="utf-8")
    except OSError:
        logger.error("Patch file not found: %s", patch_path)
        return False
    normalized = _normalize_patch(raw)
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as tmp:
        tmp.write(normalized)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            [patch_bin, "-p1", "--forward", "--ignore-whitespace", "-d", root, "-i", tmp_path],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        os.unlink(tmp_path)
    if is_patched(root):
        logger.info("vllm_ascend rq_uniform KV patch applied")
        return True
    logger.error(
        "Failed to apply rq_uniform KV patch; still unpatched: %s\n%s",
        ", ".join(missing_markers(root)),
        result.stderr.strip(),
    )
    return False


def main() -> int:
    """Gate versions, then apply the ascend-side rq_uniform patch."""
    if not should_apply_patch():
        return 0
    if not ascend_patch_supported():
        return 0

    script_dir = os.path.dirname(os.path.abspath(__file__))
    patch_path = os.path.join(script_dir, PATCH_RELPATH)
    ascend_root = os.path.dirname(vllm_ascend.__path__[0])
    if not apply_patch(ascend_root, patch_path):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
