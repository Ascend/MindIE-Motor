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

import importlib
from typing import Optional, Tuple

import torch

from ..config import CONFIG
from ..crypto_adapter import secure_h2d_tensor_inplace
from ..patch_utils import (
    current_prepare_input_ids_allowed_buffers,
    device_id_from_device,
    is_cpu_tensor,
    is_npu_tensor,
    normalize_device,
)
from ..runtime import guard, log, remember_patch, wrap_errors

PATCH_NAME = "copy_to_gpu"


def _find_pair(obj) -> Optional[Tuple[torch.Tensor, torch.Tensor]]:
    for cpu_name, gpu_name in (
        ("cpu", "gpu"),
        ("cpu_tensor", "gpu_tensor"),
        ("cpu_buffer", "gpu_buffer"),
        ("_cpu", "_gpu"),
    ):
        cpu = getattr(obj, cpu_name, None)
        gpu = getattr(obj, gpu_name, None)
        if is_cpu_tensor(cpu) and is_npu_tensor(gpu):
            return cpu, gpu
    return None


def _debug_enabled() -> bool:
    return bool(getattr(CONFIG, "debug", False))


def _extract_num_elems(args, kwargs):
    if "num_elems" in kwargs:
        return kwargs["num_elems"]
    if len(args) >= 1:
        return args[0]
    return None


def _slice_by_num_elems(
    cpu_tensor: torch.Tensor,
    gpu_tensor: torch.Tensor,
    num_elems,
) -> Tuple[torch.Tensor, torch.Tensor]:
    if num_elems is None:
        return cpu_tensor, gpu_tensor

    n = int(num_elems)
    if n < 0:
        raise RuntimeError(f"copy_to_gpu num_elems must be >= 0, got={n}")

    return cpu_tensor[:n], gpu_tensor[:n]


def _prepare_cpu_for_gpu_copy(
    cpu_tensor: torch.Tensor,
    gpu_tensor: torch.Tensor,
) -> torch.Tensor:
    if cpu_tensor.numel() != gpu_tensor.numel():
        raise RuntimeError(
            "secure copy_to_gpu only supports same-numel tensors: "
            f"cpu_shape={tuple(cpu_tensor.shape)}, "
            f"gpu_shape={tuple(gpu_tensor.shape)}, "
            f"cpu_numel={cpu_tensor.numel()}, "
            f"gpu_numel={gpu_tensor.numel()}, "
            f"cpu_dtype={cpu_tensor.dtype}, "
            f"gpu_dtype={gpu_tensor.dtype}"
        )

    if cpu_tensor.dtype != gpu_tensor.dtype:
        prepared = cpu_tensor.to(dtype=gpu_tensor.dtype)
    else:
        prepared = cpu_tensor

    if tuple(prepared.shape) != tuple(gpu_tensor.shape):
        prepared = prepared.reshape_as(gpu_tensor)

    return prepared.contiguous()


def install_copy_to_gpu_patch() -> bool:
    mod = importlib.import_module("vllm.v1.utils")
    cls = getattr(mod, "CpuGpuBuffer")
    original = getattr(cls, "copy_to_gpu")

    if not remember_patch("vllm.CpuGpuBuffer.copy_to_gpu", original):
        return False

    @wrap_errors(PATCH_NAME, original)
    def patched(self, *args, **kwargs):
        with guard(PATCH_NAME) as active:
            if not active:
                return original(self, *args, **kwargs)

            allowed = current_prepare_input_ids_allowed_buffers()
            if allowed is None or id(self) not in allowed:
                if bool(getattr(CONFIG, "trace_bypass", False)):
                    log("copy_to_gpu bypass: not in prepare_input_ids allowed buffers")
                return original(self, *args, **kwargs)

            pair = _find_pair(self)
            if pair is None:
                return original(self, *args, **kwargs)

            cpu_tensor, gpu_tensor = pair
            num_elems = _extract_num_elems(args, kwargs)
            src, dst = _slice_by_num_elems(cpu_tensor, gpu_tensor, num_elems)

            if src.numel() == 0:
                return dst.copy_(src, non_blocking=True)

            target_device = normalize_device(dst.device)
            device_id = device_id_from_device(target_device)

            prepared_cpu = _prepare_cpu_for_gpu_copy(src, dst)

            ret = secure_h2d_tensor_inplace(
                prepared_cpu,
                dst_dev=dst,
                device_id=device_id,
            )

            return ret

    setattr(cls, "copy_to_gpu", patched)
    return True
