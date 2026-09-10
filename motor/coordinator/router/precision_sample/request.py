# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FITNESS FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Inject precision-sampling fields into a selected decode request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from motor.common.logger import get_logger

if TYPE_CHECKING:
    from motor.config.coordinator import PrecisionDetectionConfig

logger = get_logger(__name__)


@dataclass(frozen=True)
class LogprobsRequestMetadata:
    """Client-visible logprobs contract and the effective engine request width."""

    is_chat: bool
    client_requested: bool
    client_count: int
    effective_count: int


def _positive_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value > 0:
        return value
    return 0


def inject_logprobs(
    req_data: dict,
    config: "PrecisionDetectionConfig",
    *,
    req_id: str = "",
) -> LogprobsRequestMetadata:
    """Ensure enough logprobs for sampling without reducing the client's width.

    Must be called before forwarding a decode request to the D engine,
    after return_token_ids has been set by recompute/kv-transfer logic.

    The effective width is the maximum of the client-requested and Motor
    sampling widths. ``return_token_ids`` is also force-set to ``True``.
    ``return_tokens_as_token_ids`` is also force-set to ``True`` so vLLM
    emits top-logprob keys as ``"token_id:<int>"`` strings (parseable
    back to ints by ``response._parse_logprob_token_id``). Negligible
    overhead (single bool in the request body).

    The returned metadata must be used to project the engine response back to
    the client's original width. ``return_tokens_as_token_ids`` is deliberately
    still forced for sampling; compatibility of that representation is tracked
    separately from width preservation.
    """
    lp_count = config.logprobs_count
    is_chat = "messages" in req_data
    api_kind = "chat" if is_chat else "completion"

    old_logprobs = req_data.get("logprobs")
    old_top = req_data.get("top_logprobs") if is_chat else None

    if is_chat:
        # Truthy check (not ``is True``): OpenAI clients may send 1 or other
        # truthy values instead of a strict boolean True; treating them as
        # "not requested" would make the exit projection strip logprobs the
        # client explicitly asked for, contradicting the max-width injection.
        client_requested = bool(old_logprobs)
        client_count = _positive_count(old_top) if client_requested else 0
        effective_count = max(client_count, lp_count)
        req_data["logprobs"] = True
        req_data["top_logprobs"] = effective_count
    else:
        # Truthy check (not ``is True``): completion clients may send boolean
        # ``logprobs=true`` as a switch without a top-k width; ``_positive_count``
        # returns 0 for bool, so client_requested must not rely on client_count alone.
        client_requested = bool(old_logprobs)
        client_count = _positive_count(old_logprobs) if client_requested else 0
        effective_count = max(client_count, lp_count)
        req_data["logprobs"] = effective_count

    req_data["return_token_ids"] = True
    req_data["return_tokens_as_token_ids"] = True

    if old_logprobs != req_data["logprobs"] or (is_chat and old_top != effective_count):
        logger.info(
            "PrecisionSample: inject_logprobs overridden/expanded api=%s req_id=%s "
            "client_logprobs=%r effective_logprobs=%r client_top_logprobs=%r effective_top_logprobs=%r "
            "return_token_ids=true return_tokens_as_token_ids=true",
            api_kind,
            req_id or "-",
            old_logprobs,
            req_data["logprobs"],
            old_top,
            effective_count if is_chat else None,
        )
    else:
        logger.debug(
            "PrecisionSample: inject_logprobs set api=%s req_id=%s logprobs=%r top_logprobs=%r "
            "return_token_ids=true return_tokens_as_token_ids=true",
            api_kind,
            req_id or "-",
            req_data["logprobs"],
            effective_count if is_chat else None,
        )

    return LogprobsRequestMetadata(
        is_chat=is_chat,
        client_requested=client_requested,
        client_count=client_count,
        effective_count=effective_count,
    )
