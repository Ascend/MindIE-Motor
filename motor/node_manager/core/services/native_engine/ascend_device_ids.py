# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Slice Ascend device IDs per engine endpoint.

Device-plugin pods either remap chips to ``/dev/davinci0..n-1`` or pass through
physical nodes (e.g. ``0,1,12,13``). ``ASCEND_RT_VISIBLE_DEVICES`` must match
the char devices that actually exist. Plugin ``ASCEND_VISIBLE_DEVICES`` is only
left untouched when nodes are already the contiguous 0..n-1 remap.
"""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Mapping

_ALLOCATED_ENV = "ASCEND_ALLOCATED_DEVICES"
_START_DEVICES_ENV = "MOTOR_START_DEVICES"
A5_DOCKER_PHYSICAL_IDS_ENV = "MOTOR_A5_DOCKER_PHYSICAL_IDS"
_VISIBLE_ENV_KEYS = ("ASCEND_VISIBLE_DEVICES", "ASCEND_RT_VISIBLE_DEVICES")
_CHIP_SUFFIX = re.compile(r"(\d+)$")
_DAVINCI_NODE = re.compile(r"^/dev/davinci(\d+)$")


def _sort_device_id_strings(device_ids: list[str]) -> list[str]:
    return sorted(device_ids, key=int)


def list_pod_physical_device_ids(env: Mapping[str, str] | None = None) -> list[str]:
    """Return plugin/env device ids if set, otherwise ``/dev/davinci*`` numbers."""
    source = env if env is not None else os.environ
    for key in _VISIBLE_ENV_KEYS:
        raw = source.get(key)
        if not raw:
            continue
        ids = [part.strip() for part in raw.split(",") if part.strip()]
        if ids:
            return _sort_device_id_strings(ids)
    return list_davinci_char_devices()


def list_davinci_char_devices() -> list[str]:
    """Sorted numeric ids of ``/dev/davinciN`` (excludes ``davinci_manager``)."""
    device_ids: list[str] = []
    for device_path in glob.glob("/dev/davinci*"):
        match = _DAVINCI_NODE.match(device_path)
        if match:
            device_ids.append(match.group(1))
    return _sort_device_id_strings(device_ids)


def is_contiguous_logical_ids(device_ids: list[str]) -> bool:
    """True when ids are exactly 0..n-1 (HAL remap or naturally first chips)."""
    if not device_ids:
        return False
    return [int(x) for x in device_ids] == list(range(len(device_ids)))


def parse_allocated_device_ids(raw: str | None) -> list[str]:
    """Parse ``Ascend910-8,Ascend910-9`` or ``8,9,10,11`` into sorted chip ids."""
    if not raw:
        return []
    device_ids: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        match = _CHIP_SUFFIX.search(part)
        if match:
            device_ids.append(match.group(1))
    return _sort_device_id_strings(device_ids)


def _looks_like_full_host(device_ids: list[str]) -> bool:
    """Privileged pods often expose every chip (0..15); that is not the allocation."""
    return len(device_ids) >= 8 and is_contiguous_logical_ids(device_ids)


def list_runtime_device_ids(
    env: Mapping[str, str] | None = None,
    device_num: int = 0,
) -> list[str]:
    """Device ids this engine may use: scheduler allocation, then /dev, then env."""
    source = env if env is not None else os.environ
    docker_ids = parse_device_id_list(source.get(A5_DOCKER_PHYSICAL_IDS_ENV))
    if docker_ids:
        return docker_ids
    allocated = parse_allocated_device_ids(source.get(_ALLOCATED_ENV))
    if allocated:
        return allocated

    davinci_ids = list_davinci_char_devices()
    if davinci_ids and not _looks_like_full_host(davinci_ids):
        if device_num <= 0 or len(davinci_ids) <= device_num:
            return davinci_ids

    visible = list_pod_physical_device_ids(source)
    if visible and not _looks_like_full_host(visible):
        return visible
    return []


def list_engine_visible_device_ids(
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Device ids for engine ``ASCEND_RT_VISIBLE_DEVICES`` when slicing a full-host pod.

    CANN ``ASCEND_RT_VISIBLE_DEVICES`` indexes ``0..drvGetDevNum()-1``. Normal
    device-plugin containers only attach the pod's own cards, so the engine must
    use logical ``0..n-1`` (returning ``[]``). Only privileged pods that expose
    all host ``/dev/davinci0..N-1`` share host chip numbering with CANN and slice
    ``ASCEND_ALLOCATED_DEVICES`` directly.
    """
    source = env if env is not None else os.environ
    allocated = parse_allocated_device_ids(source.get(_ALLOCATED_ENV))
    if not allocated:
        return []
    davinci_ids = list_davinci_char_devices()
    if davinci_ids and not (_looks_like_full_host(davinci_ids) and set(allocated).issubset(set(davinci_ids))):
        return []
    return allocated


def parse_device_id_list(raw: str | None) -> list[str]:
    """Parse a comma separated device id list, preserving the caller's order."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def select_a5_docker_physical_device_ids(
    env: Mapping[str, str],
    mounted_ids: list[str],
    device_num: int,
    device_offset: int | None,
    *,
    devices_per_endpoint: int,
) -> list[str]:
    """Return the role's compute devices in launch order, excluding extra mounts."""
    explicit_ids = parse_device_id_list(env.get(_START_DEVICES_ENV))
    if explicit_ids:
        selected_ids = explicit_ids[:device_num]
    else:
        device_offset = device_offset or 0
        if device_offset < 0:
            raise ValueError(f"Invalid A5 Docker device offset {device_offset}")
        selected_ids = mounted_ids[device_offset : device_offset + device_num]

    if (
        device_num <= 0
        or len(selected_ids) != device_num
        or any(not card.isdecimal() or card not in mounted_ids for card in selected_ids)
        or len(set(map(int, selected_ids))) != device_num
    ):
        raise ValueError(
            f"A5 Docker needs {device_num} distinct mounted cards, selected {selected_ids} from {mounted_ids}"
        )
    # CANN accepts only ascending runtime IDs. Keep the assigned cards in each endpoint.
    for start in range(0, device_num, devices_per_endpoint):
        end = start + devices_per_endpoint
        selected_ids[start:end] = sorted(selected_ids[start:end], key=int)
    return selected_ids


def a5_docker_runtime_device_ids(physical_selection: str, physical_ids: list[str]) -> str:
    """Translate a selection of physical Ascend 950 Docker devices into CANN's local indices."""
    try:
        return ",".join(str(physical_ids.index(card)) for card in parse_device_id_list(physical_selection))
    except ValueError as exc:
        raise ValueError(f"Selected A5 cards ({physical_selection}) are not all mounted: {physical_ids}") from exc


def list_pod_device_ids(
    env: Mapping[str, str] | None = None,
    device_num: int = 0,
) -> list[str]:
    """Pod device ids in the order native engine launch uses them.

    ``MOTOR_START_DEVICES`` comes from host ``--start --devices`` and must win:
    ``NativeEngineService`` slices exactly that list. Otherwise fall back to the
    scheduler allocation / device nodes.
    """
    source = env if env is not None else os.environ
    start_ids = parse_device_id_list(source.get(_START_DEVICES_ENV))
    if start_ids:
        return start_ids
    return list_runtime_device_ids(source, device_num=device_num)


def _logical_slice_indices(index: int, device_size: int, local_world_size: int) -> list[int]:
    start_device_id = index * local_world_size % device_size
    end_device_id = start_device_id + local_world_size
    if end_device_id > device_size:
        return list(range(start_device_id, device_size)) + list(range(end_device_id - device_size))
    return list(range(start_device_id, end_device_id))


def list_visible_device_ids(env: Mapping[str, str] | None = None) -> list[str]:
    """Host-visible chip ids for Controller registration.

    Reads ``ASCEND_VISIBLE_DEVICES`` only, then ``/dev/davinciN`` when ``env``
    is omitted. ``ASCEND_RT_VISIBLE_DEVICES`` and ``ASCEND_ALLOCATED_DEVICES``
    belong to the engine's logical space and are ignored.
    """
    source = env if env is not None else os.environ
    raw = source.get("ASCEND_VISIBLE_DEVICES")
    if raw:
        ids = [part.strip() for part in raw.split(",") if part.strip()]
        if ids:
            return _sort_device_id_strings(ids)
    if env is not None:
        return []
    return list_davinci_char_devices()


def slice_physical_device_ids(device_ids: list[str], device_offset: int, device_num: int) -> list[str]:
    """Slice a sorted visible list by this role's ``device_offset``.

    ``--devices 2,3,4,5`` with prefill offset 0 yields ``2,3``; decode offset 2
    yields ``4,5``. The engine does not consume this result.
    """
    if device_num <= 0 or device_offset < 0:
        return []
    ordered = _sort_device_id_strings(list(device_ids))
    end = device_offset + device_num
    if end > len(ordered):
        return []
    return ordered[device_offset:end]


def calc_endpoint_visible_device_ids(
    index: int,
    device_size: int,
    local_world_size: int,
    *,
    pod_physical_ids: list[str] | None,
    single_container_flag: bool,
    device_offset: int,
) -> str:
    """Build per-endpoint device id list for ``ASCEND_RT_VISIBLE_DEVICES``.

    When ``pod_physical_ids`` comes from real ``/dev/davinciN`` nodes, slice
    that list (pass-through physical chips). Contiguous 0..n-1 is the remap
    case and yields the same numbers as logical indices.

    Single container keeps ``device_offset`` as this role's logical slot.
    The engine uses that logical index. Controller registration slices
    host-visible ids separately.
    """
    logical_indices = _logical_slice_indices(index, device_size, local_world_size)

    if single_container_flag:
        return ",".join(str(logical + device_offset) for logical in logical_indices)

    runtime_ids = pod_physical_ids or []
    if runtime_ids:
        count = len(runtime_ids)
        return ",".join(runtime_ids[i % count] for i in logical_indices)

    return ",".join(str(i) for i in logical_indices)
