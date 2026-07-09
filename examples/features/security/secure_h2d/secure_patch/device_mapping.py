# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from __future__ import annotations

import os

from .config import CONFIG


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _device_id_mode() -> str:
    return str(getattr(CONFIG, "device_id_mode", "A2")).strip().upper()


def _parse_visible_devices() -> list[int] | None:
    value = os.getenv("ASCEND_RT_VISIBLE_DEVICES")
    if value is None or value.strip() == "":
        return None

    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        result.append(int(item))

    return result if result else None


def enable_visible_device_mapping() -> bool:
    return _env_bool("SECURE_PATCH_ENABLE_VISIBLE_DEVICE_MAPPING", True)


def global_device_id_from_local(local_device_id: int) -> int:
    local = int(local_device_id)

    if enable_visible_device_mapping():
        visible = _parse_visible_devices()
        if visible is not None:
            if local < 0 or local >= len(visible):
                raise RuntimeError(f"local_device_id={local} out of ASCEND_RT_VISIBLE_DEVICES={visible}")
            return int(visible[local])

    return local


def kms_device_id_from_global(global_device_id: int) -> int:
    gid = int(global_device_id)
    mode = _device_id_mode()

    if mode in {"A2", "IDENTITY"}:
        return gid

    if mode in {"A3", "A3_EVEN", "EVEN"}:
        return (gid // 2) * 2

    raise RuntimeError(f"unsupported SECURE_PATCH_DEVICE_ID_MODE={mode!r}, expected A2 or A3")


def kms_device_id_from_local(local_device_id: int) -> int:
    return kms_device_id_from_global(global_device_id_from_local(local_device_id))


def op_device_id_from_local(local_device_id: int) -> int:
    return global_device_id_from_local(local_device_id)


def physical_device_id_from_logical(logical_device_id: int) -> int:
    return kms_device_id_from_local(logical_device_id)
