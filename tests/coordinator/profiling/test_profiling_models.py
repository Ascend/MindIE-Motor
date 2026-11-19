# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Request models must reject unknown keys so a typo cannot widen the fan-out."""

import pytest
from pydantic import ValidationError

from motor.coordinator.profiling.models import ProfilingStartRequest, ProfilingTargetSelector


@pytest.mark.parametrize(
    "payload",
    [
        {"role": ["prefill"]},
        {"instance": [0]},
        {"dp": [0]},
        {"services": "deepseek-v3"},
    ],
)
def test_start_request_rejects_misspelled_selector_keys(payload):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProfilingStartRequest.model_validate(payload)


def test_stop_request_rejects_removed_profiling_id():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProfilingTargetSelector.model_validate({"profiling_id": "prof-missing"})


def test_start_request_still_accepts_the_documented_selectors():
    request = ProfilingStartRequest.model_validate(
        {
            "service": "deepseek-v3",
            "roles": ["prefill"],
            "instances": [0, 1],
            "dps": {"0": [0, 1], "1": [2]},
            "profiler_options": {"activities": ["CPU", "NPU"], "custom_option": {"nested": True}},
        }
    )

    assert request.service == "deepseek-v3"
    assert request.roles == ["prefill"]
    assert request.instances == [0, 1]
    assert request.dps == {0: [0, 1], 1: [2]}
    assert request.profiler_options == {"activities": ["CPU", "NPU"], "custom_option": {"nested": True}}


def test_stop_request_accepts_selector_only_payload():
    request = ProfilingTargetSelector.model_validate({"roles": ["decode"]})

    assert request.roles == ["decode"]


def test_selector_deduplicates_ids_in_input_order() -> None:
    request = ProfilingStartRequest(instances=[2, 1, 2], dps={"2": [3, 1, 3]})

    assert request.instances == [2, 1]
    assert request.dps == {2: [3, 1]}


@pytest.mark.parametrize(
    "payload",
    [
        {"instances": [0] * 4097},
        {"dps": [0] * 4097},
        {"dps": {str(instance): [0] for instance in range(4097)}},
        {"dps": {"0": [0] * 2049, "1": [0] * 2048}},
    ],
)
def test_selector_limits_raw_entries_before_deduplication(payload: dict) -> None:
    with pytest.raises(ValidationError, match="at most 4096"):
        ProfilingStartRequest.model_validate(payload)
