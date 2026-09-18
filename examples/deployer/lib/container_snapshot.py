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

import lib.constant as C


_SNAPSHOT_ENGINE_ROLES = (C.ROLE_PREFILL, C.ROLE_DECODE, C.ROLE_UNION)
_REQUIRED_PATH_KEYS = (C.HOST_SNAPSHOT_IMAGE_PATH, C.SNAPSHOT_MNT_PATH, C.DEVICE_SNAPSHOT_WEIGHT_PATH)


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
    """Select the snapshot-specific InferServiceSet template when the feature is enabled."""
    snapshot_config = validate_container_snapshot_config(user_config)
    if snapshot_config:
        return paths["container_snapshot_infer_service_input_yaml"]
    return paths["infer_service_input_yaml"]


def _set_named_item(items: list, name: str, key: str, value: object) -> None:
    for item in items:
        if item.get(C.NAME) == name:
            item[key] = value
            return
    raise ValueError(f"Container snapshot template is missing '{name}' in {key} configuration.")


def _configure_snapshot_role(role: dict, snapshot_config: dict, hardware_type: str) -> None:
    pod_spec = role[C.SPEC][C.TEMPLATE][C.SPEC]
    containers = pod_spec.get(C.CONTAINERS, [])
    if not containers:
        raise ValueError(f"Container snapshot role '{role.get(C.NAME)}' has no container.")
    container = containers[0]

    _set_named_item(
        container.get(C.ENV, []),
        C.SNAPSHOT_HOST_DIR_ENV,
        C.VALUE,
        snapshot_config[C.HOST_SNAPSHOT_IMAGE_PATH],
    )
    _set_named_item(
        container.get(C.VOLUME_MOUNTS, []),
        C.SNAPSHOT_MNT,
        C.MOUNT_PATH,
        snapshot_config[C.SNAPSHOT_MNT_PATH],
    )
    _set_named_item(
        pod_spec.get(C.VOLUMES, []),
        C.SNAPSHOT_MNT,
        C.HOST_PATH,
        {C.PATH: snapshot_config[C.SNAPSHOT_MNT_PATH]},
    )
    _set_named_item(
        pod_spec.get(C.VOLUMES, []),
        C.SNAPSHOT_WEIGHT,
        C.HOST_PATH,
        {C.PATH: snapshot_config[C.DEVICE_SNAPSHOT_WEIGHT_PATH], C.STORAGE_TYPE: "DirectoryOrCreate"},
    )

    if hardware_type in C.HARDWARE_TYPE_A3:
        mounts = container.setdefault(C.VOLUME_MOUNTS, [])
        volumes = pod_spec.setdefault(C.VOLUMES, [])
        if not any(item.get(C.NAME) == C.LQDCMI_PCIDEV for item in mounts):
            mounts.append({C.NAME: C.LQDCMI_PCIDEV, C.MOUNT_PATH: C.LQDCMI_PCIDEV_PATH})
        if not any(item.get(C.NAME) == C.LQDCMI_PCIDEV for item in volumes):
            volumes.append(
                {
                    C.NAME: C.LQDCMI_PCIDEV,
                    C.HOST_PATH: {C.PATH: C.LQDCMI_PCIDEV_PATH, C.STORAGE_TYPE: "CharDevice"},
                }
            )


def configure_container_snapshot(infer_doc: dict, user_config: dict) -> None:
    """Render snapshot paths and A3-only device mounts into all snapshot engine roles."""
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
