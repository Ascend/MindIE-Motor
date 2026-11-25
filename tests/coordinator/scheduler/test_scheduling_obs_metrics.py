# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""KV attribution decisions that the scheduling metrics actually publish."""

from types import SimpleNamespace

import pytest

from motor.coordinator.scheduler.runtime.scheduling_obs_metrics import (
    classify_and_record_kv_attribution,
    get_scheduling_obs_metrics,
    mark_kv_cache_load_failed,
    record_selection_kv_context,
)


def _context(*, predicted: int, lb_abandoned: bool = False, cache_load_failed: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        _kv_obs_recorded=False,
        _kv_obs_context={
            "selected_matched": predicted,
            "prompt_tokens": 128,
            "lb_abandoned": lb_abandoned,
            "cache_load_failed": cache_load_failed,
        },
    )


@pytest.fixture(autouse=True)
def _reset_obs_metrics():
    get_scheduling_obs_metrics().reset()
    yield
    get_scheduling_obs_metrics().reset()


def test_endpoint_outside_affinity_candidates_is_not_lb_abandoned():
    req = SimpleNamespace(
        token_ids=[1] * 128,
        kv_affinity_debug={(1, 0): (128,)},
    )
    record_selection_kv_context(req, selected_instance_id=9, selected_endpoint_id=0, candidate_policy="load_balance")
    assert req._kv_obs_context["lb_abandoned"] is False
    assert req._kv_obs_context["selected_matched"] == 0


def test_lower_hit_inside_affinity_candidates_is_lb_abandoned():
    req = SimpleNamespace(
        token_ids=[1] * 128,
        kv_affinity_debug={(1, 0): (128,), (2, 0): (32,)},
    )
    record_selection_kv_context(req, selected_instance_id=2, selected_endpoint_id=0, candidate_policy="load_balance")
    assert req._kv_obs_context["lb_abandoned"] is True
    assert (
        classify_and_record_kv_attribution(
            req, usage={"prompt_tokens": 128, "prompt_tokens_details": {"cached_tokens": 128}}
        )
        == "lb_abandoned"
    )


@pytest.mark.parametrize(
    ("predicted", "cached", "expected"),
    [
        (128, None, "unknown"),
        (128, 0, "index_stale"),
        (128, 64, "prediction_mismatch"),
        (128, 128, "hit"),
        (0, 0, "actual_recompute"),
    ],
)
def test_classify_uses_actual_cached_tokens(predicted, cached, expected):
    req = _context(predicted=predicted)
    usage = {"prompt_tokens": 128}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    assert classify_and_record_kv_attribution(req, usage=usage) == expected
    assert classify_and_record_kv_attribution(req, usage=usage) is None


def test_load_failure_mark_requires_an_existing_context():
    req = SimpleNamespace()
    assert mark_kv_cache_load_failed(req, "kv_transfer failed") is False
    req = _context(predicted=128)
    assert mark_kv_cache_load_failed(req, "kv_transfer failed") is True
    assert classify_and_record_kv_attribution(req, usage={"prompt_tokens_details": {"cached_tokens": 128}}) == (
        "cache_load_failed"
    )
