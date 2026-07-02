# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
import copy
from collections.abc import Mapping, Sequence

from lib.utils import logger

UPDATE_CONFIG_WHITELIST = {
    "motor_deploy_config": {
        "tls_config": {
            "north_tls_config": [
                "enable_tls",
                "ca_file",
                "cert_file",
                "key_file",
                "passwd_file",
                "crl_file",
            ],
        },
    },
    "north_config": ["name", "ip", "port"],
    "motor_controller_config": {
        "logging_config": [
            "log_level",
        ],
        "observability_config": [
            "observability_enable",
            "metrics_ttl",
        ],
    },
    "motor_coordinator_config": {
        "logging_config": [
            "log_level",
        ],
        "exception_config": [
            "max_retry",
            "retry_delay",
            "first_token_timeout",
            "infer_timeout",
        ],
        "timeout_config": [
            "request_timeout",
            "connection_timeout",
            "read_timeout",
            "write_timeout",
            "keep_alive_timeout",
        ],
    },
    "motor_nodemanger_config": {
        "logging_config": [
            "log_level",
        ],
    },
}


def _normalize_path_token(path_token):
    return path_token.split("[", 1)[0]


def _is_non_string_sequence(value):
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _path_is_whitelisted(path):
    whitelist_node = UPDATE_CONFIG_WHITELIST
    path_tokens = path.split(".")
    for index, path_token in enumerate(path_tokens):
        normalized_token = _normalize_path_token(path_token)
        if isinstance(whitelist_node, Mapping):
            if normalized_token not in whitelist_node:
                return False
            whitelist_node = whitelist_node[normalized_token]
            continue

        return index == len(path_tokens) - 1 and normalized_token in whitelist_node

    return False


def _collect_changed_paths(current_value, baseline_value, path=""):
    if isinstance(current_value, Mapping) and isinstance(baseline_value, Mapping):
        changed_paths = []
        all_keys = sorted(set(current_value) | set(baseline_value))
        for key in all_keys:
            next_path = f"{path}.{key}" if path else str(key)
            changed_paths.extend(
                _collect_changed_paths(
                    current_value.get(key),
                    baseline_value.get(key),
                    next_path,
                )
            )
        return changed_paths

    if baseline_value is None and isinstance(current_value, Mapping):
        changed_paths = []
        for key in sorted(current_value):
            next_path = f"{path}.{key}" if path else str(key)
            changed_paths.extend(_collect_changed_paths(current_value[key], None, next_path))
        return changed_paths

    if current_value is None and isinstance(baseline_value, Mapping):
        changed_paths = []
        for key in sorted(baseline_value):
            next_path = f"{path}.{key}" if path else str(key)
            changed_paths.extend(_collect_changed_paths(None, baseline_value[key], next_path))
        return changed_paths

    if baseline_value is None and _is_non_string_sequence(current_value):
        changed_paths = []
        for index, item in enumerate(current_value):
            next_path = f"{path}[{index}]"
            changed_paths.extend(_collect_changed_paths(item, None, next_path))
        return changed_paths

    if current_value is None and _is_non_string_sequence(baseline_value):
        changed_paths = []
        for index, item in enumerate(baseline_value):
            next_path = f"{path}[{index}]"
            changed_paths.extend(_collect_changed_paths(None, item, next_path))
        return changed_paths

    if _is_non_string_sequence(current_value) and _is_non_string_sequence(baseline_value):
        if len(current_value) != len(baseline_value):
            return [path]
        changed_paths = []
        for index, (current_item, baseline_item) in enumerate(zip(current_value, baseline_value)):
            next_path = f"{path}[{index}]"
            changed_paths.extend(_collect_changed_paths(current_item, baseline_item, next_path))
        return changed_paths

    if current_value != baseline_value:
        return [path]
    return []


def _parse_path_token(token):
    """Parse a path token into (key, index_or_None).

    Handles both plain keys (e.g. 'enable_tls') and indexed keys (e.g. 'north_tls_config[0]').

    Raises ValueError if the token has malformed bracket syntax or a non-integer index.
    """
    if "[" in token:
        if not token.endswith("]"):
            raise ValueError(f"Malformed path token '{token}': missing closing bracket ']'")
        key, bracket_part = token.split("[", 1)
        index_str = bracket_part.rstrip("]")
        if not index_str.isdigit():
            raise ValueError(f"Malformed path token '{token}': '{index_str}' is not a valid integer index")
        return key, int(index_str)
    return token, None


def _navigate(obj, token):
    """Navigate one level deeper into obj using a parsed path token.

    Returns the value at the given token, or None if the key/index doesn't exist.
    """
    try:
        key, index = _parse_path_token(token)
        if index is None:
            return obj[key]
        return obj[key][index]
    except (KeyError, IndexError, TypeError):
        return None


def _get_value_at_path(obj, path):
    """Get the value at a dot-separated path (with optional list indices) from a nested dict/list.

    Returns None if any segment of the path doesn't exist in obj.
    """
    current = obj
    for token in path.split("."):
        current = _navigate(current, token)
        if current is None:
            return None
    return current


def _set_value_at_path(obj, path, value):
    """Set the value at a dot-separated path (with optional list indices) in a nested dict/list."""
    current = obj
    tokens = path.split(".")
    for i, token in enumerate(tokens):
        key, index = _parse_path_token(token)
        if i == len(tokens) - 1:
            if index is None:
                current[key] = value
            else:
                current[key][index] = value
        else:
            current = _navigate(current, token)


def apply_whitelist_update(user_config, baseline_config):
    """Apply only whitelisted config changes from user_config onto baseline_config.

    Non-whitelisted changes are logged as warnings and silently ignored.
    Returns a new config dict with only whitelisted changes applied.
    """
    changed_paths = _collect_changed_paths(user_config, baseline_config)
    whitelisted_paths = []
    non_whitelisted_paths = []

    for path in changed_paths:
        if _path_is_whitelisted(path):
            whitelisted_paths.append(path)
        else:
            non_whitelisted_paths.append(path)

    if non_whitelisted_paths:
        logger.warning(
            "The following config items are not in the update whitelist and have been ignored: %s",
            ", ".join(sorted(non_whitelisted_paths)),
        )

    # Start from baseline and apply only whitelisted changes
    result = copy.deepcopy(baseline_config)
    for path in whitelisted_paths:
        value = _get_value_at_path(user_config, path)
        if value is None:
            logger.warning("Skipping whitelisted path '%s': not found in user_config", path)
            continue
    for path in whitelisted_paths:
        try:
            value = _get_value_at_path(user_config, path)
        except (KeyError, IndexError, TypeError):
            logger.warning("Skipping whitelisted path '%s': not found in user_config", path)
            continue
        _set_value_at_path(result, path, value)

    return result
