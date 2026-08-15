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

PROMPT_TOKEN_IDS = OpenAIField.PROMPT_TOKEN_IDS.value
TOKEN_IDS = OpenAIField.TOKEN_IDS.value
CHOICES = OpenAIField.CHOICES.value
USAGE = OpenAIField.USAGE.value

# Byte-level field probes are derived from the OpenAIField enum so the fast-path
# scan can never silently drift out of sync with the keys the strip logic pops
# (a mismatch would leak token ids). ``stop_reason``/``logprobs`` have no enum
# counterpart and stay as literals.
_JSON_FIELD_TOKEN_IDS = f'"{TOKEN_IDS}"'.encode()
_JSON_FIELD_PROMPT_TOKEN_IDS = f'"{PROMPT_TOKEN_IDS}"'.encode()
_JSON_FIELD_STOP_REASON = b'"stop_reason"'
_JSON_FIELD_LOGPROBS = b'"logprobs"'
_JSON_FIELD_USAGE = f'"{USAGE}"'.encode()
_RECOMPUTED_VALUE = b'"recomputed"'
_JSON_WHITESPACE = b" \t\r\n"


def chunk_has_recomputed_stop_reason(chunk: bytes) -> bool:
    return _contains_json_field_value(chunk, _JSON_FIELD_STOP_REASON, _RECOMPUTED_VALUE)


def stream_chunk_needs_sampling_parse(chunk: bytes) -> bool:
    """Return True when a chunk may carry precision-sampling fields worth parsing."""
    return (
        _contains_json_field(chunk, _JSON_FIELD_TOKEN_IDS)
        or _contains_json_field(chunk, _JSON_FIELD_PROMPT_TOKEN_IDS)
        or _contains_json_field(chunk, _JSON_FIELD_LOGPROBS)
    )


def chunk_has_usage_field(chunk: bytes) -> bool:
    return _contains_json_field(chunk, _JSON_FIELD_USAGE)


def _compact_json_bytes(obj: Any) -> bytes:
    """Serialize ``obj`` to compact UTF-8 JSON bytes (hot path: SSE chunk re-encode).

    Prefer :func:`msgspec.json.encode` over :func:`json.dumps` for lower CPU;
    fall back if the value is not encodable (exotic types).
    """
    try:
        return msgspec.json.encode(obj)
    except Exception:
        return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def _stream_json_payload_bytes(chunk: bytes) -> bytes | None:
    """Return the JSON object bytes inside one SSE/data line, or None when empty."""
    payload = chunk.strip()
    if not payload:
        return None
    if payload.startswith(b"data: "):
        payload = payload[6:]
    elif payload.startswith(b"data:"):
        payload = payload[5:].lstrip()
    payload = payload.strip()
    return payload or None


def _contains_json_field(chunk: bytes, field: bytes) -> bool:
    """Return whether ``field`` occurs as a JSON object key, not as text content."""
    offset = 0
    while True:
        index = chunk.find(field, offset)
        if index < 0:
            return False
        cursor = index + len(field)
        while cursor < len(chunk) and chunk[cursor] in _JSON_WHITESPACE:
            cursor += 1
        if cursor < len(chunk) and chunk[cursor] == ord(":"):
            return True
        offset = index + 1


def _contains_json_field_value(chunk: bytes, field: bytes, value: bytes) -> bool:
    """Return whether a JSON object key has the requested literal string value."""
    offset = 0
    while True:
        index = chunk.find(field, offset)
        if index < 0:
            return False
        cursor = index + len(field)
        while cursor < len(chunk) and chunk[cursor] in _JSON_WHITESPACE:
            cursor += 1
        if cursor < len(chunk) and chunk[cursor] == ord(":"):
            cursor += 1
            while cursor < len(chunk) and chunk[cursor] in _JSON_WHITESPACE:
                cursor += 1
            if chunk.startswith(value, cursor):
                return True
        offset = index + 1


def is_done_stream_chunk(chunk: bytes) -> bool:
    """Return True only for the SSE ``data: [DONE]`` sentinel."""
    return _stream_json_payload_bytes(chunk) == b"[DONE]"


def parse_stream_chunk_json(chunk: bytes, logger: Any | None = None) -> dict | None:
    """Parse one SSE/data line to JSON; return None if not JSON object."""
    payload = _stream_json_payload_bytes(chunk)
    if payload is None:
        return None

    try:
        parsed = msgspec.json.decode(payload)
    except msgspec.DecodeError:
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if logger is not None:
                logger.debug("Skipping chunk payload: %s", payload[:200])
            return None
    return parsed if isinstance(parsed, dict) else None


def stream_chunk_needs_client_strip_parse(
    chunk: bytes,
    *,
    client_return_token_ids: bool = False,
) -> bool:
    """Return True when a chunk may need token-id stripping or stop_reason normalization."""
    if is_done_stream_chunk(chunk):
        return False
    if client_return_token_ids:
        return chunk_has_recomputed_stop_reason(chunk)
    if _contains_json_field(chunk, _JSON_FIELD_PROMPT_TOKEN_IDS) or _contains_json_field(chunk, _JSON_FIELD_TOKEN_IDS):
        return True
    return chunk_has_recomputed_stop_reason(chunk)


def _drop_invalid_stream_chunk(chunk: bytes, logger: Any | None = None) -> bytes:
    """Drop a non-[DONE] chunk whose JSON payload failed to parse."""
    if is_done_stream_chunk(chunk):
        return chunk
    if logger is not None:
        logger.warning(
            "Dropping invalid stream chunk after client normalization (size=%d)",
            len(chunk),
        )
    return b""


def strip_openai_token_id_fields_for_client(
    obj: dict,
    *,
    client_return_token_ids: bool = False,
) -> bool:
    """Remove ``return_token_ids``-related fields before JSON is sent to the client (mutates ``obj``).

    When ``client_return_token_ids`` is ``True`` the token-id fields are kept
    (the client explicitly asked for them); only ``stop_reason`` normalisation
    is still applied unconditionally.

    Returns True when any client-visible field was changed.
    """
    mutated = False
    if not client_return_token_ids:
        if PROMPT_TOKEN_IDS in obj:
            obj.pop(PROMPT_TOKEN_IDS)
            mutated = True
    for ch in obj.get(CHOICES) or []:
        if isinstance(ch, dict):
            if not client_return_token_ids:
                if TOKEN_IDS in ch:
                    ch.pop(TOKEN_IDS)
                    mutated = True
                if PROMPT_TOKEN_IDS in ch:
                    ch.pop(PROMPT_TOKEN_IDS)
                    mutated = True
            if ch.get("stop_reason") == "recomputed":
                ch["stop_reason"] = "stop"
                mutated = True
    return mutated


def merge_prompt_tokens_details_into_usage(
    chunk_json: dict,
    prompt_tokens_details: dict,
) -> bool:
    """Merge cached prefill ``prompt_tokens_details`` into a usage block when needed."""
    if not prompt_tokens_details:
        return False
    usage = chunk_json.get(USAGE)
    if not isinstance(usage, dict) or not usage:
        return False
    if usage.get("prompt_tokens_details") == prompt_tokens_details:
        return False
    usage["prompt_tokens_details"] = prompt_tokens_details
    return True


def encode_stream_chunk_bytes(original_chunk: bytes, chunk_json: dict) -> bytes:
    """Re-serialize one SSE ``data:`` line or a raw JSON line after in-place edits to ``chunk_json``."""
    payload = _compact_json_bytes(chunk_json)
    stripped = original_chunk.lstrip()
    if stripped.startswith(b"data: "):
        line_b = b"data: " + payload
    elif stripped.startswith(b"data:"):
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
    pti = chunk_json.get(PROMPT_TOKEN_IDS)
    if pti is None:
        choices = chunk_json.get(CHOICES) or []
        if choices and isinstance(choices[0], dict):
            pti = choices[0].get(PROMPT_TOKEN_IDS)
    if isinstance(pti, (list, tuple)) and request_info.get("cached_prompt_token_ids") is None:
        request_info["cached_prompt_token_ids"] = list(pti)

    choices = chunk_json.get(CHOICES) or []
    if not choices:
        return
    c0 = choices[0]
    token_ids = c0.get(TOKEN_IDS)
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
    logger: Any | None = None,
) -> bytes:
    """Strip token id fields from one stream chunk (SSE or raw JSON line)."""
    if not stream_chunk_needs_client_strip_parse(chunk, client_return_token_ids=client_return_token_ids):
        # No fields to strip on the default path: trust upstream engine bytes (zero parse).
        return chunk

    chunk_json = parse_stream_chunk_json(chunk, logger=logger)
    if chunk_json is None:
        return _drop_invalid_stream_chunk(chunk, logger=logger)
    if not strip_openai_token_id_fields_for_client(
        chunk_json,
        client_return_token_ids=client_return_token_ids,
    ):
        return chunk
    return encode_stream_chunk_bytes(chunk, chunk_json)


def strip_nonstream_response_body_for_client(
    body: dict,
    *,
    client_return_token_ids: bool = False,
) -> None:
    """Strip token id fields from a non-streaming OpenAI-style JSON body (mutates ``body``)."""
    strip_openai_token_id_fields_for_client(body, client_return_token_ids=client_return_token_ids)
