# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Container IPC configuration for Docker and Kubernetes deployers."""

import lib.constant as C


def host_ipc_enabled(env_config: dict) -> bool:
    """Read the container IPC opt-in from env.json, without shell-env fallback."""
    value = env_config.get(C.MOTOR_COMMON_ENV, {}).get("MOTOR_ENABLE_IPC_HOST", 0)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise ValueError("motor_common_env.MOTOR_ENABLE_IPC_HOST must be 0 or 1 (false or true)")


def apply_k8s_host_ipc(pod_spec: dict, enabled: bool) -> None:
    """Use host IPC and let the runtime supply /dev/shm without a volume override."""
    if not enabled:
        return
    pod_spec["hostIPC"] = True
    shm_path = "/dev/shm"  # nosec B108 - compare mount destinations; no temporary file is created
    if C.VOLUMES in pod_spec:
        pod_spec[C.VOLUMES] = [v for v in pod_spec[C.VOLUMES] if v.get(C.NAME) != C.DSHM_VOLUME]
    for container_key in (C.CONTAINERS, "initContainers"):
        for container in pod_spec.get(container_key, []):
            if C.VOLUME_MOUNTS in container:
                container[C.VOLUME_MOUNTS] = [
                    mount
                    for mount in container[C.VOLUME_MOUNTS]
                    if mount.get(C.NAME) != C.DSHM_VOLUME and mount.get(C.MOUNT_PATH) != shm_path
                ]
