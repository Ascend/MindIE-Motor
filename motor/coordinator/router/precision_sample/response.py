# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Extract logprobs from engine responses and strip sampling fields from client output."""

from __future__ import annotations

import re
from typing import Any

from motor.common.logger import get_logger
from motor.coordinator.models.constants import OpenAIField

logger = get_logger(__name__)

_TOKEN_ID_LABEL = re.compile(r"^token_id:(-?\d+)$")


def _parse_logprob_token_id(token: Any) -> int | None:
    """Parse ``"token_id:<int>"`` (vLLM ``return_tokens_as_token_ids``) or return ``None``."""
    if not isinstance(token, str):
        return None
    m = _TOKEN_ID_LABEL.match(token)
    if m is None:
        return None
    return int(m.group(1))


def _topk_from_entry(
    entry: dict,
    fallback_tid: int | None,
    topk: int,
) -> dict[int, float] | None:
    """Build a ``{token_id: logprob}`` dict for a single Chat content entry.

    Steps (in order):
    1. Parse every entry in ``entry["top_logprobs"]`` whose ``token`` looks
       like ``"token_id:<int>"`` (vLLM ``return_tokens_as_token_ids``). When
       the engine doesn't return that label the candidate is silently dropped.
    2. **Sampled-token fallback**: ensure the actually-generated token id
       (``fallback_tid`` = ``cached_output_token_ids[i]``) is in the dict
       using ``entry["logprob"]`` — overwriting any candidate that disagrees.
       This guarantees msprobe sees at least the sampled token even when
       ``top_logprobs`` is missing, empty, or contains only decoded strings.
    """
    out: dict[int, float] = {}
    if topk > 1:
        for sub in entry.get("top_logprobs") or []:
            if not isinstance(sub, dict):
                continue
            tid = _parse_logprob_token_id(sub.get("token"))
            if tid is None:
                continue
            lp = sub.get("logprob")
            if lp is None:
                continue
            out[tid] = float(lp)
    if "logprob" in entry and fallback_tid is not None:
        # Sampled token always wins over a possibly-mis-labelled top candidate.
        out[int(fallback_tid)] = float(entry["logprob"])
    return out or None


def update_logprob_cache(
    request_info: dict,
    chunk_json: dict,
    *,
    logprobs_count: int,
) -> None:
    """Accumulate per-token logprobs and per-position top-k dicts.

    Two caches are produced:

    - ``cached_logprobs: list[float]`` — top-1 logprob per token.
    - ``cached_topk_logprobs: list[dict[int, float]]`` — per-position top-k
      distribution; index aligned with ``cached_output_token_ids`` (used by
      msprobe). Built for both Chat and Completion responses.

    Args:
        logprobs_count: same field that drives ``top_logprobs`` injection
            (``TokenSamplingConfig.logprobs_count``). 1 → only top-1 fallback;
            >1 → parse ``content[].top_logprobs`` (Chat) or
            ``top_logprobs[i]`` (Completion) for multi-key views.
    """
    choices = chunk_json.get(OpenAIField.CHOICES) or []
    if not choices or not isinstance(choices[0], dict):
        return
    c0 = choices[0]
    token_ids_in_chunk = c0.get(OpenAIField.TOKEN_IDS) or []
    lp_field = c0.get("logprobs")
    if lp_field is None:
        if token_ids_in_chunk:
            _warn_logprobs_missing_once(request_info, len(token_ids_in_chunk))
        return

    is_chat = isinstance(lp_field, dict) and isinstance(lp_field.get("content"), list)
    if not is_chat:
        raw = lp_field.get("token_logprobs") or []
        if raw and all(v is None for v in raw):
            _warn_completion_logprobs_all_null_once(request_info, len(raw))
            return
    if is_chat:
        _update_logprob_cache_chat(
            request_info,
            lp_field,
            logprobs_count=logprobs_count,
        )
        return

    _update_logprob_cache_completion(
        request_info,
        lp_field,
        logprobs_count=logprobs_count,
    )


def _update_logprob_cache_chat(
    request_info: dict,
    lp_field: dict,
    *,
    logprobs_count: int,
) -> None:
    content = lp_field.get("content") or []

    # For per-position token id alignment we always look at the tail of
    # cached_output_token_ids (the ids that arrived in this chunk). The
    # upstream cache is shared and may be appended to by other paths
    # after we read it, so we snapshot the tail length we plan to consume.
    # The fallback is required for every topk (sampled token must be
    # present in cached_topk_logprobs even when logprobs_count > 1 and
    # the engine's top_logprobs list is missing or unparseable).
    ids_tail: list[int] = []
    if content:
        ids = request_info.get("cached_output_token_ids") or []
        take = len(content)
        ids_tail = [int(t) for t in ids[-take:]] if take else []
        if take and len(ids_tail) < take:
            logger.warning(
                "PrecisionSample: content entries=%d exceeds available token_ids tail=%d; "
                "sampled-token fallback will be incomplete for the tail",
                take,
                len(ids_tail),
            )

    collected_floats: list[float] = []
    collected_topk: list[dict[int, float]] = []
    for i, entry in enumerate(content):
        if not isinstance(entry, dict):
            continue
        if "logprob" in entry:
            collected_floats.append(float(entry["logprob"]))

        if logprobs_count <= 0:
            continue

        fallback_tid: int | None = None
        if i < len(ids_tail):
            fallback_tid = ids_tail[i]
        topk = _topk_from_entry(entry, fallback_tid, logprobs_count)
        if topk is not None:
            collected_topk.append(topk)

    if collected_floats:
        request_info.setdefault("cached_logprobs", []).extend(collected_floats)
    if collected_topk:
        request_info.setdefault("cached_topk_logprobs", []).extend(collected_topk)
    if collected_floats or collected_topk:
        logger.debug(
            "PrecisionSample: cached +%d floats +%d topk path=chat total_floats=%d total_topk=%d",
            len(collected_floats),
            len(collected_topk),
            len(request_info.get("cached_logprobs", [])),
            len(request_info.get("cached_topk_logprobs", [])),
        )


def _update_logprob_cache_completion(
    request_info: dict,
    lp_field: dict,
    *,
    logprobs_count: int,
) -> None:
    """Build per-position top-k dicts for Completion responses.

    Mirrors Chat parity: every position whose sampled token id is known
    (from ``cached_output_token_ids`` tail) gets a single-key dict; if
    ``top_logprobs`` is also present and ``logprobs_count > 1``, multi-key
    views are built by reusing ``_parse_logprob_token_id`` for
    ``"token_id:<int>"`` labels and falling back to the raw string key.

    ``cached_logprobs`` keeps its top-1 float semantics (used by
    MsprobeChecker's length-mismatch fallback).
    """
    token_logprobs = lp_field.get("token_logprobs") or []
    completion_top_logprobs = lp_field.get("top_logprobs") or []

    # Align with cached_output_token_ids tail — same convention as Chat path.
    ids = request_info.get("cached_output_token_ids") or []
    take = len(token_logprobs)
    ids_tail = [int(t) for t in ids[-take:]] if take else []
    if take and len(ids_tail) < take:
        logger.warning(
            "PrecisionSample: completion content entries=%d exceeds available token_ids tail=%d; "
            "topk will be incomplete for the tail",
            take,
            len(ids_tail),
        )

    collected_floats: list[float] = []
    collected_topk: list[dict[int, float]] = []
    for i, lp in enumerate(token_logprobs):
        if lp is None:
            continue
        float_val = float(lp)
        collected_floats.append(float_val)

        fallback_tid = ids_tail[i] if i < len(ids_tail) else None
        topk_dict = _build_completion_topk_dict(i, completion_top_logprobs, logprobs_count)

        # Sampled token always wins over (possibly mis-labelled) top candidate.
        if fallback_tid is not None:
            topk_dict[fallback_tid] = float_val

        if topk_dict:
            collected_topk.append(topk_dict)

    if collected_floats:
        request_info.setdefault("cached_logprobs", []).extend(collected_floats)
    if collected_topk:
        request_info.setdefault("cached_topk_logprobs", []).extend(collected_topk)
    if collected_floats or collected_topk:
        logger.debug(
            "PrecisionSample: cached +%d floats +%d topk path=completion total_floats=%d total_topk=%d",
            len(collected_floats),
            len(collected_topk),
            len(request_info.get("cached_logprobs", [])),
            len(request_info.get("cached_topk_logprobs", [])),
        )


def _build_completion_topk_dict(
    i: int,
    completion_top_logprobs: list,
    logprobs_count: int,
) -> dict[int, float]:
    """Build multi-key top-k dict from completion top_logprobs entry."""
    topk_dict: dict[int, float] = {}
    if logprobs_count > 1 and i < len(completion_top_logprobs):
        raw_top = completion_top_logprobs[i] or {}
        if isinstance(raw_top, dict):
            for k, v in raw_top.items():
                if v is None:
                    continue
                tid = _parse_logprob_token_id(k)
                if tid is None:
                    try:
                        tid = int(k)
                    except (TypeError, ValueError):
                        continue
                topk_dict[tid] = float(v)
    return topk_dict


def _warn_logprobs_missing_once(request_info: dict, token_ids_in_chunk: int) -> None:
    """Emit a single WARN per request when chunks arrive with token_ids but no logprobs."""
    state = request_info.setdefault("_precision_logprob_state", {"missing_warned": False})
    if state.get("missing_warned"):
        return
    state["missing_warned"] = True
    logger.warning(
        "PrecisionSample: chunk missing logprobs: token_ids_in_chunk=%d "
        "(engine returned logprobs=null; sampling will fail-open at submit)",
        token_ids_in_chunk,
    )


def _warn_completion_logprobs_all_null_once(request_info: dict, raw_count: int) -> None:
    """Emit a single WARN per request when Completion logprobs object has only null entries."""
    state = request_info.setdefault("_precision_logprob_state", {"null_warned": False})
    if state.get("null_warned"):
        return
    state["null_warned"] = True
    logger.warning(
        "PrecisionSample: completion logprobs all null: raw_count=%d "
        "(engine emitted logprobs object but token_logprobs entries are all null)",
        raw_count,
    )


def strip_logprobs_for_client(
    obj: dict,
    *,
    client_requested_logprobs: bool = False,
) -> None:
    """Strip logprobs fields injected for sampling from a response dict (mutates obj).

    When client_requested_logprobs is True the fields are kept (client asked for them).
    """
    if client_requested_logprobs:
        return
    for ch in obj.get(OpenAIField.CHOICES) or []:
        if isinstance(ch, dict):
            ch.pop("logprobs", None)


# encode_stream_chunk_bytes lives in motor.coordinator.router.recompute.stream
# (shared by recompute and sampling paths).
