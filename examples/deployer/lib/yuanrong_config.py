# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Validation helpers for the NodeManager-managed Yuanrong Worker launcher."""

_LAUNCHER_KEYS = frozenset({"start_args", "worker_args"})
_RESERVED_ARGS = frozenset({"w", "worker_address", "etcd_address", "shared_memory_size_mb"})
_SCALAR_TYPES = (str, int, float, bool)


def validate_yuanrong_config(kv_config: dict) -> None:
    """Validate and normalize the deployer-owned Yuanrong configuration."""
    if "worker_port" not in kv_config:
        raise ValueError("kv_cache_store_config.worker_port is required for the yuanrong backend")
    kv_config["worker_port"] = _parse_positive_int(kv_config["worker_port"], "worker_port", maximum=65535)

    if "shared_memory_size_mb" not in kv_config:
        raise ValueError("kv_cache_store_config.shared_memory_size_mb is required for the yuanrong backend")
    kv_config["shared_memory_size_mb"] = _parse_positive_int(
        kv_config["shared_memory_size_mb"],
        "shared_memory_size_mb",
    )

    if "worker_args" in kv_config:
        raise ValueError("kv_cache_store_config.worker_args is unsupported; use worker_launcher.worker_args")

    launcher = kv_config.get("worker_launcher", {})
    if not isinstance(launcher, dict):
        raise ValueError("kv_cache_store_config.worker_launcher must be an object")  # noqa: TRY004
    unknown_keys = set(launcher) - _LAUNCHER_KEYS
    if unknown_keys:
        raise ValueError(f"kv_cache_store_config.worker_launcher contains unsupported fields: {sorted(unknown_keys)}")

    for section in _LAUNCHER_KEYS:
        _validate_arg_section(launcher.get(section, []), f"kv_cache_store_config.worker_launcher.{section}")


def _parse_positive_int(value: object, field_name: str, maximum: int | None = None) -> int:
    range_description = f"between 1 and {maximum}" if maximum is not None else "a positive integer"
    message = f"kv_cache_store_config.{field_name} must be {range_description}"
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(message)  # noqa: TRY004
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(message) from exc
    if parsed <= 0 or (maximum is not None and parsed > maximum):
        raise ValueError(message)  # noqa: TRY004
    return parsed


def _validate_arg_section(value: object, field_name: str) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")  # noqa: TRY004
    for index, item in enumerate(value):
        if not isinstance(item, dict) or len(item) != 1:
            raise ValueError(f"{field_name}[{index}] must be an object containing exactly one argument")
        arg_name, arg_value = next(iter(item.items()))
        if (
            not isinstance(arg_name, str)
            or not arg_name
            or arg_name.startswith("-")
            or any(char.isspace() for char in arg_name)
        ):
            raise ValueError(f"{field_name}[{index}] has invalid argument name {arg_name!r}")
        if arg_name in _RESERVED_ARGS:
            raise ValueError(f"{field_name}[{index}] contains Motor-managed argument {arg_name!r}")
        if arg_value is not None and not isinstance(arg_value, _SCALAR_TYPES):
            raise ValueError(f"{field_name}[{index}] value must be a scalar or null")
