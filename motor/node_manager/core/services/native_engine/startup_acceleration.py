# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from motor.common.logger import get_logger
from motor.common.resources.instance import PDRole
from motor.common.utils.patch_check import safe_open
from motor.config.node_manager import VLLMStartupAccelerationConfig

logger = get_logger(__name__)

STARTUP_PLAN_DIRECTORY = "startup_plan"
STARTUP_PLAN_PATTERN = "startup_plan_*.json"
MAX_STARTUP_PLAN_FILE_SIZE_BYTES = 1024 * 1024

VLLM_CACHE_ROOT_ENV = "VLLM_CACHE_ROOT"
VLLM_ENABLE_STARTUP_PLAN_ENV = "VLLM_ENABLE_STARTUP_PLAN"
VLLM_DISABLE_COMPILE_CACHE_ENV = "VLLM_DISABLE_COMPILE_CACHE"
VLLM_CUDAGRAPH_MODE_FULL = "FULL"
VLLM_CUDAGRAPH_MODE_FULL_DECODE_ONLY = "FULL_DECODE_ONLY"


def _normalize_cache_root(cache_root: str, source: str = "cache_root") -> Path:
    """Validate and normalize one effective vLLM cache root."""
    if not isinstance(cache_root, str) or not cache_root:
        raise ValueError("cache_root must be a non-empty string")
    root = Path(cache_root).expanduser()
    if not root.is_absolute():
        raise ValueError("%s must resolve to an absolute path: %s" % (source, cache_root))
    return root


def resolve_vllm_cache_root(
    config: VLLMStartupAccelerationConfig,
    base_environment: Mapping[str, str],
) -> str:
    """Resolve Motor config, inherited environment, then the vLLM-compatible default."""
    if not isinstance(config.cache_root, str):
        raise ValueError("cache_root must be a string")
    configured_root = config.cache_root.strip()
    inherited_root = base_environment.get(VLLM_CACHE_ROOT_ENV, "").strip()
    if configured_root:
        root = _normalize_cache_root(configured_root, "configured cache_root")
        source = "Motor config"
    elif inherited_root:
        root = _normalize_cache_root(inherited_root, VLLM_CACHE_ROOT_ENV)
        source = "inherited environment"
    else:
        xdg_cache_home = base_environment.get("XDG_CACHE_HOME", "").strip()
        if xdg_cache_home:
            root = _normalize_cache_root(str(Path(xdg_cache_home).expanduser() / "vllm"), "vLLM default")
        else:
            home = base_environment.get("HOME", "").strip()
            home_path = Path(home).expanduser() if home else Path.home()
            root = _normalize_cache_root(str(home_path / ".cache" / "vllm"), "vLLM default")
        source = "vLLM default"
        logger.warning(
            "vLLM cache_root is not configured; using %s. Configure an explicit persistent path for reuse",
            root,
        )

    logger.info("Resolved vLLM cache root from %s: %s", source, root)
    return str(root)


def build_vllm_startup_environment(
    config: VLLMStartupAccelerationConfig,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    """Translate Motor startup acceleration settings into vLLM environment variables."""
    environment = {
        VLLM_CACHE_ROOT_ENV: resolve_vllm_cache_root(config, base_environment),
        VLLM_ENABLE_STARTUP_PLAN_ENV: "1" if config.enable_startup_plan else "0",
    }
    if config.enable_graph_reuse:
        environment[VLLM_DISABLE_COMPILE_CACHE_ENV] = "0"
    for name, value in environment.items():
        if name in base_environment and base_environment[name] != value:
            logger.warning("Motor overrides environment %s=%r with %r", name, base_environment[name], value)
    return environment


def build_vllm_graph_reuse_engine_overrides(
    config: VLLMStartupAccelerationConfig,
    role: PDRole,
) -> dict[str, Any]:
    """Build vLLM/vLLM-Ascend engine overrides required for reusable full graphs."""
    ascend_overrides = {"enable_npugraph_ex": config.enable_graph_reuse}
    overrides: dict[str, Any] = {
        "additional_config": {"ascend_compilation_config": ascend_overrides},
    }
    if not config.enable_graph_reuse:
        # vLLM-Ascend requires static kernels to be disabled when npugraph_ex is disabled.
        ascend_overrides["enable_static_kernel"] = False
        return overrides

    cudagraph_mode = VLLM_CUDAGRAPH_MODE_FULL_DECODE_ONLY if role == PDRole.ROLE_D else VLLM_CUDAGRAPH_MODE_FULL
    overrides.update({"enforce_eager": False, "compilation_config": {"cudagraph_mode": cudagraph_mode}})
    return overrides


def inspect_graph_reuse_cache_root(cache_root: str) -> None:
    """Verify that vLLM can create or update graph reuse artifacts under the cache root."""
    root = _normalize_cache_root(cache_root)
    if root.exists():
        _validate_directory(root, "vLLM graph reuse cache root")
    else:
        _validate_creatable_root(root, "vLLM graph reuse cache parent")
    logger.info("vLLM graph reuse cache root preflight completed: %s", root)


def inspect_startup_plan_profiles(cache_root: str) -> tuple[int, int, int]:
    """Return candidate, readable and invalid counts; vLLM decides profile applicability."""
    root = _normalize_cache_root(cache_root)
    if not root.exists():
        _validate_creatable_root(root, "StartPlan cache parent")
        logger.info(
            "StartPlan cache root does not exist yet; full profiling is expected and vLLM will create it: %s",
            root,
        )
        return 0, 0, 0

    _validate_directory(root, "StartPlan cache root")
    plan_directory = root / STARTUP_PLAN_DIRECTORY
    if not plan_directory.exists():
        logger.info(
            "No StartPlan directory found; full profiling is expected and vLLM will create it: %s",
            plan_directory,
        )
        return 0, 0, 0

    _validate_directory(plan_directory, "StartPlan directory")
    candidates = sorted(plan_directory.glob(STARTUP_PLAN_PATTERN))
    if not candidates:
        logger.info("No StartPlan profile candidate found under %s; full profiling is expected", plan_directory)
        return 0, 0, 0

    readable_count = 0
    issue_count = 0
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_file():
            issue_count += 1
            logger.warning("Ignoring unsafe StartPlan profile candidate: %s", candidate)
            continue
        try:
            file_size = candidate.stat().st_size
            if not 0 < file_size <= MAX_STARTUP_PLAN_FILE_SIZE_BYTES:
                raise ValueError("file size is outside the accepted DFX range")
            with safe_open(str(candidate), "r") as profile_file:
                profile = json.load(profile_file)
            if not isinstance(profile, dict):
                raise ValueError("profile root must be a JSON object")
            readable_count += 1
        except (OSError, ValueError) as error:
            issue_count += 1
            logger.warning("StartPlan profile candidate is not readable: %s, reason: %s", candidate, error)

    logger.info(
        "StartPlan profile preflight completed: candidates=%d, readable=%d, issues=%d, directory=%s",
        len(candidates),
        readable_count,
        issue_count,
        plan_directory,
    )
    return len(candidates), readable_count, issue_count


def _validate_creatable_root(root: Path, label: str) -> None:
    parent = root.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    _validate_directory(parent, label)


def _validate_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError("%s must be a real directory: %s" % (label, path))
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise ValueError("%s is not accessible with the required permissions: %s" % (label, path))
