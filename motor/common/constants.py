# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Shared API format constants (OpenAI/vLLM compatible).
Used by coordinator and engine_server.
"""

__all__ = [
    "CHAT_COMPLETION_PREFIX",
    "COMPLETION_PREFIX",
    "COMPLETION_SUFFIX",
    "FT_STATUS_PATH",
    "ENGINE_STATUS_HEALTHY",
    "ENGINE_STATUS_DEAD",
    "ENGINE_STATUS_UNHEALTHY",
    "ENGINE_STATUS_UNKNOWN",
    "ENGINE_FT_TIMEOUT",
]

from typing import Final

# vLLM/OpenAI request ID format: chatcmpl-xxx or cmpl-xxx-0
CHAT_COMPLETION_PREFIX: Final[str] = "chatcmpl-"
COMPLETION_PREFIX: Final[str] = "cmpl-"
# /v1/completions: cmpl-xxx-0
COMPLETION_SUFFIX: Final[str] = "-0"

# Engine FT API routes (vLLM FaultTolerance framework, vllm-project/vllm#44428).
FT_STATUS_PATH: Final[str] = "/fault_tolerance/status"

# Engine FT status vocabulary.
ENGINE_STATUS_HEALTHY: Final[str] = "healthy"
ENGINE_STATUS_DEAD: Final[str] = "dead"
ENGINE_STATUS_UNHEALTHY: Final[str] = "unhealthy"
ENGINE_STATUS_UNKNOWN: Final[str] = "unknown"

# Engine FT API timeout (business port, non-TLS).
ENGINE_FT_TIMEOUT: Final[float] = 5.0
