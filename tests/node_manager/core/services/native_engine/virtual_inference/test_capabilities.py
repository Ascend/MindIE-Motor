# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest

from motor.node_manager.core.services.native_engine.virtual_inference.capabilities import (
    is_error_ascend_global_log_level,
    should_enable_vllm_virtual_inference,
)


@pytest.mark.parametrize(
    "enabled,dp_rank,headless,threshold,expected",
    [
        (True, 0, False, 3, True),
        (False, 0, False, 3, False),
        (True, 1, False, 3, False),
        (True, 0, True, 3, False),
        (True, 0, False, 0, False),
        (True, 0, False, -1, False),
        (True, 0, False, 101, False),
        (True, 0, False, 200, False),
    ],
    ids=["enabled", "config_off", "non_dp0", "headless", "zero", "negative", "over_100", "far_over_100"],
)
def test_vllm_virtual_inference_enablement(enabled, dp_rank, headless, threshold, expected):
    assert (
        should_enable_vllm_virtual_inference(
            enable_virtual_inference=enabled,
            dp_rank=dp_rank,
            headless=headless,
            npu_usage_threshold=threshold,
        )
        is expected
    )


@pytest.mark.parametrize(
    "raw_value",
    [None, "", "   ", "\t", "3", 3, " 3 "],
    ids=["unset", "empty", "spaces", "tab", "str3", "int3", "padded3"],
)
def test_error_log_level_allows_virtual_inference(raw_value):
    assert is_error_ascend_global_log_level(raw_value) is True


@pytest.mark.parametrize(
    "raw_value",
    ["0", "1", "2", "4", "debug", "ERROR", " 1 "],
    ids=["0", "1", "2", "4", "debug", "ERROR_word", "padded1"],
)
def test_non_error_log_level_rejects_virtual_inference(raw_value):
    assert is_error_ascend_global_log_level(raw_value) is False
