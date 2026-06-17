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

"""Adapt vLLM OpenAI Completion responses to Chat Completion shape for client contract."""

from __future__ import annotations

from typing import Any

from motor.coordinator.models.constants import OpenAIField


def _lift_prompt_token_ids_from_choice(choice: dict[str, Any], response: dict[str, Any]) -> None:
    """Move ``prompt_token_ids`` from a completion choice to the top-level response if absent."""
    pti = choice.pop(OpenAIField.PROMPT_TOKEN_IDS, None)
    if pti is not None and response.get(OpenAIField.PROMPT_TOKEN_IDS) is None:
        response[OpenAIField.PROMPT_TOKEN_IDS] = pti


def _chat_completion_id(req_id: str) -> str:
    base = req_id.replace("cmpl-", "").replace("chatcmpl-", "")
    return f"chatcmpl-{base}"


def is_completion_like_stream_chunk(chunk_json: dict[str, Any]) -> bool:
    """True if chunk looks like a text_completion stream object (not chat chunk)."""
    if chunk_json.get("object") == "text_completion":
        return True
    choices = chunk_json.get(OpenAIField.CHOICES) or []
    if not choices:
        return False
    c0 = choices[0]
    if c0.get(OpenAIField.DELTA):
        return False
    return OpenAIField.TEXT in c0


def _map_completion_logprobs_to_chat_content(c0: dict[str, Any]) -> None:
    """Rewrite ``choices[0].logprobs`` (Completion shape) into Chat ``content[]`` shape.

    Completion ``logprobs`` carries ``token_logprobs: list[float|None]`` and
    optionally ``top_logprobs: list[dict[str, float]]``; Chat carries
    ``content: list[{token, logprob, top_logprobs}]``. This rewrites the
    Completion object into the Chat shape so ``update_logprob_cache``'s Chat
    path can pick it up. The mapping drops a null entry but keeps the rest.
    """
    lp = c0.get("logprobs")
    if not isinstance(lp, dict):
        return
    token_logprobs = lp.get("token_logprobs") or []
    top_logprobs = lp.get("top_logprobs") or []
    if not token_logprobs:
        return

    content: list[dict[str, Any]] = []
    for i, float_lp in enumerate(token_logprobs):
        if float_lp is None:
            continue
        entry: dict[str, Any] = {"logprob": float(float_lp)}
        if i < len(top_logprobs) and isinstance(top_logprobs[i], dict):
            sub: list[dict[str, Any]] = []
            for k, v in top_logprobs[i].items():
                if v is None:
                    continue
                sub.append({"token": str(k), "logprob": float(v)})
            if sub:
                entry["top_logprobs"] = sub
        content.append(entry)

    if content:
        c0["logprobs"] = {"content": content}
    else:
        c0.pop("logprobs", None)


def adapt_completion_stream_chunk_to_chat(
    chunk_json: dict[str, Any],
    *,
    req_id: str,
    stream_state: dict[str, Any],
) -> None:
    """Mutate ``chunk_json`` in place: Completion stream chunk → ``chat.completion.chunk``."""
    chunk_json["object"] = "chat.completion.chunk"
    chunk_json["id"] = _chat_completion_id(req_id)

    choices = chunk_json.get(OpenAIField.CHOICES) or []
    if not choices:
        return
    c0 = choices[0]
    idx = c0.get("index", 0)
    text = c0.pop(OpenAIField.TEXT, None) or ""
    finish_reason = c0.pop("finish_reason", None)
    stop_reason = c0.pop("stop_reason", None)
    # Map Completion logprobs into Chat content[] so precision-sampling can
    # still cache topk (and so the client still sees structured logprobs).
    _map_completion_logprobs_to_chat_content(c0)
    remapped_logprobs = c0.pop("logprobs", None)

    _lift_prompt_token_ids_from_choice(c0, chunk_json)

    delta: dict[str, Any] = {}
    if not stream_state.get("stream_role_sent"):
        delta["role"] = "assistant"
        stream_state["stream_role_sent"] = True
    if text:
        delta["content"] = text

    c0.clear()
    c0["index"] = idx
    c0[OpenAIField.DELTA] = delta
    if finish_reason is not None:
        c0["finish_reason"] = finish_reason
    if stop_reason is not None:
        c0["stop_reason"] = stop_reason
    if remapped_logprobs is not None:
        c0["logprobs"] = remapped_logprobs


def adapt_completion_nonstream_to_chat(body: dict[str, Any], *, req_id: str) -> None:
    """Mutate ``body`` in place: ``text_completion`` → ``chat.completion``."""
    body["object"] = "chat.completion"
    body["id"] = _chat_completion_id(req_id)

    choices = body.get(OpenAIField.CHOICES) or []
    if not choices:
        return
    c0 = choices[0]
    text = c0.pop(OpenAIField.TEXT, None) or ""
    finish_reason = c0.pop("finish_reason", None)
    stop_reason = c0.pop("stop_reason", None)
    token_ids = c0.pop(OpenAIField.TOKEN_IDS, None)
    _map_completion_logprobs_to_chat_content(c0)
    remapped_logprobs = c0.pop("logprobs", None)
    c0.pop("prompt_logprobs", None)

    _lift_prompt_token_ids_from_choice(c0, body)

    c0.clear()
    c0["index"] = 0
    c0[OpenAIField.MESSAGE] = {
        OpenAIField.ROLE: "assistant",
        OpenAIField.CONTENT: text,
    }
    if finish_reason is not None:
        c0["finish_reason"] = finish_reason
    if stop_reason is not None:
        c0["stop_reason"] = stop_reason
    if token_ids is not None:
        c0[OpenAIField.TOKEN_IDS] = token_ids
    if remapped_logprobs is not None:
        c0["logprobs"] = remapped_logprobs
