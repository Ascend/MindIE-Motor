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

"""SSE stream handling, token ID cache, and client-facing response stripping for recompute."""

from __future__ import annotations

import json
from typing import Any

import msgspec

from motor.coordinator.models.constants import OpenAIField


def _compact_json_bytes(obj: Any) -> bytes:
    """Serialize ``obj`` to compact UTF-8 JSON bytes (hot path: SSE chunk re-encode).

    Prefer :func:`msgspec.json.encode` over :func:`json.dumps` for lower CPU;
    fall back if the value is not encodable (exotic types).
    """
    try:
        return msgspec.json.encode(obj)
    except Exception:
        return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def parse_stream_chunk_json(chunk: bytes, logger: Any | None = None) -> dict | None:
    """Parse one SSE/data line to JSON; return None if not JSON object."""
    try:
        chunk_str = chunk.decode("utf-8").strip()
    except UnicodeDecodeError:
        if logger is not None:
            logger.debug("Skipping chunk: %s", chunk)
        return None

    if not chunk_str:
        return None

    if chunk_str.startswith("data: "):
        chunk_str = chunk_str[len("data: ") :]

    try:
        return json.loads(chunk_str)
    except json.JSONDecodeError:
        if logger is not None:
            logger.debug("Skipping chunk str: %s", chunk_str)
        return None


def strip_openai_token_id_fields_for_client(
    obj: dict,
    *,
    client_return_token_ids: bool = False,
) -> None:
    """Remove ``return_token_ids``-related fields before JSON is sent to the client (mutates ``obj``).

    When ``client_return_token_ids`` is ``True`` the token-id fields are kept
    (the client explicitly asked for them); only ``stop_reason`` normalisation
    is still applied unconditionally.
    """
    if not client_return_token_ids:
        obj.pop(OpenAIField.PROMPT_TOKEN_IDS, None)
    for ch in obj.get(OpenAIField.CHOICES) or []:
        if isinstance(ch, dict):
            if not client_return_token_ids:
                ch.pop(OpenAIField.TOKEN_IDS, None)
                ch.pop(OpenAIField.PROMPT_TOKEN_IDS, None)
            if ch.get("stop_reason") == "recomputed":
                ch["stop_reason"] = "stop"


def encode_stream_chunk_bytes(original_chunk: bytes, chunk_json: dict) -> bytes:
    """Re-serialize one SSE ``data:`` line or a raw JSON line after in-place edits to ``chunk_json``."""
    raw = original_chunk.decode("utf-8", errors="replace").strip()
    payload = _compact_json_bytes(chunk_json)
    if raw.startswith("data: "):
        line_b = b"data: " + payload
    else:
        line_b = payload
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
    return line_b + suffix


def update_token_id_cache(request_info: dict, chunk_json: dict) -> None:
    """Accumulate ``return_token_ids`` response fields into ``request_info`` (mutates in place).

    - Root ``prompt_token_ids``: set ``cached_prompt_token_ids`` once (first non-null list).
    - ``choices[0].prompt_token_ids`` (Completion stream): promoted when root is absent.
    - ``choices[0].token_ids``: extend ``cached_output_token_ids`` when a list.
    - ``choices[0].delta.content`` (Chat) or ``choices[0].text`` (Completion):
      accumulate chunks for content-free output structure summarization.
    """
    pti = chunk_json.get(OpenAIField.PROMPT_TOKEN_IDS)
    if pti is None:
        choices = chunk_json.get(OpenAIField.CHOICES) or []
        if choices and isinstance(choices[0], dict):
            pti = choices[0].get(OpenAIField.PROMPT_TOKEN_IDS)
    if isinstance(pti, (list, tuple)) and request_info.get("cached_prompt_token_ids") is None:
        request_info["cached_prompt_token_ids"] = list(pti)

    choices = chunk_json.get(OpenAIField.CHOICES) or []
    if not choices:
        return
    c0 = choices[0]
    token_ids = c0.get(OpenAIField.TOKEN_IDS)
    if isinstance(token_ids, list):
        request_info.setdefault("cached_output_token_ids", []).extend(token_ids)
    # Accumulate output text: Chat API uses delta.content, Completion uses text.
    chunk_text = None
    delta = c0.get(OpenAIField.DELTA)
    if isinstance(delta, dict):
        chunk_text = delta.get(OpenAIField.CONTENT)
    if chunk_text is None:
        chunk_text = c0.get(OpenAIField.TEXT)
    if isinstance(chunk_text, str) and chunk_text:
        request_info.setdefault("cached_output_text_chunks", []).append(chunk_text)


def strip_stream_chunk_bytes_for_client(
    chunk: bytes,
    *,
    client_return_token_ids: bool = False,
) -> bytes:
    """Strip token id fields from one stream chunk (SSE or raw JSON line)."""
    chunk_json = parse_stream_chunk_json(chunk, logger=None)
    if chunk_json is None:
        try:
            text = chunk.decode("utf-8", errors="replace").strip()
        except Exception:
            text = ""
        if "[DONE]" in text:
            return chunk
        return b""
    strip_openai_token_id_fields_for_client(chunk_json, client_return_token_ids=client_return_token_ids)
    return encode_stream_chunk_bytes(chunk, chunk_json)


def strip_nonstream_response_body_for_client(
    body: dict,
    *,
    client_return_token_ids: bool = False,
) -> None:
    """Strip token id fields from a non-streaming OpenAI-style JSON body (mutates ``body``)."""
    strip_openai_token_id_fields_for_client(body, client_return_token_ids=client_return_token_ids)
