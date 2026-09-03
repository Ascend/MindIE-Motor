# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Extract a text-only scheduling view from a native Responses request."""

from typing import Any


def responses_scheduling_messages(request: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Build messages for tokenization without changing the body sent to the engine.

    Return ``None`` when an input item cannot be reproduced safely by the current
    text scheduler. The native request is still forwarded, but KV affinity falls
    back instead of claiming a potentially incorrect prefix match.
    """
    input_value = request.get("input")
    messages: list[dict[str, Any]] = []
    instructions = request.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    if isinstance(input_value, str):
        return [*messages, {"role": "user", "content": input_value}]
    if not isinstance(input_value, list) or not input_value:
        return None

    for item in input_value:
        if not isinstance(item, dict):
            return None
        role = item.get("role")
        if role not in {"system", "developer", "user", "assistant"}:
            return None
        content = _text_content(item.get("content"))
        if content is None:
            return None
        messages.append(
            {
                "role": "system" if role == "developer" else role,
                "content": content,
            }
        )
    return messages


def _text_content(content: Any) -> str | list[dict[str, str]] | None:
    if isinstance(content, str) and content:
        return content
    if not isinstance(content, list) or not content:
        return None
    parts: list[dict[str, str]] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in {"input_text", "output_text"}:
            return None
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return None
        parts.append({"type": "text", "text": text})
    return parts
