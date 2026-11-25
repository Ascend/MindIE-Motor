# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Resolve the common log root and apply it to existing log mounts."""

from pathlib import PurePosixPath

DEFAULT_LOG_ROOT = "/root/ascend/log"


def common_log_root(env_config):
    value = env_config.get("motor_common_env", {}).get("MOTOR_LOG_ROOT_PATH") or DEFAULT_LOG_ROOT
    if not isinstance(value, str) or any(c in value for c in "\n\r\x00"):
        raise ValueError("MOTOR_LOG_ROOT_PATH must be an absolute directory")
    path = PurePosixPath(value)
    if not path.is_absolute() or path == PurePosixPath("/") or ".." in path.parts:
        raise ValueError("MOTOR_LOG_ROOT_PATH must be absolute and must not be / or contain ..")
    return str(path)


def apply_log_mount(pod_spec, log_root, host_root=None):
    """Update the existing log volume, preserving an explicitly configured host root."""
    volumes = pod_spec.get("volumes", [])
    log_volume = next((v for v in volumes if v.get("name") == "plog-path"), None)
    if not log_volume or "hostPath" not in log_volume:
        return
    for container in pod_spec.get("containers", []):
        mounts = container.get("volumeMounts", [])
        for mount in mounts:
            if mount.get("name") != "plog-path":
                continue
            if any(m is not mount and m.get("mountPath") == log_root for m in mounts):
                raise ValueError(f"Log mount destination {log_root} is already used")
            mount["mountPath"] = log_root
    log_volume["hostPath"]["path"] = host_root or log_root


def apply_document_log_mounts(value, log_root, user_config=None, role=None):
    """Update log mounts in workload and nested role templates."""
    if isinstance(value, list):
        for item in value:
            apply_document_log_mounts(item, log_root, user_config, role)
    elif isinstance(value, dict):
        if value.get("name") in ("controller", "coordinator", "encode", "prefill", "decode", "union"):
            role = value["name"]
        volumes = value.get("volumes", [])
        if isinstance(volumes, list) and "containers" in value:
            volume = next((v for v in volumes if v.get("name") == "plog-path"), {})
            existing = volume.get("hostPath", {}).get("path")
            previous_mount = next(
                (
                    m.get("mountPath")
                    for c in value.get("containers", [])
                    for m in c.get("volumeMounts", [])
                    if m.get("name") == "plog-path"
                ),
                None,
            )
            for container in value.get("containers", []):
                for item in container.get("env", []):
                    if item.get("name") == "ROLE" and item.get("value"):
                        role = item["value"]
            section = {"controller": "motor_controller_config", "coordinator": "motor_coordinator_config"}.get(
                role, "motor_nodemanger_config"
            )
            logging_config = (user_config or {}).get(section, {}).get("logging_config", {})
            explicit_host = logging_config.get("host_log_dir") if isinstance(logging_config, dict) else None
            preserved = existing if existing not in (DEFAULT_LOG_ROOT, previous_mount) else None
            apply_log_mount(value, log_root, explicit_host or preserved)
        for item in value.values():
            apply_document_log_mounts(item, log_root, user_config, role)
