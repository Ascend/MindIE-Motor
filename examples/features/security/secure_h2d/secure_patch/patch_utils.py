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

import contextvars
from contextlib import contextmanager
from typing import Any, Iterable, Optional, Set

import torch


_PREPARE_INPUT_IDS_ALLOWED_BUFFERS: contextvars.ContextVar[Optional[Set[int]]] = contextvars.ContextVar(
    "secure_patch_prepare_input_ids_allowed_buffers",
    default=None,
)


def is_cpu_tensor(value) -> bool:
    return isinstance(value, torch.Tensor) and value.device.type == "cpu"


def is_npu_tensor(value) -> bool:
    return isinstance(value, torch.Tensor) and value.device.type == "npu"


def normalize_device(device) -> torch.device:
    dev = torch.device(device)
    if dev.type != "npu":
        raise RuntimeError(f"expected npu device, got={dev}")
    if dev.index is None:
        try:
            return torch.device("npu", int(torch.npu.current_device()))
        except Exception:
            return torch.device("npu", 0)
    return dev


def device_id_from_device(device) -> int:
    return int(normalize_device(device).index or 0)


def device_id_from_tensor(t: torch.Tensor) -> int:
    if t.device.type != "npu":
        raise RuntimeError(f"expected npu tensor, got={t.device}")
    return device_id_from_device(t.device)


def byte_view(t: torch.Tensor) -> torch.Tensor:
    c = t.contiguous()
    if c.dtype == torch.uint8:
        return c.reshape(-1)
    return c.view(torch.uint8).reshape(-1)


def restore_from_byte_view(t: torch.Tensor, *, dtype: torch.dtype, shape) -> torch.Tensor:
    if dtype == torch.uint8:
        return t.reshape(shape).contiguous()
    return t.view(dtype).reshape(shape).contiguous()


def supported_tensor(t: torch.Tensor) -> bool:
    return isinstance(t, torch.Tensor) and t.numel() > 0 and t.layout == torch.strided


def current_prepare_input_ids_allowed_buffers() -> Optional[Set[int]]:
    return _PREPARE_INPUT_IDS_ALLOWED_BUFFERS.get()


def set_prepare_input_ids_allowed_buffers(buffers: Iterable[Any]):
    ids: Set[int] = set()

    for item in buffers:
        if isinstance(item, int):
            ids.add(item)
        elif item is not None:
            ids.add(id(item))

    return _PREPARE_INPUT_IDS_ALLOWED_BUFFERS.set(ids)


def reset_prepare_input_ids_allowed_buffers(token) -> None:
    _PREPARE_INPUT_IDS_ALLOWED_BUFFERS.reset(token)


@contextmanager
def prepare_input_ids_allowed_buffers(buffers: Iterable[Any]):
    token = set_prepare_input_ids_allowed_buffers(buffers)
    try:
        yield
    finally:
        reset_prepare_input_ids_allowed_buffers(token)


def collect_cpu_gpu_buffer_ids(value: Any) -> Set[int]:
    result: Set[int] = set()
    seen: Set[int] = set()

    def visit(x: Any) -> None:
        oid = id(x)
        if oid in seen:
            return
        seen.add(oid)

        cpu = getattr(x, "cpu", None)
        gpu = getattr(x, "gpu", None)
        if is_cpu_tensor(cpu) and is_npu_tensor(gpu):
            result.add(id(x))
            return

        if isinstance(x, dict):
            for k, v in x.items():
                visit(k)
                visit(v)
            return

        if isinstance(x, (tuple, list, set, frozenset)):
            for item in x:
                visit(item)
            return

    visit(value)
    return result


set_current_prepare_input_ids_allowed_buffers = set_prepare_input_ids_allowed_buffers
reset_current_prepare_input_ids_allowed_buffers = reset_prepare_input_ids_allowed_buffers
prepare_input_ids_allowed_buffers_scope = prepare_input_ids_allowed_buffers
push_prepare_input_ids_allowed_buffers = set_prepare_input_ids_allowed_buffers
pop_prepare_input_ids_allowed_buffers = reset_prepare_input_ids_allowed_buffers
