# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Client-facing OpenAI response normalization for dispatch adapters."""

from __future__ import annotations

import json
from typing import Any

import msgspec


CHOICES = "choices"
CONTENT = "content"
DELTA = "delta"
MESSAGE = "message"
PROMPT_TOKEN_IDS = "prompt_token_ids"  # nosec B105 -- OpenAI response JSON field name
ROLE = "role"
TEXT = "text"
TOKEN_IDS = "token_ids"  # nosec B105 -- OpenAI response JSON field name
KV_TRANSFER_PARAMS = "kv_transfer_params"


def normalize_nonstream_body(
    body: dict[str, Any],
    *,
    client_expects_chat_shape: bool = False,
    req_id: str = "",
    client_return_token_ids: bool = False,
) -> None:
    if client_expects_chat_shape and _is_completion_like_body(body):
        _adapt_completion_nonstream_to_chat(body, req_id=req_id)
    _strip_token_id_fields(body, client_return_token_ids=client_return_token_ids)


def strip_engine_dispatch_fields(body: dict[str, Any]) -> None:
    """Remove engine-native dispatch fields before retrying or exposing a body."""
    body.pop(KV_TRANSFER_PARAMS, None)
    for key in list(body):
        if key.startswith("bootstrap_"):
            body.pop(key, None)


def normalize_stream_chunk(
    chunk: bytes | str,
    *,
    client_expects_chat_shape: bool = False,
    req_id: str = "",
    stream_state: dict[str, Any] | None = None,
    client_return_token_ids: bool = False,
) -> bytes | str | None:
    chunk_bytes = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
    chunk_json = _parse_stream_chunk_json(chunk_bytes)
    if chunk_json is None:
        text = chunk_bytes.decode("utf-8", errors="replace").strip()
        if "[DONE]" in text:
            return chunk
        return b"" if isinstance(chunk, bytes) else ""
    if client_expects_chat_shape and _is_completion_like_stream_chunk(chunk_json):
        _adapt_completion_stream_chunk_to_chat(
            chunk_json,
            req_id=req_id,
            stream_state=stream_state if stream_state is not None else {},
        )
    _strip_token_id_fields(chunk_json, client_return_token_ids=client_return_token_ids)
    out = _encode_stream_chunk_bytes(chunk_bytes, chunk_json)
    return out if isinstance(chunk, bytes) else out.decode("utf-8")


def _strip_token_id_fields(
    obj: dict[str, Any],
    *,
    client_return_token_ids: bool = False,
) -> None:
    if not client_return_token_ids:
        obj.pop(PROMPT_TOKEN_IDS, None)
    for choice in obj.get(CHOICES) or []:
        if not isinstance(choice, dict):
            continue
        if not client_return_token_ids:
            choice.pop(TOKEN_IDS, None)
            choice.pop(PROMPT_TOKEN_IDS, None)
        if choice.get("stop_reason") == "recomputed":
            choice["stop_reason"] = "stop"


def _parse_stream_chunk_json(chunk: bytes) -> dict[str, Any] | None:
    try:
        chunk_str = chunk.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not chunk_str:
        return None
    if chunk_str.startswith("data: "):
        chunk_str = chunk_str[len("data: ") :]
    try:
        parsed = json.loads(chunk_str)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _encode_stream_chunk_bytes(original_chunk: bytes, chunk_json: dict[str, Any]) -> bytes:
    raw = original_chunk.decode("utf-8", errors="replace").strip()
    payload = _compact_json_bytes(chunk_json)
    line = b"data: " + payload if raw.startswith("data: ") else payload
    if original_chunk.endswith(b"\r\n\r\n"):
        suffix = b"\r\n\r\n"
    elif original_chunk.endswith(b"\n\n"):
        suffix = b"\n\n"
    elif original_chunk.endswith(b"\r\n"):
        suffix = b"\r\n"
    elif original_chunk.endswith(b"\n"):
        suffix = b"\n"
    else:
        suffix = b""
    return line + suffix


def _compact_json_bytes(obj: Any) -> bytes:
    try:
        return msgspec.json.encode(obj)
    except Exception:
        return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def _is_completion_like_body(body: dict[str, Any]) -> bool:
    if body.get("object") == "text_completion":
        return True
    choices = body.get(CHOICES) or []
    return bool(choices and isinstance(choices[0], dict) and TEXT in choices[0])


def _is_completion_like_stream_chunk(chunk_json: dict[str, Any]) -> bool:
    if chunk_json.get("object") == "text_completion":
        return True
    choices = chunk_json.get(CHOICES) or []
    if not choices or not isinstance(choices[0], dict):
        return False
    choice = choices[0]
    if choice.get(DELTA):
        return False
    return TEXT in choice


def _adapt_completion_nonstream_to_chat(body: dict[str, Any], *, req_id: str) -> None:
    body["object"] = "chat.completion"
    body["id"] = _chat_completion_id(req_id)
    choices = body.get(CHOICES) or []
    if not choices or not isinstance(choices[0], dict):
        return
    choice = choices[0]
    text = choice.pop(TEXT, None) or ""
    finish_reason = choice.pop("finish_reason", None)
    stop_reason = choice.pop("stop_reason", None)
    token_ids = choice.pop(TOKEN_IDS, None)
    choice.pop("logprobs", None)
    choice.pop("prompt_logprobs", None)
    _lift_prompt_token_ids(choice, body)
    choice.clear()
    choice["index"] = 0
    choice[MESSAGE] = {ROLE: "assistant", CONTENT: text}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    if stop_reason is not None:
        choice["stop_reason"] = stop_reason
    if token_ids is not None:
        choice[TOKEN_IDS] = token_ids


def _adapt_completion_stream_chunk_to_chat(
    chunk_json: dict[str, Any],
    *,
    req_id: str,
    stream_state: dict[str, Any],
) -> None:
    chunk_json["object"] = "chat.completion.chunk"
    chunk_json["id"] = _chat_completion_id(req_id)
    choices = chunk_json.get(CHOICES) or []
    if not choices or not isinstance(choices[0], dict):
        return
    choice = choices[0]
    index = choice.get("index", 0)
    text = choice.pop(TEXT, None) or ""
    finish_reason = choice.pop("finish_reason", None)
    stop_reason = choice.pop("stop_reason", None)
    choice.pop("logprobs", None)
    _lift_prompt_token_ids(choice, chunk_json)
    delta: dict[str, Any] = {}
    if not stream_state.get("stream_role_sent"):
        delta[ROLE] = "assistant"
        stream_state["stream_role_sent"] = True
    if text:
        delta[CONTENT] = text
    choice.clear()
    choice["index"] = index
    choice[DELTA] = delta
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    if stop_reason is not None:
        choice["stop_reason"] = stop_reason


def _lift_prompt_token_ids(choice: dict[str, Any], response: dict[str, Any]) -> None:
    prompt_token_ids = choice.pop(PROMPT_TOKEN_IDS, None)
    if prompt_token_ids is not None and response.get(PROMPT_TOKEN_IDS) is None:
        response[PROMPT_TOKEN_IDS] = prompt_token_ids


def _chat_completion_id(req_id: str) -> str:
    base = req_id.replace("cmpl-", "").replace("chatcmpl-", "")
    return f"chatcmpl-{base}"
