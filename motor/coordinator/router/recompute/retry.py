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

"""Recompute retry: request rewrite, request-id management, and response validation."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status

from motor.coordinator.models.constants import OpenAIField

from .common import (
    _CHAT_ONLY_KEYS_RECOMPUTE,
    _COORDINATOR_ONLY_REQ_KEYS,
    _CUMULATIVE_COMPLETION_TOKENS_KEY,
    _recompute_log,
    extract_content_from_choice,
    extract_request_info,
)
from .stream import fill_recompute_kv_from_token_cache, update_token_id_cache

# Offset for a 2-digit retry counter embedded in coordinator-generated request IDs.
_REQ_ID_RETRY_INDEX = 16
_REQ_ID_RETRY_LEN = 2


def modify_req_id_retry_segment(req_id: str) -> str:
    """Bump a 2-digit segment in ``req_id`` at a fixed offset when digits are present."""
    if len(req_id) > _REQ_ID_RETRY_INDEX:
        target = req_id[_REQ_ID_RETRY_INDEX : _REQ_ID_RETRY_INDEX + _REQ_ID_RETRY_LEN]
        if target.isdigit():
            new_digit = (int(target) + 1) % 100
            return req_id[:_REQ_ID_RETRY_INDEX] + f"{new_digit:02d}" + req_id[_REQ_ID_RETRY_INDEX + _REQ_ID_RETRY_LEN :]
        return req_id
    return req_id


def bump_req_id_after_recompute_prepare(
    req_info: Any,
    *,
    retry_count: int,
    logger: Any,
) -> None:
    """Rotate ``req_info.req_id`` retry segment and log (call after ``prepare_retry_request``)."""
    original_req_id = req_info.req_id
    req_info.req_id = modify_req_id_retry_segment(original_req_id)
    logger.info(
        "Recomputing old req_id %s, new req_id %s, retry count: %d, new req_info: %s",
        original_req_id,
        req_info.req_id,
        retry_count,
        req_info,
    )


def copy_req_data_for_engine(req_data: dict) -> dict:
    """Shallow copy omitting coordinator-only keys that must not appear in engine JSON."""
    if not _COORDINATOR_ONLY_REQ_KEYS.intersection(req_data):
        return req_data
    return {k: v for k, v in req_data.items() if k not in _COORDINATOR_ONLY_REQ_KEYS}


def update_nonstream_retry_choice(
    choice: dict,
    request_info: dict,
    total_generated_token: str,
    *,
    client_expects_chat_shape: bool = False,
) -> None:
    """Rewrite choice content with accumulated text for non-stream retry responses."""
    if request_info["chat_flag"] or client_expects_chat_shape:
        msg = choice.get(OpenAIField.MESSAGE)
        if isinstance(msg, dict):
            msg[OpenAIField.CONTENT] = total_generated_token
    else:
        choice[OpenAIField.TEXT] = total_generated_token


def is_recomputed_nonstream_response(resp_json: dict) -> bool:
    choices = resp_json.get(OpenAIField.CHOICES) or []
    return bool(choices and choices[0].get("stop_reason") == "recomputed")


def validate_nonstream_recompute_body(
    req_id: str,
    resp_json: dict,
    *,
    logger: Any | None = None,
) -> None:
    """Validate non-stream recompute response carries token-id cache fields.

    Prompt token ids are always required. If ``usage.completion_tokens > 0``,
    ``choices[0].token_ids`` must be a non-empty list so the retry prompt matches
    generated output (no silent ``usage``-only fallback in ``prepare_retry_request``).
    """
    log = logger if logger is not None else _recompute_log
    choices = resp_json.get(OpenAIField.CHOICES) or []
    if not choices or not isinstance(choices[0], dict):
        log.error(
            "Recompute non-stream response malformed for request %s: missing choices[0]",
            req_id,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="Recompute non-stream response malformed: missing choices[0].",
        )

    prompt_ids = resp_json.get(OpenAIField.PROMPT_TOKEN_IDS)
    if prompt_ids is None:
        prompt_ids = choices[0].get(OpenAIField.PROMPT_TOKEN_IDS)
    if not isinstance(prompt_ids, list) or not prompt_ids:
        log.error(
            "Recompute aborted for request %s: non-stream response missing prompt_token_ids",
            req_id,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=("Recompute requires non-stream prompt_token_ids; ensure decode returns return_token_ids."),
        )

    token_ids = choices[0].get(OpenAIField.TOKEN_IDS)
    if token_ids is not None and not isinstance(token_ids, list):
        log.error(
            "Recompute aborted for request %s: non-stream choices[0].token_ids is not a list",
            req_id,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="Recompute token cache corrupted: non-stream choices[0].token_ids is not a list.",
        )

    usage = resp_json.get(OpenAIField.USAGE) if isinstance(resp_json.get(OpenAIField.USAGE), dict) else {}
    raw_ct = usage.get(OpenAIField.COMPLETION_TOKENS, 0)
    try:
        usage_ct = int(raw_ct) if raw_ct is not None else 0
    except (TypeError, ValueError):
        usage_ct = 0
    if usage_ct > 0:
        if not isinstance(token_ids, list) or len(token_ids) == 0:
            log.error(
                "Recompute aborted for request %s: non-stream output token_ids missing but usage.completion_tokens=%s",
                req_id,
                usage_ct,
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=("Recompute requires non-stream choices[0].token_ids when usage.completion_tokens > 0."),
            )


def completions_retry_eligible_for_chat_request(req_data: dict) -> bool:
    """Whether Chat ingress may use Completions (``prompt: all_ids``) for recompute.

    If ``False``, :func:`prepare_retry_request` raises **502** — token-id replay on the
    Chat API is unsupported by vLLM (``messages[].content`` must not be a raw id list).
    """
    rf = req_data.get("response_format")
    if rf is not None:
        if isinstance(rf, dict):
            rtype = rf.get("type")
            if rtype is not None and rtype != "text":
                return False
        else:
            return False

    if req_data.get("tools"):
        return False
    if req_data.get("logprobs"):
        return False
    if req_data.get("top_logprobs"):
        return False
    messages = req_data.get(OpenAIField.MESSAGES) or []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype and ptype != "text":
                return False
    return True


def build_request_info_for_nonstream_recompute(req_data: dict, resp_json: dict) -> dict:
    """request_info for ``prepare_retry_request`` after a non-stream recomputed body."""
    info = extract_request_info(req_data)
    update_token_id_cache(info, resp_json)
    choices = resp_json.get(OpenAIField.CHOICES) or []
    if not choices:
        fill_recompute_kv_from_token_cache(info)
        return info
    c0 = choices[0]
    info["generated_token"] = extract_content_from_choice(c0)
    usage = resp_json.get(OpenAIField.USAGE, {})
    info[OpenAIField.COMPLETION_TOKENS] = usage.get(OpenAIField.COMPLETION_TOKENS, 0)
    fill_recompute_kv_from_token_cache(info)
    return info


def prepare_retry_request(
    req_data: dict,
    request_info: dict,
    *,
    new_retry_count: int,
    req_id: str,
    logger: Any,
    req_info=None,
) -> None:
    """Apply P/D retry body and ``max_tokens`` after recompute.

    Chat ingress: only **Completions-style** retry (``prompt: list[int]``, ``api`` →
    ``v1/completions``) is supported. If ``completions_retry_eligible_for_chat_request``
    is false (e.g. ``tools``, ``logprobs``, multimodal parts), raises **502** — vLLM Chat
    does not accept token-id ``content`` for replay.

    ``max_tokens`` uses a **session-wide** completion count in
    ``req_data[_cumulative_completion_tokens]``: each recompute adds this leg's
    ``len(all_token_ids) - len(prompt_token_ids)`` so the budget stays aligned with
    the client's original ``_origin_max_tokens`` even when per-chunk
    ``prompt_token_ids`` boundaries shift across rounds.

    Args:
        new_retry_count: Already incremented (1-based recompute index); used for logging only.
        req_info: Optional object with ``req_len`` and ``req_data`` to sync (Coordinator RequestInfo).
    """
    kv = request_info.pop("recompute_kv_transfer", None) or {}
    all_token_ids = kv.get("all_token_ids")
    prompt_token_ids = kv.get(OpenAIField.PROMPT_TOKEN_IDS)

    use_token_retry = (
        all_token_ids is not None
        and prompt_token_ids is not None
        and isinstance(all_token_ids, (list, tuple))
        and isinstance(prompt_token_ids, (list, tuple))
    )

    if not use_token_retry:
        logger.error(
            "Recompute for request %s missing all_token_ids/prompt_token_ids in recompute_kv_transfer",
            req_id,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=("Recompute requires token id cache (all_token_ids and prompt_token_ids)"),
        )

    try:
        n_val = int(req_data.get("n", 1))
    except (TypeError, ValueError):
        logger.error("Recompute aborted for request %s: invalid n", req_id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="Recompute aborted: invalid n parameter.",
        ) from None
    if n_val != 1:
        logger.error(
            "Recompute aborted for request %s: parallel sampling n=%s not supported",
            req_id,
            n_val,
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="Recompute does not support parallel sampling (n>1).",
        )

    all_ids = list(all_token_ids)
    prompt_ids = list(prompt_token_ids)
    origin_cap = req_data.setdefault("_origin_max_tokens", request_info["origin_max_tokens"])
    leg_completion = len(all_ids) - len(prompt_ids)
    try:
        prev_cumulative = int(req_data.get(_CUMULATIVE_COMPLETION_TOKENS_KEY, 0))
    except (TypeError, ValueError):
        prev_cumulative = 0
    cumulative_completion = prev_cumulative + leg_completion
    req_data[_CUMULATIVE_COMPLETION_TOKENS_KEY] = cumulative_completion
    raw_max_tokens = origin_cap - cumulative_completion + 1
    if raw_max_tokens < 1:
        logger.warning(
            "Recompute max_tokens budget non-positive (clamped to 1): req_id=%s "
            "origin_max_tokens=%s cumulative_completion_tokens=%s (leg=%s)",
            req_id,
            origin_cap,
            cumulative_completion,
            leg_completion,
        )
    req_data[OpenAIField.MAX_TOKENS] = max(1, raw_max_tokens)
    if request_info["chat_flag"]:
        if not completions_retry_eligible_for_chat_request(req_data):
            logger.error(
                "Recompute aborted for request %s: Chat ingress ineligible for Completions-style token-id replay.",
                req_id,
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "Recompute is not supported for this Chat request (tools, logprobs, "
                    "top_logprobs, non-text multimodal content, structured "
                    "response_format, or n>1). vLLM does not accept token-id arrays in "
                    "Chat messages."
                ),
            )
        for k in _CHAT_ONLY_KEYS_RECOMPUTE:
            req_data.pop(k, None)
        req_data[OpenAIField.PROMPT] = all_ids
        if req_info is not None:
            req_info.api = "v1/completions"
            req_info.recompute_engine_mode = "completions"
        logger.info(
            "Recomputing request %s (completions engine): retry=%d all_len=%d prompt_len=%d "
            "cumulative_completion=%d max_tokens=%s",
            req_id,
            new_retry_count,
            len(all_ids),
            len(prompt_ids),
            cumulative_completion,
            req_data.get(OpenAIField.MAX_TOKENS),
        )
    else:
        req_data[OpenAIField.PROMPT] = all_ids
        if req_info is not None:
            req_info.recompute_engine_mode = None
        logger.info(
            "Recomputing request %s (token-id completion retry): retry=%d all_len=%d prompt_len=%d "
            "cumulative_completion=%d max_tokens=%s",
            req_id,
            new_retry_count,
            len(all_ids),
            len(prompt_ids),
            cumulative_completion,
            req_data.get(OpenAIField.MAX_TOKENS),
        )

    if req_info is not None:
        engine_view = copy_req_data_for_engine(req_data)
        req_info.req_len = len(json.dumps(engine_view).encode("utf-8"))
        req_info.req_data = req_data


def recompute_limit_reached(current_retry_count: int, max_retry: int) -> bool:
    """True if another recompute must not be started."""
    return current_retry_count >= max_retry
