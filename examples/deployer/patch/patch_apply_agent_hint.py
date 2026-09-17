# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


"""Apply the agent-hint patches for vLLM 0.26.0 / vllm-ascend 0.26.0rc1.

Ports the agent-hint workflow from Dockerfile.a3-agent-hint:

1. apply ``vllm_v0.26.0_agent_hint.patch`` to the installed vLLM source, which
   adds the ``AgentHintManager`` hook and threads it through the scheduler and
   the KV cache managers;
2. apply ``vllm-ascend_v0.26.0rc1_agent_hint.patch`` to the installed
   vllm-ascend source, which adds the Ascend backend for those hooks;
3. register the ``ascend_agent_hint`` entry point in the vllm-ascend
   distribution metadata so vLLM's plugin loader activates the backend.

Both patches are git format-patch files, so the ``From``/``Subject``/diffstat
envelope is stripped before handing the ``diff --git`` payload to ``patch``. The
vllm-ascend patch also carries a ``setup.py`` hunk for the build-time entry-point
declaration; that hunk is dropped here and the entry point is registered directly
against the installed metadata instead, which works for editable and wheel installs.
"""

import ast
import importlib.metadata as md
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

try:
    import vllm
except ImportError:  # noqa: PERF203
    vllm = None

try:
    import vllm_ascend
except ImportError:  # noqa: PERF203
    vllm_ascend = None

# Both patches live in the versioned directory and target a single release.
VLLM_TARGET_VERSION = "0.26.0"
VLLM_ASCEND_TARGET_PREFIX = "0.26.0rc1"

ENTRY_POINT_SECTION = "[vllm.general_plugins]"
ENTRY_POINT_LINE = "ascend_agent_hint = vllm_ascend:register_agent_hint"


def _base_version(version: str) -> str:
    """Strip local (``+``) and pre-release (``-``) suffixes from a version."""
    return version.split("+")[0].split("-")[0]


def should_apply_patch() -> bool:
    """Return True only for the vLLM base release these patches target."""
    version = md.version("vllm")
    if _base_version(version) != VLLM_TARGET_VERSION:
        logger.info("Skip agent-hint patch: vLLM %s is not %s", version, VLLM_TARGET_VERSION)
        return False
    return True


def ascend_patch_supported() -> bool:
    """Return True only when vllm-ascend is installed at the targeted release.

    The vLLM side of this patch is inert without the Ascend backend (every hook
    falls back to the no-op ``AgentHintManager``) but it still rewrites vLLM
    source, so both sides are gated before either one is applied. Environments
    without the backend must stay untouched rather than carrying a half patch.
    """
    if vllm_ascend is None:
        logger.info("Skip agent-hint patch: vllm_ascend is not installed")
        return False
    try:
        ascend_version = md.version("vllm_ascend")
    except md.PackageNotFoundError:
        ascend_version = ""
    if not _base_version(ascend_version).startswith(VLLM_ASCEND_TARGET_PREFIX):
        logger.info("Skip agent-hint patch: vllm-ascend %s is not %s", ascend_version, VLLM_ASCEND_TARGET_PREFIX)
        return False
    return True


def is_patched(path: str, marker: str) -> bool:
    """Return True if the target file already carries the marker and stays valid Python."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if marker not in content:
            return False
        ast.parse(content)
        return True
    except (OSError, SyntaxError):
        return False


def _normalize_patch(text: str, strip_setup_py: bool) -> str:
    """Strip the format-patch envelope and, optionally, the ``setup.py`` hunk."""
    marker = "diff --git "
    idx = text.find(marker)
    if idx == -1:
        return text
    text = text[idx:]
    if not strip_setup_py:
        return text
    kept = []
    for hunk in re.split(r"(?m)(?=^diff --git )", text):
        if not hunk:
            continue
        if hunk.splitlines()[0].endswith(" b/setup.py"):
            continue
        kept.append(hunk)
    return "".join(kept)


def apply_patch(pkg_name: str, root: str, patch_path: str, marker_path: str, marker: str, strip_setup_py: bool) -> bool:
    """Apply one agent-hint patch under ``root``; idempotent via ``marker``."""
    patch_bin = shutil.which("patch")
    if not patch_bin:
        logger.error("patch command not found in PATH")
        return False
    if is_patched(marker_path, marker):
        logger.info("Already patched: %s", marker_path)
        return True
    try:
        raw = pathlib.Path(patch_path).read_text(encoding="utf-8")
    except OSError:
        logger.error("Patch file not found: %s", patch_path)
        return False
    normalized = _normalize_patch(raw, strip_setup_py)
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as tmp:
        tmp.write(normalized)
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            [patch_bin, "-p1", "--ignore-whitespace", "-d", root, "-i", tmp_path],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        os.unlink(tmp_path)
    if result.returncode == 0:
        logger.info("%s agent-hint patch applied", pkg_name)
        return True
    if is_patched(marker_path, marker):
        logger.info("Already patched: %s", marker_path)
        return True
    logger.error("Failed to apply %s agent-hint patch\n%s", pkg_name, result.stderr.strip())
    return False


def _find_entry_point_files() -> list[str]:
    """Locate vllm-ascend ``entry_points.txt`` in wheel and editable installs."""
    targets: set[str] = set()
    purelib = pathlib.Path(sysconfig.get_paths()["purelib"])
    for pattern in ("vllm_ascend*.dist-info/entry_points.txt", "vllm_ascend*.egg-info/entry_points.txt"):
        targets.update(str(p) for p in purelib.glob(pattern))
    if vllm_ascend is not None:
        ascend_root = pathlib.Path(os.path.dirname(vllm_ascend.__path__[0]))
        targets.update(str(p) for p in ascend_root.glob("*.egg-info/entry_points.txt"))
    return sorted(targets)


def _register_in_file(path: str, entry: str) -> bool:
    """Insert ``entry`` under ``[vllm.general_plugins]``; idempotent."""
    path_obj = pathlib.Path(path)
    try:
        lines = path_obj.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.error("Cannot read %s", path)
        return False
    if entry in lines:
        return True
    try:
        idx = lines.index(ENTRY_POINT_SECTION) + 1
    except ValueError:
        logger.error("Missing %s section in %s", ENTRY_POINT_SECTION, path)
        return False
    lines.insert(idx, entry)
    path_obj.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Registered %s in %s", entry, path)
    return True


def register_entry_point() -> bool:
    """Register the agent-hint backend in every vllm-ascend metadata file found."""
    targets = _find_entry_point_files()
    if not targets:
        logger.error("No vllm_ascend entry_points.txt found; agent-hint backend will not activate")
        return False
    return all(_register_in_file(p, ENTRY_POINT_LINE) for p in targets)


def main() -> int:
    """Apply the vllm and vllm-ascend patches and register the entry point."""
    if not should_apply_patch():
        return 0
    # Gate both sides before touching either one: the vLLM-side patch is only
    # meaningful together with the vllm-ascend backend that implements the hooks.
    if not ascend_patch_supported():
        return 0

    script_dir = os.path.dirname(os.path.abspath(__file__))
    patch_dir = os.path.join(script_dir, VLLM_TARGET_VERSION)

    if vllm is None:
        logger.info("Skip vllm agent-hint patch: vllm is not installed")
    else:
        vllm_root = os.path.dirname(vllm.__path__[0])
        vllm_marker = os.path.join(vllm.__path__[0], "v1", "core", "agent_hint_manager.py")
        if not apply_patch(
            "vllm",
            vllm_root,
            os.path.join(patch_dir, "vllm_agent_hint.patch"),
            vllm_marker,
            "create_agent_hint_manager",
            strip_setup_py=False,
        ):
            # vllm_ascend.register_agent_hint imports
            # vllm.v1.core.agent_hint_manager, so a patched vllm-ascend without
            # its vLLM side would break plugin loading at engine startup.
            logger.error("vllm agent-hint patch failed; skipping the vllm-ascend side")
            return 1

    ascend_root = os.path.dirname(vllm_ascend.__path__[0])
    ascend_marker = os.path.join(vllm_ascend.__path__[0], "core", "agent_hint", "backend.py")
    applied = apply_patch(
        "vllm_ascend",
        ascend_root,
        os.path.join(patch_dir, "vllm_ascend_agent_hint.patch"),
        ascend_marker,
        "ASCEND_AGENT_HINT_BACKEND",
        strip_setup_py=True,
    )
    if not applied or not register_entry_point():
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
