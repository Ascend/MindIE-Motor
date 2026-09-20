# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
from copy import deepcopy

import lib.constant as C


_SNAPSHOT_ENGINE_ROLES = (C.ROLE_PREFILL, C.ROLE_DECODE, C.ROLE_UNION)
_REQUIRED_PATH_KEYS = (C.HOST_SNAPSHOT_IMAGE_PATH, C.SNAPSHOT_MNT_PATH, C.DEVICE_SNAPSHOT_WEIGHT_PATH)
_SNAPSHOT_LABEL = "infer.huawei.com/container-snapshot"
_SNAPSHOT_FAULT_SCHEDULING = "external-force"
_SNAPSHOT_EXCLUDED_VOLUME_NAMES = frozenset(("data", "dshm", "coredump", "plog-path", "cache-path"))
_SNAPSHOT_READINESS_PROBE = {
    "exec": {"command": ["bash", "-c", "$CONFIGMAP_PATH/probe.sh readiness"]},
    "periodSeconds": 5,
    "timeoutSeconds": 4,
    "failureThreshold": 12,
}
_SNAPSHOT_HOST_TOOLS = (
    ("dcmi", "/usr/local/dcmi"),
    ("npu-smi", "/usr/local/bin/npu-smi"),
)


def _normalized_absolute_path(config: dict, key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"'{C.MOTOR_CONTAINER_SNAPSHOT_CONFIG}.{key}' must be a non-empty absolute path when snapshot is enabled."
        )
    normalized = os.path.normpath(value.strip())
    if not os.path.isabs(normalized):
        raise ValueError(f"'{C.MOTOR_CONTAINER_SNAPSHOT_CONFIG}.{key}' must be an absolute path, got {value!r}.")
    return normalized


def _paths_overlap(first: str, second: str) -> bool:
    """Return whether either normalized path contains the other."""
    return os.path.commonpath((first, second)) in (first, second)


def validate_container_snapshot_config(user_config: dict) -> dict:
    """Validate and normalize deployment fields for the container snapshot template."""
    raw_config = user_config.get(C.MOTOR_CONTAINER_SNAPSHOT_CONFIG)
    if raw_config is None:
        return {}
    if not isinstance(raw_config, dict):
        raise ValueError(f"'{C.MOTOR_CONTAINER_SNAPSHOT_CONFIG}' must be an object.")

    enabled = raw_config.get(C.ENABLE_SNAPSHOT, False)
    if not isinstance(enabled, bool):
        raise ValueError(f"'{C.MOTOR_CONTAINER_SNAPSHOT_CONFIG}.{C.ENABLE_SNAPSHOT}' must be a JSON boolean.")
    if not enabled:
        return {}
    if raw_config.get(C.SNAPSHOT_METADATA_PATH):
        return {}

    deploy_config = user_config.get(C.MOTOR_DEPLOY_CONFIG, {})
    deploy_mode = deploy_config.get(C.DEPLOY_MODE_CONFIG_KEY, C.DEPLOY_MODE_INFER_SERVICE_SET)
    if deploy_mode != C.DEPLOY_MODE_INFER_SERVICE_SET:
        raise ValueError(
            "Container snapshot is supported only when motor_deploy_config.deploy_mode is 'infer_service_set'."
        )

    config = {key: _normalized_absolute_path(raw_config, key) for key in _REQUIRED_PATH_KEYS}
    if _paths_overlap(config[C.HOST_SNAPSHOT_IMAGE_PATH], config[C.SNAPSHOT_MNT_PATH]):
        raise ValueError(
            f"'{C.MOTOR_CONTAINER_SNAPSHOT_CONFIG}.{C.HOST_SNAPSHOT_IMAGE_PATH}' and "
            f"'{C.MOTOR_CONTAINER_SNAPSHOT_CONFIG}.{C.SNAPSHOT_MNT_PATH}' must not overlap."
        )
    return config


def resolve_infer_service_template(paths: dict, user_config: dict) -> str:
    """Validate snapshot config and use the common InferServiceSet template."""
    validate_container_snapshot_config(user_config)
    return paths["infer_service_input_yaml"]


def _replace_named_item(items: list[dict], name: str, values: dict, required: bool = False) -> None:
    replacement = {C.NAME: name, **values}
    for index, item in enumerate(items):
        if item.get(C.NAME) == name:
            items[index] = replacement
            return
    if required:
        raise ValueError(f"Container snapshot base template is missing required item '{name}'.")
    items.append(replacement)


def _remove_named_items(items: list[dict], names: frozenset[str]) -> None:
    items[:] = [item for item in items if item.get(C.NAME) not in names]


def _configure_snapshot_metadata(role: dict) -> None:
    role.setdefault(C.METADATA, {}).setdefault(C.LABELS, {})[_SNAPSHOT_LABEL] = "true"
    pod_template = role[C.SPEC][C.TEMPLATE]
    pod_labels = pod_template.setdefault(C.METADATA, {}).setdefault(C.LABELS, {})
    pod_labels[C.FAULT_SCHEDULING_LABEL] = _SNAPSHOT_FAULT_SCHEDULING


def _configure_snapshot_runtime(container: dict, snapshot_config: dict) -> None:
    container["readinessProbe"] = deepcopy(_SNAPSHOT_READINESS_PROBE)
    env = container.setdefault(C.ENV, [])
    _replace_named_item(env, "CRIU_LOG_LEVEL", {C.VALUE: "3"})
    _replace_named_item(
        env,
        C.SNAPSHOT_HOST_DIR_ENV,
        {C.VALUE: snapshot_config[C.HOST_SNAPSHOT_IMAGE_PATH]},
    )


def _set_host_path_mount(
    container: dict,
    pod_spec: dict,
    name: str,
    mount_path: str,
    host_path: str,
    *,
    host_path_type: str | None = None,
    mount_propagation: str | None = None,
    required: bool = False,
) -> None:
    mount = {C.MOUNT_PATH: mount_path}
    if mount_propagation is not None:
        mount["mountPropagation"] = mount_propagation
    volume_host_path = {C.PATH: host_path}
    if host_path_type is not None:
        volume_host_path[C.STORAGE_TYPE] = host_path_type
    _replace_named_item(container.setdefault(C.VOLUME_MOUNTS, []), name, mount, required=required)
    _replace_named_item(pod_spec.setdefault(C.VOLUMES, []), name, {C.HOST_PATH: volume_host_path}, required=required)


def _remove_snapshot_incompatible_storage(container: dict, pod_spec: dict) -> None:
    _remove_named_items(container.setdefault(C.VOLUME_MOUNTS, []), _SNAPSHOT_EXCLUDED_VOLUME_NAMES)
    _remove_named_items(pod_spec.setdefault(C.VOLUMES, []), _SNAPSHOT_EXCLUDED_VOLUME_NAMES)


def _configure_snapshot_storage(
    container: dict,
    pod_spec: dict,
    snapshot_config: dict,
    hardware_type: str,
) -> None:
    _remove_snapshot_incompatible_storage(container, pod_spec)
    _set_host_path_mount(
        container,
        pod_spec,
        C.SNAPSHOT_MNT,
        snapshot_config[C.SNAPSHOT_MNT_PATH],
        snapshot_config[C.SNAPSHOT_MNT_PATH],
        required=True,
    )
    _set_host_path_mount(
        container,
        pod_spec,
        C.SNAPSHOT_WEIGHT,
        "/snapshot/weight",
        snapshot_config[C.DEVICE_SNAPSHOT_WEIGHT_PATH],
        host_path_type="DirectoryOrCreate",
    )
    _set_host_path_mount(
        container,
        pod_spec,
        "ascend-driver",
        "/usr/local/Ascend/driver",
        "/usr/local/Ascend/driver",
        mount_propagation="HostToContainer",
        required=True,
    )
    for name, path in _SNAPSHOT_HOST_TOOLS:
        _set_host_path_mount(container, pod_spec, name, path, path)
    if hardware_type in C.HARDWARE_TYPE_A3:
        _set_host_path_mount(
            container,
            pod_spec,
            C.LQDCMI_PCIDEV,
            C.LQDCMI_PCIDEV_PATH,
            C.LQDCMI_PCIDEV_PATH,
            host_path_type="CharDevice",
        )


def _configure_snapshot_role(role: dict, snapshot_config: dict, hardware_type: str) -> None:
    _configure_snapshot_metadata(role)
    pod_spec = role[C.SPEC][C.TEMPLATE][C.SPEC]
    containers = pod_spec.get(C.CONTAINERS, [])
    if not containers:
        raise ValueError(f"Container snapshot role '{role.get(C.NAME)}' has no container.")
    container = containers[0]
    _configure_snapshot_runtime(container, snapshot_config)
    _configure_snapshot_storage(container, pod_spec, snapshot_config, hardware_type)


def configure_container_snapshot(infer_doc: dict, user_config: dict) -> None:
    """Render snapshot-specific deltas into the common InferServiceSet template."""
    snapshot_config = validate_container_snapshot_config(user_config)
    if not snapshot_config:
        return
    hardware_type = user_config[C.MOTOR_DEPLOY_CONFIG].get(C.HARDWARE_TYPE, C.HARDWARE_TYPE_800I_A2)
    roles = infer_doc.get(C.SPEC, {}).get(C.TEMPLATE, {}).get(C.ROLES, [])
    roles_by_name = {role.get(C.NAME): role for role in roles}
    for role_name in _SNAPSHOT_ENGINE_ROLES:
        role = roles_by_name.get(role_name)
        if role is None:
            raise ValueError(f"Container snapshot template is missing engine role '{role_name}'.")
        _configure_snapshot_role(role, snapshot_config, hardware_type)
