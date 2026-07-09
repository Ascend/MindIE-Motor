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

from .config import CONFIG
from .runtime import log, safe_patch


def install_all() -> None:
    if not CONFIG.enable:
        log("SECURE_PATCH_ENABLE=0")
        return

    if CONFIG.patch_copy_to_gpu:
        from .patches.copy_to_gpu import install_copy_to_gpu_patch  # pylint: disable=import-outside-toplevel

        safe_patch("vllm.CpuGpuBuffer.copy_to_gpu", install_copy_to_gpu_patch)

    if CONFIG.patch_async_output_d2h:
        from .patches.async_output_d2h import install_async_output_d2h_patch  # pylint: disable=import-outside-toplevel

        safe_patch("vllm.AsyncGPUModelRunnerOutput.__init__", install_async_output_d2h_patch)

    if CONFIG.patch_weight_loader:
        from .patches.weight_loader import install_weight_loader_patch  # pylint: disable=import-outside-toplevel

        safe_patch("vllm.default_weight_loader", install_weight_loader_patch)
