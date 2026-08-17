# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import patch

import pytest

from motor.config.coordinator import CONTEXT_BUDGET_ON, CoordinatorConfig
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.policy.kv_cache_affinity import adapt_context_budget


def _config() -> CoordinatorConfig:
    config = CoordinatorConfig()
    config.context_budget_mode = CONTEXT_BUDGET_ON
    config.aigw_model = {"p_max_seqlen": 16, "d_max_seqlen": 20}
    return config


def _request(req_data: dict, token_ids: list[int] | None = None) -> RequestInfo:
    return RequestInfo(
        req_id="context-budget",
        req_data=req_data,
        req_len=1,
        token_ids=token_ids,
        api="/v1/chat/completions" if "messages" in req_data else "/v1/completions",
    )


def test_context_budget_clamps_active_chat_completion_field():
    request = _request(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 50,
            "max_completion_tokens": 20,
        },
        token_ids=list(range(5)),
    )

    with patch("motor.coordinator.scheduler.policy.kv_cache_affinity.logger") as logger:
        adapt_context_budget(request, _config())

    assert request.req_data["max_completion_tokens"] == 11
    assert request.req_data["max_tokens"] == 50
    logger.info.assert_called_once_with(
        "Context budget clamped req_id=%s parameter=%s "
        "requested=%d effective=%d prompt_token_len=%d max_model_len=%d",
        "context-budget",
        "max_completion_tokens",
        20,
        11,
        5,
        16,
    )


@pytest.mark.parametrize("req_data", [{"prompt": "hello", "max_tokens": 50}])
def test_context_budget_tokenizes_and_clamps_completions(req_data):
    request = _request(req_data)
    with patch("motor.coordinator.scheduler.policy.kv_cache_affinity.TokenizerManager") as tokenizer_manager:
        tokenizer_manager.return_value.encode.return_value = list(range(5))
        adapt_context_budget(request, _config())

    assert request.req_data["max_tokens"] == 11
    assert request.token_ids == list(range(5))


def test_context_budget_does_not_change_exhausted_context():
    request = _request({"prompt": "hello", "max_tokens": 8}, token_ids=list(range(16)))

    with patch("motor.coordinator.scheduler.policy.kv_cache_affinity.logger") as logger:
        adapt_context_budget(request, _config())

    assert request.req_data["max_tokens"] == 8
    logger.info.assert_not_called()


def test_context_budget_does_not_log_when_budget_is_unchanged():
    request = _request({"prompt": "hello", "max_tokens": 8}, token_ids=list(range(5)))

    with patch("motor.coordinator.scheduler.policy.kv_cache_affinity.logger") as logger:
        adapt_context_budget(request, _config())

    assert request.req_data["max_tokens"] == 8
    logger.info.assert_not_called()
