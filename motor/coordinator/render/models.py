# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Coordinator-owned models for normalized tokenization results."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TokenizerSource(str, Enum):
    """Frontend component that produced the final prompt token IDs."""

    RENDER = "render"
    LOCAL = "local"


class TokenizedRequest(BaseModel):
    """Runtime-neutral prompt tokenization result used by Coordinator."""

    prompt_token_ids: list[int]
    tokenizer_source: TokenizerSource
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("prompt_token_ids", mode="before")
    @classmethod
    def validate_prompt_token_ids(cls, value: Any) -> list[int]:
        if not isinstance(value, list) or not value:
            raise ValueError("prompt_token_ids must not be empty")
        if any(not isinstance(token_id, int) or isinstance(token_id, bool) or token_id < 0 for token_id in value):
            raise ValueError("prompt_token_ids must contain non-negative integers")
        return value
