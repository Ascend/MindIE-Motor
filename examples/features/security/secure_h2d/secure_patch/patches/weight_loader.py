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

import torch

from ..config import CONFIG
from ..crypto_adapter import secure_h2d_tensor_inplace
from ..patch_utils import device_id_from_device, is_cpu_tensor, is_npu_tensor, normalize_device
from ..runtime import guard, log, remember_patch, wrap_errors

PATCH_NAME = "weight_loader"


def _debug_enabled() -> bool:
    return bool(getattr(CONFIG, "debug", False))


def _prepare_cpu_for_param_copy(
    loaded_weight: torch.Tensor,
    dst_dev: torch.Tensor,
) -> torch.Tensor:
    if loaded_weight.numel() != dst_dev.numel():
        raise RuntimeError(
            "secure weight H2D only supports same-numel tensors: "
            f"loaded_shape={tuple(loaded_weight.shape)}, "
            f"param_shape={tuple(dst_dev.shape)}, "
            f"loaded_numel={loaded_weight.numel()}, "
            f"param_numel={dst_dev.numel()}, "
            f"loaded_dtype={loaded_weight.dtype}, "
            f"param_dtype={dst_dev.dtype}"
        )

    if loaded_weight.dtype != dst_dev.dtype:
        if _debug_enabled():
            log(
                "[secure_patch][WEIGHT_H2D] dtype convert before crypto: "
                f"loaded_dtype={loaded_weight.dtype}, param_dtype={dst_dev.dtype}, "
                f"loaded_shape={tuple(loaded_weight.shape)}, "
                f"param_shape={tuple(dst_dev.shape)}, "
                f"loaded_nbytes={loaded_weight.numel() * loaded_weight.element_size()}, "
                f"param_nbytes={dst_dev.numel() * dst_dev.element_size()}"
            )
        prepared = loaded_weight.to(dtype=dst_dev.dtype)
    else:
        prepared = loaded_weight

    if tuple(prepared.shape) != tuple(dst_dev.shape):
        prepared = prepared.reshape_as(dst_dev)

    return prepared.contiguous()


def _should_fallback_to_original(param, loaded_weight) -> bool:
    if not isinstance(param, torch.Tensor):
        return True
    if not isinstance(loaded_weight, torch.Tensor):
        return True
    if not is_npu_tensor(param):
        return True
    if not is_cpu_tensor(loaded_weight):
        return True
    return False


def install_weight_loader_patch() -> bool:
    mod = importlib.import_module("vllm.model_executor.model_loader.weight_utils")
    original = getattr(mod, "default_weight_loader")

    if not remember_patch(
        "vllm.model_executor.model_loader.weight_utils.default_weight_loader",
        original,
    ):
        return False

    @wrap_errors(PATCH_NAME, original)
    def patched(param, loaded_weight, *args, **kwargs):
        with guard(PATCH_NAME) as active:
            if not active:
                return original(param, loaded_weight, *args, **kwargs)

            if _should_fallback_to_original(param, loaded_weight):
                return original(param, loaded_weight, *args, **kwargs)

            # 对齐 vLLM default_weight_loader 的语义：写入 param.data。
            dst_dev = param.data

            if not is_npu_tensor(dst_dev):
                return original(param, loaded_weight, *args, **kwargs)

            if not dst_dev.is_contiguous():
                if getattr(CONFIG, "strict", False):
                    raise RuntimeError(
                        "secure weight H2D requires contiguous param.data: "
                        f"shape={tuple(dst_dev.shape)}, dtype={dst_dev.dtype}, "
                        f"device={dst_dev.device}"
                    )

                log(
                    "weight_loader fallback: non-contiguous param.data, "
                    f"shape={tuple(dst_dev.shape)}, dtype={dst_dev.dtype}, "
                    f"device={dst_dev.device}"
                )
                return original(param, loaded_weight, *args, **kwargs)

            target_device = normalize_device(dst_dev.device)
            device_id = device_id_from_device(target_device)

            prepared_cpu = _prepare_cpu_for_param_copy(
                loaded_weight,
                dst_dev,
            )

            if _debug_enabled():
                log(
                    "[secure_patch][WEIGHT_H2D] request: "
                    f"loaded_shape={tuple(loaded_weight.shape)}, "
                    f"param_shape={tuple(dst_dev.shape)}, "
                    f"loaded_dtype={loaded_weight.dtype}, "
                    f"param_dtype={dst_dev.dtype}, "
                    f"prepared_shape={tuple(prepared_cpu.shape)}, "
                    f"prepared_dtype={prepared_cpu.dtype}, "
                    f"nbytes={prepared_cpu.numel() * prepared_cpu.element_size()}, "
                    f"device={target_device}, device_id={device_id}"
                )

            with torch.no_grad():
                secure_h2d_tensor_inplace(
                    prepared_cpu,
                    dst_dev=dst_dev,
                    device_id=device_id,
                )

            if _debug_enabled():
                log(
                    "secure weight H2D inplace: "
                    f"shape={tuple(dst_dev.shape)}, "
                    f"dtype={dst_dev.dtype}, "
                    f"device={target_device}"
                )
            return None

    setattr(mod, "default_weight_loader", patched)
    return True
