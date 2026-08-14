# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.node_manager.core.services.native_engine.backends.base import NativeEngineBackend
from motor.node_manager.core.services.native_engine.backends.sglang.backend import SGLangBackend
from motor.node_manager.core.services.native_engine.backends.vllm.backend import VllmBackend


_BACKENDS: dict[str, NativeEngineBackend] = {
    VllmBackend.engine_type: VllmBackend(),
    SGLangBackend.engine_type: SGLangBackend(),
}


def get_backend(engine_type: str | None) -> NativeEngineBackend:
    """Return the stateless backend for the configured native engine type."""
    normalized = str(engine_type or "").strip().lower()
    backend = _BACKENDS.get(normalized)
    if backend is None:
        raise ValueError(f"Unsupported engine type: {engine_type}")
    return backend
