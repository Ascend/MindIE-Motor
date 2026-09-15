# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Shared native-library bootstrap and model-directory helpers for the obfuscation paths."""

import importlib.util
import os
from pathlib import Path


class ObfuscationLibraryError(RuntimeError):
    """The ai-asset-obfuscate SDK cannot be loaded."""


def resolve_obfuscation_model_paths(
    explicit_model_path: str,
    engine_model_paths: list[str] | None,
) -> list[str]:
    """Pick the candidate model directories used to read model-side obfuscation parameters.

    An explicitly configured path always wins; otherwise the directories declared by the engine
    sections are tried in order (callers fail closed when none of them is usable).
    """
    if isinstance(explicit_model_path, str) and explicit_model_path.strip():
        return [explicit_model_path]
    return [path for path in (engine_model_paths or []) if isinstance(path, str) and path.strip()]


def is_obfuscation_enabled(*services: object) -> bool:
    """Report whether at least one obfuscation path (token or image) is active.

    Token and image obfuscation are configured independently, but both need Render and both must stay
    fail closed, so every guard uses this single predicate instead of inspecting one service.
    """
    return any(service is not None for service in services)


def configure_obfuscation_library_path() -> None:
    """Expose SDK shared libraries before Coordinator spawns inference workers."""
    spec = importlib.util.find_spec("ai_asset_obfuscate")
    if spec is None or spec.origin is None:
        raise ObfuscationLibraryError("ai-asset-obfuscate is unavailable")
    library_path = str(Path(spec.origin).resolve().parent / "libs")
    current_paths = [item for item in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if item]
    if library_path not in current_paths:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([library_path, *current_paths])
