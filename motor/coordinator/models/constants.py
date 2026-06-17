# -*- coding: utf-8 -*-
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
Coordinator API constants: request ID prefixes, keys, OpenAI schema fields.
"""

__all__ = [
    "CHAT_COMPLETION_PREFIX",
    "COMPLETION_PREFIX",
    "COMPLETION_SUFFIX",
    "DEFAULT_REQUEST_ID",
    "REQUEST_ID_KEY",
    "REQUEST_DATA_KEY",
    "RESOURCE_KEY",
    "OpenAIField",
]

from enum import StrEnum
from typing import Final

from motor.common.constants import (
    CHAT_COMPLETION_PREFIX,
    COMPLETION_PREFIX,
    COMPLETION_SUFFIX,
)

# Re-export from common for backward compatibility
# (coordinator.models.constants remains the main import for coordinator code)

DEFAULT_REQUEST_ID: Final[str] = "unknown"
REQUEST_ID_KEY: Final[str] = "req_id"
REQUEST_DATA_KEY: Final[str] = "req_data"
RESOURCE_KEY: Final[str] = "resource"


class OpenAIField(StrEnum):
    """OpenAI-style API request/response field names (body, choices, usage)."""

    MESSAGES = "messages"
    PROMPT = "prompt"
    MODEL = "model"
    STREAM = "stream"
    STREAM_OPTIONS = "stream_options"
    ROLE = "role"
    CONTENT = "content"
    MIN_TOKENS = "min_tokens"
    MAX_TOKENS = "max_tokens"
    MAX_COMPLETION_TOKENS = "max_completion_tokens"
    COMPLETION_TOKENS = "completion_tokens"
    PROMPT_TOKEN_IDS = "prompt_token_ids"
    CHOICES = "choices"
    USAGE = "usage"
    TOKEN_IDS = "token_ids"
    DELTA = "delta"
    TEXT = "text"
    MESSAGE = "message"
    TOOLS = "tools"
    TOOLS_CALLS = "tool_calls"
    FUNCTION = "function" 
    ARGUMENTS = "arguments"