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
    Compute demand workload for non-affinity allocation paths.

    KV-affinity ALLOCATE commits via :func:`calculate_committed_workload` on the scheduler
    after final selection (ISL - matched_tokens).
    """
    if role == PDRole.ROLE_E:
        return Workload(active_tokens=_calculate_encode_scores(req_info))
    if role == PDRole.ROLE_P:
        return Workload(active_tokens=_prefill_load_score(req_info))
    if role == PDRole.ROLE_D:
        return Workload(active_tokens=_calculate_decode_scores(req_info.req_len))
    if role == PDRole.ROLE_U:
        # Same compute demand as ROLE_P (LB / non-affinity): ISL or prefill heuristic.
        return Workload(active_tokens=_prefill_load_score(req_info))
    logger.warning("Unknown role %s for workload calculation", role)
    return Workload()


def calculate_committed_workload(
    role: PDRole,
    isl: float,
    matched_tokens: float = 0.0,
) -> Workload:
    """
    Authoritative compute load after final affinity endpoint selection.

    ROLE_P / ROLE_U both commit ``ISL - matched_tokens`` (KV reuse reduces remaining
    prefill compute). Non-affinity paths pass matched_tokens=0 → commit ISL.
    """
    isl_f = max(0.0, float(isl))
    matched = min(max(0.0, float(matched_tokens)), isl_f)
    effective = isl_f - matched

    if role not in (PDRole.ROLE_P, PDRole.ROLE_U):
        # Defensive: the affinity commit branch is gated to P/U roles upstream (worker policy
        # selection and the scheduler's commit guard); any other role here means a caller
        # bypassed both gates. The returned value is still the effective compute load.
        logger.warning("calculate_committed_workload called for unexpected role %s", role)
    return Workload(active_tokens=effective)


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
    """Prefill load: real token count when available, else legacy byte-length heuristic."""
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
