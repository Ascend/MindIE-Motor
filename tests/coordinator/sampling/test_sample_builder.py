# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import logging

import motor.coordinator.router.precision_sample.sample_builder as sample_builder_mod
from motor.coordinator.router.precision_sample.sample_builder import (
    build_decode_sample,
)


def _import_build_decode_sample():
    # Same bootstrap pattern as other router tests; see
    # ``tests/coordinator/sampling/test_precision_logprob_cache.py`` for
    # why this indirection is needed.
    return build_decode_sample


def test_build_decode_sample_passes_topk_logprobs_through() -> None:
    bds = _import_build_decode_sample()
    request_info = {
        "cached_prompt_token_ids": [101, 102],
        "cached_output_token_ids": [201, 202, 203],
        "cached_logprobs": [-0.1, -0.2, -0.3],
        "cached_topk_logprobs": [{201: -0.1}, {202: -0.2}, {203: -0.3}],
    }
    sample = bds(
        p_instance_id=1,
        d_instance_id=2,
        request_info=request_info,
        req_id="req-1",
        model="m",
    )
    assert sample.topk_logprobs == [{201: -0.1}, {202: -0.2}, {203: -0.3}]
    assert len(sample.topk_logprobs) == len(sample.output_token_ids)
    assert sample.extra["model"] == "m"


def test_build_decode_sample_warns_on_length_mismatch(caplog) -> None:
    bds = _import_build_decode_sample()
    request_info = {
        "cached_output_token_ids": [201, 202, 203],
        "cached_topk_logprobs": [{201: -0.1}],  # only 1 of 3
    }
    with caplog.at_level(logging.WARNING, logger=sample_builder_mod.logger.name):
        sample = bds(
            p_instance_id=None,
            d_instance_id=2,
            request_info=request_info,
            req_id="req-mismatch",
        )
    # Sample is still built (checker will fail-open); we just want the
    # mismatch surfaced for ops.
    assert len(sample.topk_logprobs) == 1
    assert len(sample.output_token_ids) == 3
    assert any("topk_logprobs len=1 != output_token_ids len=3" in r.message for r in caplog.records)


def test_build_decode_sample_no_topk_no_warning() -> None:
    """Completion path or empty topk should not trigger a spurious warning."""
    bds = _import_build_decode_sample()
    request_info = {
        "cached_output_token_ids": [201, 202],
        # no cached_topk_logprobs at all
    }
    sample = bds(
        p_instance_id=1,
        d_instance_id=2,
        request_info=request_info,
        req_id="req-no-topk",
    )
    assert not sample.topk_logprobs
    assert sample.output_token_ids == [201, 202]
