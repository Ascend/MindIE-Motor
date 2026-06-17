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

"""Inject logprobs and return_token_ids into decode request payload (always-on for precision sampling)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from motor.common.logger import get_logger

if TYPE_CHECKING:
    from motor.config.coordinator import TokenSamplingConfig

logger = get_logger(__name__)


def inject_logprobs(
    req_data: dict,
    config: "TokenSamplingConfig",
    *,
    req_id: str = "",
) -> None:
    """Always inject logprobs and ensure return_token_ids for precision sampling.

    Must be called before forwarding a decode request to the D engine,
    after return_token_ids has been set by recompute/kv-transfer logic.

    Force-overwrites any pre-existing ``logprobs`` / ``top_logprobs`` field on
    the request so a client-supplied ``null/0/false`` value cannot silently
    disable sampling. ``return_token_ids`` is also force-set to ``True``.

    When a pre-existing logprobs value is replaced, emits one INFO line tagged
    ``PrecisionSample: inject_logprobs`` so ops can see what was sent to the
    engine vs. what the client asked for.
    """
    lp_count = config.logprobs_count
    is_chat = "messages" in req_data
    api_kind = "chat" if is_chat else "completion"

    new_logprobs: object = lp_count if not is_chat else True
    old_logprobs = req_data.get("logprobs")
    req_data["logprobs"] = new_logprobs

    old_top = req_data.get("top_logprobs") if is_chat else None
    if is_chat:
        req_data["top_logprobs"] = lp_count
    req_data["return_token_ids"] = True

    if old_logprobs != new_logprobs:
        logger.info(
            "PrecisionSample: inject_logprobs overridden api=%s req_id=%s "
            "client_logprobs=%r->%r top_logprobs=%r->%r return_token_ids=true",
            api_kind,
            req_id or "-",
            old_logprobs,
            new_logprobs,
            old_top,
            lp_count if is_chat else None,
        )
    else:
        logger.debug(
            "PrecisionSample: inject_logprobs set api=%s req_id=%s logprobs=%r top_logprobs=%r return_token_ids=true",
            api_kind,
            req_id or "-",
            new_logprobs,
            lp_count if is_chat else None,
        )
