# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Build DecodeSample from accumulated stream/response data."""

from __future__ import annotations

import json
from typing import Any

from motor.common.logger import get_logger
from motor.coordinator.fault_tolerance.precision.sample_controller import (
    DecodeSample,
)

logger = get_logger(__name__)


def build_decode_sample(
    p_instance_id: int | None,
    d_instance_id: int,
    request_info: dict,
    req_id: str,
    model: str = "",
    d_infer_base_url: str = "",
    trace_headers: dict[str, str] | None = None,
    request_structure: str = "",
) -> DecodeSample:
    """Construct a DecodeSample from request_info and context.

    Args:
        p_instance_id: P instance id (None if unknown in CDP mode).
        d_instance_id: D instance id.
        request_info: Per-request mutable dict populated by
                      update_token_id_cache / update_logprob_cache.
        req_id: Unique request id.
        model: Model name from the original request data.
        d_infer_base_url: Base URL of the D engine this request was forwarded to.
        request_structure: Content-free request structure summary for tracing.

    Returns:
        A DecodeSample ready for submit_sample().
    """
    extra: dict[str, Any] = {}
    extra["model"] = model
    if d_infer_base_url:
        extra["d_infer_base_url"] = d_infer_base_url

    output_token_ids = request_info.get("cached_output_token_ids") or []
    topk_logprobs = request_info.get("cached_topk_logprobs") or []
    if topk_logprobs and len(topk_logprobs) != len(output_token_ids):
        logger.warning(
            "PrecisionSample: topk_logprobs len=%d != output_token_ids len=%d req_id=%s; "
            "checker will fail-open on this sample",
            len(topk_logprobs),
            len(output_token_ids),
            req_id,
        )

    return DecodeSample(
        p_instance_id=p_instance_id,
        d_instance_id=d_instance_id,
        prompt_token_ids=request_info.get("cached_prompt_token_ids") or [],
        output_token_ids=output_token_ids,
        logprobs=request_info.get("cached_logprobs") or [],
        req_id=req_id,
        extra=extra,
        topk_logprobs=topk_logprobs,
        trace_headers=trace_headers or {},
        request_structure=request_structure,
        output_structure=_build_output_structure(request_info),
    )


def _log_sample_submission(sample: DecodeSample) -> None:
    """One DEBUG line per submit so ops can see msprobe input shape at a glance."""
    n_tokens = len(sample.output_token_ids)
    n_logprobs = len(sample.logprobs)
    n_topk = len(sample.topk_logprobs)
    aligned = (n_logprobs == n_tokens) and n_topk in (0, n_tokens)
    logger.debug(
        "PrecisionSample: submit req_id=%s tokens=%d logprobs=%d topk=%d aligned=%s",
        sample.req_id,
        n_tokens,
        n_logprobs,
        n_topk,
        aligned,
    )
    if n_tokens > 0 and n_logprobs == 0:
        logger.warning(
            "PrecisionSample: sample incomplete req_id=%s tokens=%d logprobs=0; "
            "MsprobeChecker will fail-open on this sample",
            sample.req_id,
            n_tokens,
        )


def _build_output_structure(request_info: dict) -> str:
    """Return content-free output shape captured during precision sampling."""
    output_text_chunks: list[str] = request_info.get("cached_output_text_chunks") or []
    return json.dumps(
        {
            "type": "stream_text",
            "chunks": len(output_text_chunks),
            "length": sum(len(chunk) for chunk in output_text_chunks),
        },
        ensure_ascii=False,
    )
