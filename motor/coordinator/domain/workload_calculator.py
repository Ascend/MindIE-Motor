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

"""Demand workload scoring from role and request length (scheduler + router allocation)."""

from __future__ import annotations

from motor.common.resources.endpoint import Workload
from motor.common.resources.instance import PDRole
from motor.common.logger import get_logger
from motor.coordinator.models.request import RequestInfo
from motor.common.utils.image_utils import get_mul_token


logger = get_logger(__name__)


def calculate_demand_workload(role: PDRole, req_info: RequestInfo) -> Workload:
    """
    Compute demand workload for this allocation from role and request length.
    Shared by BaseRouter.prepare_resource and WorkloadActionHandler ALLOCATION.

    Args:
        role: PDRole enum (encode/prefill/decode/both)
        request_length: Request length

    Returns:
        Workload: Load for ALLOCATION (used by select_and_allocate / add_req_workload)
    """

    if role == PDRole.ROLE_E:
        score = _calculate_encode_scores(req_info)
        return Workload(active_tokens=score)
    if role == PDRole.ROLE_P:
        score = _prefill_load_score(req_info)
        return Workload(active_kv_cache=score, active_tokens=score)
    if role == PDRole.ROLE_D:
        score = _calculate_decode_scores(req_info.req_len)
        return Workload(active_tokens=score)
    if role == PDRole.ROLE_U:
        score = _calculate_both_scores(req_info.req_len)
        return Workload(active_kv_cache=score, active_tokens=score)
    logger.warning("Unknown role %s for workload calculation", role)
    return Workload()


def _calculate_encode_scores(req_info: RequestInfo) -> float:
    """Encode role workload score."""
    messages = req_info.req_data.get("messages")
    mul_token = 0
    if not messages:
        return mul_token

    for msg in messages:
        if not isinstance(msg.get("content"), list):
            continue

        for content_item in msg["content"]:
            content_type = content_item.get("type")
            if not content_type:
                continue

            if content_type == "image_url":
                img_url = content_item.get("image_url", {}).get("url", "")
                mul_token += get_mul_token(img_url)
            elif content_type == "video_url":
                mul_token += req_info.req_len * 32
    return mul_token


def _prefill_load_score(req_info: RequestInfo) -> float:
    """
    Prefill load in **real prompt tokens** when available, else the legacy byte-length estimate.

    When the request was tokenized at routing (KV affinity sets ``req_info.token_ids``), the load
    uses the real token count so it shares the token unit with the affinity prefill cost
    (``isl - matched_tokens``) -- this is what makes the unified score's weights principled instead
    of mixing token counts with a byte-length fit. Falls back to ``_calculate_prefill_scores`` when
    token ids are absent (e.g. load_balance/round_robin, or no tokenizer configured).
    """
    token_ids = getattr(req_info, "token_ids", None)
    if isinstance(token_ids, list) and token_ids:
        return float(len(token_ids))
    return _calculate_prefill_scores(req_info.req_len)


def _calculate_prefill_scores(request_length: int) -> float:
    """Prefill role workload score (legacy byte-length heuristic; fallback only)."""
    length_score = request_length / 4.0
    return length_score * 0.0345 + 120.0745


def _calculate_decode_scores(request_length: int) -> float:
    """Decode role workload score."""
    return float(request_length)


def _calculate_both_scores(request_length: int) -> float:
    """Hybrid role workload score."""
    return (_calculate_prefill_scores(request_length) + _calculate_decode_scores(request_length)) * 0.5
