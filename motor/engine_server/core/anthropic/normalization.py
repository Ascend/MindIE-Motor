# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
#
# MindIE is licensed under both the Mulan PSL v2 and the Apache License, Version 2.0.
# You may choose to use this software under the terms of either license.
#
# ---------------------------------------------------------------------------
# Mulan PSL v2:
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
#
# Apache License, Version 2.0:
# You may obtain a copy of the License at:
#         http://www.apache.org/licenses/LICENSE-2.0
# ---------------------------------------------------------------------------
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the respective licenses for more details.

"""Anthropic protocol response normalization for engine-side Anthropic routes.

The deployed vLLM fork maps engine finish reasons through
``stop_reason_map.get(finish_reason)``; an engine-internal recompute
(``finish_reason="recomputed"``) is unmapped, so the Anthropic response ends
up with a missing/``null`` ``stop_reason``. The Anthropic ``stop_reason`` is a
closed Literal, so the engine server must rewrite the missing/``null`` (or a
raw ``"recomputed"``) value to a spec-compliant one and record the recompute
in the logs, mirroring the OpenAI-path ``recomputed`` -> ``stop`` rewrite.
"""

import json
from typing import Any

from motor.common.logger import get_logger

logger = get_logger(__name__)

RECOMPUTED_STOP_REASON = "recomputed"
DEFAULT_STOP_REASON = "end_turn"
MESSAGE_DELTA_EVENT = "message_delta"
MESSAGE_STOP_EVENT = "message_stop"
ANTHROPIC_MESSAGES_API = "v1/messages"


def is_anthropic_messages_api(api: str) -> bool:
    """Return whether a DispatchResponseContext api path is the Anthropic messages route."""
    return api == ANTHROPIC_MESSAGES_API


def _rewrite_stop_reason(holder: dict[str, Any], *, req_id: str, where: str) -> bool:
    stop_reason = holder.get("stop_reason")
    if stop_reason is not None and stop_reason != RECOMPUTED_STOP_REASON:
        return False
    holder["stop_reason"] = DEFAULT_STOP_REASON
    logger.warning(
        "Engine recompute detected on Anthropic path: %s stop_reason rewritten from %r to %r req_id=%s",
        where,
        stop_reason,
        DEFAULT_STOP_REASON,
        req_id,
    )
    return True


def normalize_anthropic_nonstream_body(body: dict[str, Any], *, req_id: str = "") -> bool:
    """Rewrite a missing/null/``recomputed`` stop_reason on an Anthropic message body.

    Only completed Anthropic message responses (``type == "message"``) are due
    a stop reason; error envelopes and dispatch payloads (e.g. PrefillResult)
    are left untouched. Returns True when a rewrite happened.
    """
    if body.get("type") != "message":
        return False
    return _rewrite_stop_reason(body, req_id=req_id, where="non-stream response")


def normalize_anthropic_stream_chunk(chunk: bytes | str, *, req_id: str = "") -> bytes | str:
    """Rewrite the stop_reason inside ``message_delta`` SSE frames.

    Every other frame (including ``message_start``, whose ``stop_reason`` is
    legitimately ``null``) passes through byte-identically, preserving the
    Anthropic ``event:``/``data:`` framing.
    """
    raw = chunk.decode("utf-8", errors="ignore") if isinstance(chunk, (bytes, bytearray)) else chunk
    lines = raw.split("\n")
    # Fast path: only a genuine ``event: message_delta`` line can mark this
    # frame for rewriting; the same text inside a data payload must not match.
    if not any(line.strip() == f"event: {MESSAGE_DELTA_EVENT}" for line in lines):
        return chunk
    rewritten = False
    for index, line in enumerate(lines):
        if not line.startswith("data: "):
            continue
        try:
            data = json.loads(line[len("data: ") :])
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("type") != MESSAGE_DELTA_EVENT:
            continue
        delta = data.get("delta")
        if not isinstance(delta, dict):
            continue
        if _rewrite_stop_reason(delta, req_id=req_id, where="message_delta frame"):
            lines[index] = "data: " + json.dumps(data, separators=(",", ":"))
            rewritten = True
    if not rewritten:
        return chunk
    out = "\n".join(lines)
    return out.encode("utf-8") if isinstance(chunk, (bytes, bytearray)) else out


def chunk_contains_message_stop(chunk: bytes | str) -> bool:
    """Return whether an SSE chunk terminates the Anthropic stream (``message_stop``)."""
    text = chunk.decode("utf-8", errors="ignore") if isinstance(chunk, (bytes, bytearray)) else chunk
    for line in text.splitlines():
        if line.strip() == f"event: {MESSAGE_STOP_EVENT}":
            return True
    return False
