# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Compute native output paths for one process role.

Entrypoints supply model/service identity and COMPUTE_UNIT_LOG_SUBPATH for a
NodeManager and its local engines. This standalone helper also supports older
Motor containers; it emits KEY=VALUE assignments for the launcher.
"""

import argparse
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

DEFAULT_ROOT = "/root/ascend/log"
CONTROL_ROLES = ("controller", "coordinator")
ENGINE_ROLES = ("encode", "prefill", "decode", "union")


def _validate_directory(value: str, name: str) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "\n\r\x00"):
        raise ValueError(f"{name} must be a non-empty absolute directory")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ValueError(f"{name} must be absolute and must not be / or contain ..")
    return str(path)


def validate_root(value: str) -> str:
    return _validate_directory(value, "MOTOR_LOG_ROOT_PATH")


def _configured_path(environment: Mapping[str, str], name: str, default: Path) -> str:
    configured = environment.get(name)
    if not configured:
        return str(default)
    path = _validate_directory(configured, name)
    if path != str(default):
        print(
            f"Warning: preserving {name}={path}; verify its mount and isolation from other instances",
            file=sys.stderr,
        )
    return path


def _compute_subpath(environment: Mapping[str, str]) -> Path:
    value = environment.get("COMPUTE_UNIT_LOG_SUBPATH", "")
    if value == "":
        return Path()
    if not isinstance(value, str) or any(c in value for c in "\n\r\x00"):
        raise ValueError("COMPUTE_UNIT_LOG_SUBPATH must be a relative directory")
    path = Path(value)
    if path.anchor or path == Path(".") or ".." in path.parts:
        raise ValueError("COMPUTE_UNIT_LOG_SUBPATH must stay below the service root")
    return path


def relative_name(value: str, name: str) -> str:
    # A served model name may fall back to the absolute weights path.
    path = Path(value.strip("/"))
    if not str(path) or str(path) == "." or ".." in path.parts or any(c in value for c in "\n\r\x00"):
        raise ValueError(f"{name} must identify a directory below the service root")
    return str(path)


def resolve_paths(environment: Mapping[str, str], role: str) -> dict[str, str]:
    if role not in CONTROL_ROLES + ENGINE_ROLES:
        return {}

    # Aliases keep the existing Kubernetes and Docker service identities.
    model = environment.get("MODEL_NAME") or environment.get("model_name")
    service = environment.get("SERVICE_ID") or environment.get("service_id")
    if not model or not service:
        if role in ENGINE_ROLES:
            print("Warning: model or service identity is missing; preserving existing log paths", file=sys.stderr)
        return {}
    root = validate_root(environment.get("MOTOR_LOG_ROOT_PATH") or DEFAULT_ROOT)
    service_root = Path(root) / relative_name(model, "MODEL_NAME") / relative_name(service, "SERVICE_ID")
    paths = {"MOTOR_LOG_ROOT_PATH": root}
    if role in CONTROL_ROLES:
        paths["MOTOR_LOG_PATH"] = _configured_path(environment, "MOTOR_LOG_PATH", service_root / "motor-control" / role)
        return paths

    compute_root = service_root / _compute_subpath(environment)
    paths["MOTOR_LOG_PATH"] = _configured_path(environment, "MOTOR_LOG_PATH", compute_root / "motor")
    cann_root = compute_root / "cann"
    paths["ASCEND_WORK_PATH"] = _configured_path(environment, "ASCEND_WORK_PATH", cann_root / "work")
    paths["ASCEND_CACHE_PATH"] = _configured_path(environment, "ASCEND_CACHE_PATH", cann_root / "cache")
    paths["TORCHINDUCTOR_CACHE_DIR"] = _configured_path(
        environment, "TORCHINDUCTOR_CACHE_DIR", cann_root / "torchinductor"
    )
    engine_type = environment.get("engine_type", environment.get("ENGINE_TYPE", "vllm"))
    engine_root = compute_root / ("sglang" if engine_type == "sglang" else "vllm")
    paths["SERVICE_ENGINE_LOG_PATH"] = str(engine_root)
    paths["TRITON_HOME"] = _configured_path(environment, "TRITON_HOME", compute_root / "triton")
    for name, leaf in (
        ("TRITON_CACHE_DIR", "cache"),
        ("TRITON_DUMP_DIR", "dump"),
        ("TRITON_OVERRIDE_DIR", "override"),
    ):
        configured = environment.get(name)
        expected = str(Path(paths["TRITON_HOME"]) / ".triton" / leaf)
        if configured and str(Path(configured).expanduser()) != expected:
            print(
                f"Warning: {name}={configured} overrides TRITON_HOME; preserving the explicit Triton path",
                file=sys.stderr,
            )

    if engine_type == "vllm":
        paths["XDG_CACHE_HOME"] = _configured_path(environment, "XDG_CACHE_HOME", engine_root / "xdg" / "cache")
        paths["XDG_CONFIG_HOME"] = _configured_path(environment, "XDG_CONFIG_HOME", engine_root / "xdg" / "config")
        for explicit, xdg in (
            ("VLLM_CACHE_ROOT", "XDG_CACHE_HOME"),
            ("VLLM_CONFIG_ROOT", "XDG_CONFIG_HOME"),
        ):
            configured = environment.get(explicit)
            expected = str(Path(paths[xdg]) / "vllm")
            if configured and str(Path(configured).expanduser()) != expected:
                print(
                    f"Warning: {explicit}={configured} overrides {xdg}; preserving the explicit vLLM path",
                    file=sys.stderr,
                )
    return paths


def prepare_paths(paths: Mapping[str, str], environment: Mapping[str, str]) -> None:
    directory_names = (
        "MOTOR_LOG_PATH",
        "SERVICE_ENGINE_LOG_PATH",
        "ASCEND_WORK_PATH",
        "ASCEND_CACHE_PATH",
        "TORCHINDUCTOR_CACHE_DIR",
        "TRITON_HOME",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    )
    for name in directory_names:
        path = paths.get(name)
        if not path:
            continue
        try:
            Path(path).mkdir(mode=0o750, parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Warning: cannot prepare native output directory {path}: {exc}", file=sys.stderr)
    # XDG/Triton also contain engine inputs; copy existing configuration without
    # overwriting configuration already present at the target.
    copies = []
    home = Path(environment.get("HOME") or Path.home())
    if paths.get("XDG_CONFIG_HOME"):
        original = Path(environment.get("XDG_CONFIG_HOME") or str(home / ".config")).expanduser()
        copies.append((original / "vllm", Path(paths["XDG_CONFIG_HOME"]) / "vllm"))
    if paths.get("TRITON_HOME"):
        original = Path(environment.get("TRITON_HOME") or str(home)).expanduser()
        copies.append(
            (
                original / ".triton/override",
                Path(paths["TRITON_HOME"]) / ".triton/override",
            )
        )
    for source, target in copies:
        try:
            if source.resolve() != target.resolve() and source.is_dir() and not target.exists():
                target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
                shutil.copytree(source, target, symlinks=True)
        except OSError as exc:
            print(f"Warning: cannot seed native configuration from {source} to {target}: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--role", required=True, help="process role; roles outside the native-log list are left unchanged"
    )
    args = parser.parse_args()
    try:
        paths = resolve_paths(os.environ, args.role)
    except ValueError as exc:
        print(f"Native log path setup failed: {exc}", file=sys.stderr)
        return 1
    prepare_paths(paths, os.environ)
    for name, value in paths.items():
        print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
