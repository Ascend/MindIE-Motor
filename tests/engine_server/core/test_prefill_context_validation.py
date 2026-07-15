# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace

import pytest

from motor.engine_server.core.vllm.prefill_context_validation import (
    PrefillContextCheck,
    activate_prefill_context_check,
    install_chat_render_validator,
    install_completion_render_validator,
    reset_prefill_context_check,
    validate_tokenized_prompts,
)


class _Serving:
    def __init__(self, prompt_len=8, max_model_len=16):
        self.model_config = SimpleNamespace(max_model_len=max_model_len)
        self.prompt_len = prompt_len
        self.rendered = False

    def _extract_prompt_len(self, prompt):
        return prompt["tokens"]

    def create_error_response(self, message, param=None):
        return {"error": message, "param": param}

    async def render_chat_request(self, _request):
        self.rendered = True  # Represents vLLM's completed tokenizer/render stage.
        return [], [{"tokens": 8}]

    async def render_completion_request(self, _request):
        return [{"tokens": 8}]


def test_validate_tokenized_prompts_rejects_original_budget_after_tokenization():
    serving = _Serving()
    token = activate_prefill_context_check(PrefillContextCheck(9, "max_tokens"))
    try:
        error = validate_tokenized_prompts(serving, [{"tokens": 8}])
    finally:
        reset_prefill_context_check(token)

    assert error == "Requested max_tokens (9) plus prompt length (8) exceeds the model context length (16)."


def test_validate_tokenized_prompts_reports_max_completion_tokens_parameter():
    serving = _Serving()
    token = activate_prefill_context_check(PrefillContextCheck(9, "max_completion_tokens"))
    try:
        error = validate_tokenized_prompts(serving, [{"tokens": 8}])
    finally:
        reset_prefill_context_check(token)

    assert error == (
        "Requested max_completion_tokens (9) plus prompt length (8) exceeds the model context length (16)."
    )


@pytest.mark.asyncio
async def test_chat_validator_returns_400_error_before_engine_submission():
    serving = _Serving()
    install_chat_render_validator(serving)
    token = activate_prefill_context_check(PrefillContextCheck(9, "max_tokens"))
    try:
        result = await serving.render_chat_request(object())
    finally:
        reset_prefill_context_check(token)

    assert serving.rendered is True
    assert result["param"] == "max_tokens"
    assert "exceeds the model context length" in result["error"]


@pytest.mark.asyncio
async def test_completion_validator_leaves_in_context_request_unchanged():
    serving = _Serving()
    install_completion_render_validator(serving)
    token = activate_prefill_context_check(PrefillContextCheck(8, "max_tokens"))
    try:
        result = await serving.render_completion_request(object())
    finally:
        reset_prefill_context_check(token)

    assert result == [{"tokens": 8}]
