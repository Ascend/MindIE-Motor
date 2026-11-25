# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Prepare log CLI paths using installed distribution versions.

Version rules cannot detect source patches that keep the same version.
Path configuration does not create files or change runtime feature switches.
"""

import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from importlib import metadata
from pathlib import PurePosixPath
from typing import Any

from packaging.version import InvalidVersion, Version

from motor.common.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PackageRequirement:
    distribution: str
    minimum_version: Version


@dataclass(frozen=True)
class VllmLogCliCapabilities:
    """False omits options, including explicit input, for unconfirmed versions."""

    runtime_paths: bool = False
    watchdog: bool = False


_RUNTIME_REQUIREMENT = PackageRequirement("vllm-ascend", Version("0.33.0"))
_WATCHDOG_REQUIREMENT = PackageRequirement("vllm", Version("0.33.0"))

RUNTIME_PATHS = {
    "runtime_config_path": "runtime/config/runtime_config.json",
    "runtime_report_dir": "runtime/report",
    "runtime_dump_dir": "runtime/kv_cache",
}


def _read_version(distribution: str) -> str | None:
    try:
        return metadata.version(distribution)
    except (metadata.PackageNotFoundError, OSError, UnicodeError):
        return None


def read_engine_versions() -> dict[str, str | None]:
    distributions = dict.fromkeys(
        requirement.distribution for requirement in (_WATCHDOG_REQUIREMENT, _RUNTIME_REQUIREMENT)
    )
    return {distribution: _read_version(distribution) for distribution in distributions}


def _supports_requirement(versions: Mapping[str, str | None], requirement: PackageRequirement) -> bool:
    raw_version = versions.get(requirement.distribution)
    if raw_version is None:
        return False
    try:
        version = Version(raw_version)
    except InvalidVersion:
        return False
    return version >= requirement.minimum_version


def resolve_log_cli_capabilities(versions: Mapping[str, str | None]) -> VllmLogCliCapabilities:
    """Apply independent Ascend and vLLM rules to one version snapshot."""
    return VllmLogCliCapabilities(
        runtime_paths=_supports_requirement(versions, _RUNTIME_REQUIREMENT),
        watchdog=_supports_requirement(versions, _WATCHDOG_REQUIREMENT),
    )


def _warn_omitted(config_path: str) -> None:
    logger.warning(
        "Omitting native log option %s; support is not confirmed by the installed-version rules", config_path
    )


def _set_or_validate_path(target: dict[str, Any], key: str, expected: str, config_path: str) -> None:
    configured = target.get(key)
    if configured is None:
        target[key] = expected
        return
    if (
        not isinstance(configured, str)
        or not PurePosixPath(configured).is_absolute()
        or ".." in PurePosixPath(configured).parts
    ):
        raise ValueError(f"{config_path}={configured!r} must be an absolute path without '..'")
    if str(PurePosixPath(configured)) != expected:
        logger.warning(
            "Preserving explicit native log path %s=%s instead of the unified default %s",
            config_path,
            configured,
            expected,
        )


def _decode_native_object(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _ensure_config_object(target: dict[str, Any], key: str, config_path: str) -> dict[str, Any]:
    value = _decode_native_object(target.get(key))
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{config_path} must be an object for native log path configuration")  # noqa: TRY004
    target[key] = value
    return value


def _merge_additional_objects(target: dict[str, Any], incoming: dict[str, Any], path: str) -> None:
    for key, value in incoming.items():
        field_path = path + "." + key
        if key not in target:
            target[key] = value
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _merge_additional_objects(target[key], value, field_path)
        elif type(target[key]) is not type(value) or target[key] != value:
            raise ValueError(f"Conflicting values for {field_path} in additional_config and dotted fields")


def _fold_additional_fields(configured: dict[str, Any], additional: dict[str, Any]) -> None:
    """Fold ordinary dotted object fields; list indexing/append syntax is not supported."""
    for key in list(configured):
        if not key.startswith("additional_config."):
            continue
        parts = key.removeprefix("additional_config.").split(".")
        if any(
            not part
            or part.lstrip("-").isdecimal()
            or any(character in "[]+" or character.isspace() for character in part)
            for part in parts
        ):
            raise ValueError(f"{key} must use object field names; empty fields and list indexing are unsupported")
        nested = configured.pop(key)
        for part in reversed(parts):
            nested = {part: nested}
        _merge_additional_objects(additional, nested, "engine_config.additional_config")


def _prepare_additional_config(configured: dict[str, Any], supports_runtime_paths: bool) -> dict[str, Any]:
    additional = _ensure_config_object(configured, "additional_config", "engine_config.additional_config")
    if not supports_runtime_paths:
        for key in RUNTIME_PATHS:
            dotted_key = "additional_config." + key
            _warn_omitted(dotted_key)
            additional.pop(key, None)
            configured.pop(dotted_key, None)

    _fold_additional_fields(configured, additional)
    return additional


def _apply_additional_paths(
    additional: dict[str, Any], engine_root: PurePosixPath, supports_runtime_paths: bool
) -> None:
    # The framework owns Ascend log coverage; Motor only supplies its directory.
    _set_or_validate_path(
        additional,
        "ascend_log_path",
        str(engine_root / "vllm_ascend"),
        "engine_config.additional_config.ascend_log_path",
    )
    if supports_runtime_paths:
        for key, relative in RUNTIME_PATHS.items():
            _set_or_validate_path(
                additional, key, str(engine_root / relative), "engine_config.additional_config." + key
            )


def _apply_watchdog_paths(configured: dict[str, Any], engine_root: PurePosixPath, supports_watchdog: bool) -> None:
    watchdog_keys = [key for key in configured if key == "watchdog_config" or key.startswith("watchdog_config.")]
    if not watchdog_keys:
        return
    if not supports_watchdog:
        _warn_omitted("watchdog_config")
        for key in watchdog_keys:
            configured.pop(key)
        return
    nested = _decode_native_object(configured.get("watchdog_config"))
    dotted_key = "watchdog_config.dump_dir"
    if isinstance(nested, dict) and "dump_dir" in nested and dotted_key in configured:
        if type(nested["dump_dir"]) is not type(configured[dotted_key]) or nested["dump_dir"] != configured[dotted_key]:
            raise ValueError(
                "Conflicting values for engine_config.watchdog_config.dump_dir in object and dotted fields"
            )
    # The native empty/default dump_dir disables Watchdog. Merely configuring
    # timeout/check_interval must not turn it on by injecting an output path.
    expected = str(engine_root / "watchdog")
    if configured.get(dotted_key) not in (None, ""):
        _set_or_validate_path(configured, dotted_key, expected, "engine_config." + dotted_key)
    if isinstance(nested, dict) and nested.get("dump_dir") not in (None, ""):
        _set_or_validate_path(nested, "dump_dir", expected, "engine_config.watchdog_config.dump_dir")
        configured["watchdog_config"] = nested


def apply_log_cli_paths(
    engine_config: dict[str, Any], engine_root: PurePosixPath, capabilities: VllmLogCliCapabilities
) -> None:
    """Leave engine_config unchanged if any managed setting is rejected."""
    configured = deepcopy(engine_config)
    additional = _prepare_additional_config(configured, capabilities.runtime_paths)
    _apply_additional_paths(additional, engine_root, capabilities.runtime_paths)
    _apply_watchdog_paths(configured, engine_root, capabilities.watchdog)
    engine_config.clear()
    engine_config.update(configured)
