# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Render per-instance Motor configuration from the unmodified ConfigMap.

This deployment helper uses only the standard library. Always pass the original
ConfigMap JSON as --source; already adapted output is not a new baseline.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROLE_SECTIONS = {
    "prefill": "motor_engine_prefill_config",
    "decode": "motor_engine_decode_config",
}


def decimal(value, name, minimum=0, maximum=None):
    """Accept integers or decimal strings, never booleans/floats/shell text."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"{name} must be a decimal integer")
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"{name} must be a decimal integer")
    result = int(value)
    if result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{name} is outside the permitted range")
    return result


def validate_role_index(role, index):
    if role not in ROLE_SECTIONS:
        raise ValueError("role must be prefill or decode")
    return decimal(index, "index")


def stride(environ, name, default):
    return decimal(environ.get(name, str(default)), name, minimum=1, maximum=65535)


def shifted_port(value, offset, name):
    original = decimal(value, name, minimum=1, maximum=65535)
    result = original + offset
    if result > 65535:
        raise ValueError(f"{name} + instance offset exceeds 65535")
    return str(result) if isinstance(value, str) else result


def required_dict(parent, key):
    value = parent.get(key)
    if not isinstance(value, dict):
        raise TypeError(f"required configuration object is missing: {key}")
    return value


def offset_required_port(parent, key, offset):
    if key not in parent:
        raise ValueError(f"required explicit port is missing: {key}")
    parent[key] = shifted_port(parent[key], offset, key)


def offset_kv_ports(node, offset):
    """Visit only the selected engine's KV-transfer configuration subtree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "kv_port":
                node[key] = shifted_port(value, offset, "kv_port")
            else:
                offset_kv_ports(value, offset)
    elif isinstance(node, list):
        for item in node:
            offset_kv_ports(item, offset)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def atomic_update(path, payload):
    """Replace a whole file atomically, preserving mtime when bytes match."""
    if path.is_file() and path.read_bytes() == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        temporary = None
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def adapt_config(source, dest, role, index, environ=None):
    """Validate everything before writing destination JSON and its marker."""
    environ = os.environ if environ is None else environ
    index = validate_role_index(role, index)
    source, dest = Path(source), Path(dest)
    marker_path = dest.parent / "instance-ports.json"
    if source.resolve() in (dest.resolve(), marker_path.resolve()):
        raise ValueError("source must differ from destination and marker")
    for output in (dest, marker_path):
        if output.exists() and os.path.samefile(source, output):
            raise ValueError("source must not alias destination or marker")

    with source.open("r", encoding="utf-8") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise TypeError("source configuration must be a JSON object")
    section = required_dict(config, ROLE_SECTIONS[role])
    node_manager = required_dict(section, "motor_nodemanger_config")
    if required_dict(node_manager, "port_allocator_config").get("enable", True) is not False:
        raise ValueError("per-instance explicit ports require port_allocator_config.enable=false")
    api = required_dict(node_manager, "api_config")
    endpoints = required_dict(node_manager, "endpoint_config")
    engine = required_dict(section, "engine_config")
    offset = index * stride(environ, "MOTOR_INSTANCE_PORT_STRIDE", 100)
    kv_offset = index * stride(environ, "MOTOR_INSTANCE_KV_PORT_STRIDE", 100)
    offset_required_port(api, "node_manager_port", offset)
    offset_required_port(endpoints, "base_port", offset)
    offset_required_port(engine, "data_parallel_rpc_port", offset)
    offset_kv_ports(engine.get("kv_transfer_config"), kv_offset)

    marker = {
        "role": role,
        "index": index,
        "effective_config": str(dest.resolve()),
        "ports": {
            "node_manager_port": api["node_manager_port"],
            "base_port": endpoints["base_port"],
            "data_parallel_rpc_port": engine["data_parallel_rpc_port"],
        },
    }
    config_payload, marker_payload = json_bytes(config), json_bytes(marker)
    atomic_update(dest, config_payload)
    atomic_update(marker_path, marker_payload)
    return marker


def adapt_env(role, index, environ=None):
    environ = os.environ if environ is None else environ
    index = validate_role_index(role, index)
    settings = (
        ("ASCEND_BASE_PORT", "MOTOR_INSTANCE_ASCEND_PORT_STRIDE", 4000),
        ("HIXL_LISTEN_PORT", "MOTOR_INSTANCE_STORE_PORT_STRIDE", 200),
    )
    result = {}
    for variable, stride_variable, default in settings:
        if variable not in environ:
            raise ValueError(f"required environment variable is missing: {variable}")
        offset = index * stride(environ, stride_variable, default)
        result[variable] = str(shifted_port(environ[variable], offset, variable))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    for name in ("config", "env"):
        mode = modes.add_parser(name)
        mode.add_argument("--role", required=True, choices=tuple(ROLE_SECTIONS))
        mode.add_argument("--index", required=True)
        if name == "config":
            mode.add_argument("--source", required=True)
            mode.add_argument("--dest", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.mode == "config":
            adapt_config(arguments.source, arguments.dest, arguments.role, arguments.index)
        else:
            values = adapt_env(arguments.role, arguments.index)
            # All values are validated before printing; output is safe for a
            # shell read/whitelist/export loop without eval or shell execution.
            for name, value in values.items():
                print(f"{name}={value}")
    except (ValueError, TypeError, OSError) as error:
        print(f"adapt_instance: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
