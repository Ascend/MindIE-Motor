# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import copy
import json
from motor.coordinator.models.constants import OpenAIField

# Maps OpenAI content-part ``type`` -> text field name, matching vLLM
# ``MM_PARSER_MAP`` in ``vllm/entrypoints/chat_utils.py``.
_TEXT_CONTENT_PART_FIELDS = {
    "text": "text",
    "input_text": "text",
    "output_text": "text",
    "refusal": "refusal",
    "thinking": "thinking",
}


def _extract_text_from_content_part(part: dict) -> str | None:
    """Extract plain text from a typed OpenAI content part dict.

    Returns ``None`` for non-text / unsupported part types so the caller can
    emit an ``[Unsupported ...]`` placeholder (same as vLLM string format).
    """
    part_type = part.get("type")
    if part_type is None:
        return None
    field = _TEXT_CONTENT_PART_FIELDS.get(part_type)
    if field is None:
        return None
    # Prefer the type-specific field; fall back to ``text`` / rare aliases so
    # slightly non-spec payloads still contribute tokens for KV affinity.
    value = part.get(field)
    if value is None and field != "text":
        value = part.get("text")
    if value is None and part_type == "thinking":
        value = part.get("reasoning_content")
    return value or ""


def content_parts_to_string(content) -> str:
    """Flatten OpenAI string or multipart content to a single string.

    Mirrors vLLM ``parse_chat_messages(..., content_format="string")`` for
    text-only parts so DeepSeek V4 ``encode_messages`` receives plain strings.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict):
                part_type = part.get("type")
                extracted = _extract_text_from_content_part(part)
                if extracted is not None:
                    texts.append(extracted)
                elif part_type is not None:
                    texts.append(f"[Unsupported {part_type}]")
        return "\n".join(texts)
    return str(content)


def preprocess_messages_for_standard(messages: list[dict]) -> list[dict]:
    """Flatten multipart ``content`` to strings for jinja chat templates.

    Many model templates (e.g. Qwen) treat non-string ``content`` as empty, which
    would under-count tokens for OpenAI multipart requests. vLLM prefill flattens
    these parts first via ``parse_chat_messages(..., content_format="string")``.
    """

    processed_messages, _ = _preprocess_items(
        messages=messages,
        tools=None,
        message_processors=[_flatten_message_content_to_string],
        tool_processors=[],
    )
    return processed_messages


def preprocess_messages_for_dsv4(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> tuple[list[dict], list[dict] | None]:
    """Normalize messages/tools for DeepSeek V4 tokenization.

    Applies the same argument coercion as ``preprocess_input`` and flattens
    multipart ``content`` lists to strings before vLLM's ``encode_messages``.
    """

    return _preprocess_items(
        messages=messages,
        tools=tools,
        message_processors=[exchange_arguments, _flatten_message_content_to_string],
        tool_processors=[exchange_tools],
    )


def preprocess_input(messages: list[dict], tools: list[dict] | None = None) -> tuple[list[dict], list[dict] | None]:
    """
    Preprocessing Input Messages and Tools Listed in the Table Below.

    Args:
        messages: message list
        tools: (Optional) Tool List

    Returns:
        tuple: (List of processed messages, List of processed tools)
    """
    return _preprocess_items(
        messages=messages,
        tools=tools,
        message_processors=[exchange_arguments, exchange_tool_content],
        tool_processors=[exchange_tools],
    )


def _flatten_message_content_to_string(message: dict) -> None:
    if OpenAIField.CONTENT not in message:
        return
    message[OpenAIField.CONTENT] = content_parts_to_string(message[OpenAIField.CONTENT])


def _preprocess_items(
    messages: list[dict],
    tools: list[dict] | None,
    message_processors: list,
    tool_processors: list,
) -> tuple[list[dict], list[dict] | None]:
    processed_messages = copy.deepcopy(messages)
    for message in processed_messages:
        for processor in message_processors:
            processor(message)

    processed_tools = None
    if tools:
        processed_tools = copy.deepcopy(tools)
        for tool in processed_tools:
            for processor in tool_processors:
                processor(tool)

    return processed_messages, processed_tools


def exchange_arguments(message: dict) -> None:
    """
    Converts the tool call arguments in the message from a string to a JSON object.

    Args:
        message: Message dictionary containing tool invoking information.

    Returns:
        None: The message dictionary is modified in place.
    """
    if OpenAIField.TOOLS_CALLS not in message:
        return
    for tool in message[OpenAIField.TOOLS_CALLS]:
        if OpenAIField.FUNCTION not in tool:
            continue
        if isinstance(tool[OpenAIField.FUNCTION][OpenAIField.ARGUMENTS], str):
            tool[OpenAIField.FUNCTION][OpenAIField.ARGUMENTS] = json.loads(
                tool[OpenAIField.FUNCTION][OpenAIField.ARGUMENTS]
            )


def exchange_tool_content(message: dict) -> None:
    """
    Message content format of the conversion tool.

    Args:
        message: Dictionary containing the message content.

    Returns:
        None: The input message dictionary is directly modified.
    """
    if OpenAIField.ROLE not in message:
        return
    if message[OpenAIField.ROLE] != "tool":
        return
    if OpenAIField.CONTENT not in message:
        return
    content = message[OpenAIField.CONTENT]
    if isinstance(content, str):
        exchange_content = {"type": "text", "text": content}
        message[OpenAIField.CONTENT] = f"{exchange_content}"


def exchange_tools(tool: dict) -> None:
    """
    Sort the fields of the tool function to ensure the fields are arranged according to the specified priority.

    Args:
        tool: a dictionary containing tool information

    Returns:
        None: The passed tool dictionary is modified directly
    """
    if OpenAIField.FUNCTION not in tool:
        return

    max_seq = 100
    priority = {"name": 1, "description": 2, "parameters": 3}
    tool[OpenAIField.FUNCTION] = dict(
        sorted(tool[OpenAIField.FUNCTION].items(), key=lambda x: priority.get(x[0], max_seq))
    )
