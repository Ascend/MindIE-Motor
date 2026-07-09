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
import os
import warnings
from typing import Callable

import torch

from .constants import VERSION

_CACHED_CRYPTO_OP = None


def _import_torch_npu() -> None:
    try:
        importlib.import_module("torch_npu")
    except (ImportError, RuntimeError, OSError) as exc:
        if os.getenv("SECURE_PATCH_DEBUG", "0") == "1":
            warnings.warn(f"import torch_npu failed: {exc!r}", RuntimeWarning)


def _torch_npu_crypto():
    global _CACHED_CRYPTO_OP
    if _CACHED_CRYPTO_OP is not None:
        return _CACHED_CRYPTO_OP
    _import_torch_npu()
    try:
        _CACHED_CRYPTO_OP = torch.ops.npu.crypto
        return _CACHED_CRYPTO_OP
    except Exception as exc:
        raise RuntimeError("cannot resolve torch.ops.npu.crypto") from exc


def create_crypto_op_config(
    *, mode: int, alg_type: int, key_type: int, key_id: int, device_id: int, device
) -> torch.Tensor:
    return torch.tensor(
        [
            int(VERSION),
            int(mode),
            int(alg_type),
            int(key_type),
            int(key_id),
            int(device_id),
        ],
        dtype=torch.uint32,
        device=device,
    )


def load_host_function(module_name: str, function_name: str) -> Callable:
    mod = importlib.import_module(module_name)
    return getattr(mod, function_name)
