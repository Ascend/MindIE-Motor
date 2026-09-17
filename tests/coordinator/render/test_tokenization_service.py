# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi import HTTPException

from motor.common.utils.singleton import ThreadSafeSingleton
from motor.config.coordinator import RenderConfig
from motor.coordinator.render.image_obfuscation_service import ImageObfuscationError
from motor.coordinator.render.models import TokenizedRequest, TokenizerSource
from motor.coordinator.render.tokenization_service import TokenizationService
from motor.coordinator.render.vllm_render_client import (
    RenderRequestError,
    RenderTimeoutError,
    VLLMRenderClient,
)
from motor.coordinator.scheduler.policy.kv_cache_affinity import TokenizerManager

pytestmark = pytest.mark.anyio


def _render_result():
    return [
        TokenizedRequest(
            prompt_token_ids=[11, 12],
            tokenizer_source=TokenizerSource.RENDER,
            metadata={"sampling_params": {"max_tokens": 10}},
        )
    ]


@pytest.fixture
def tokenizer_manager_config():
    ThreadSafeSingleton._instances.pop(TokenizerManager, None)
    config = MagicMock()
    conductor = config.scheduler_config.kv_conductor_config
    conductor.conductor_service = ""
    conductor.model_path = "/path/to/model"
    conductor.engine_type = "vllm"
    config.render_config.enable = True
    config.context_budget_mode = "on"
    yield config
    ThreadSafeSingleton._instances.pop(TokenizerManager, None)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def local():
    tokenizer = MagicMock()
    tokenizer.apply_chat_template.return_value = [21, 22]
    tokenizer.encode.return_value = [31, 32]
    return tokenizer


@pytest.fixture
def render_client():
    client = AsyncMock(spec=VLLMRenderClient)
    client.render.return_value = _render_result()
    return client


@pytest.fixture
def service_factory(render_client, local):
    def make(enabled=True, **kwargs):
        return TokenizationService(RenderConfig(enable=enabled), render_client, local, **kwargs)

    return make


async def test_render_success_does_not_call_local_tokenizer(service_factory, render_client, local):
    request = {"model": "model", "prompt": "hello", "max_tokens": 10}
    assert (
        await service_factory(context_budget_mode="off").tokenize("request-1", "v1/completions", request)
        == _render_result()
    )
    render_client.render.assert_awaited_once_with("v1/completions", request)
    assert render_client.render.await_args.args[1] is request
    local.encode.assert_not_called()


async def test_render_chat_request_is_unchanged_without_obfuscation(service_factory, render_client, local):
    request = {
        "model": "model",
        "messages": [{"role": "user", "content": "hello"}],
        "chat_template_kwargs": {"enable_thinking": True},
        "max_tokens": 10,
    }
    await service_factory(context_budget_mode="off").tokenize("request-chat", "v1/chat/completions", request)

    render_request = render_client.render.await_args.args[1]
    assert render_request is request
    assert render_request["chat_template_kwargs"] == {"enable_thinking": True}


async def test_render_chat_request_leaves_thinking_unspecified_for_obfuscation(render_client, local):
    obfuscation_service = MagicMock()
    service = TokenizationService(
        RenderConfig(enable=True),
        render_client,
        local,
        obfuscation_service=obfuscation_service,
    )
    request = {
        "model": "model",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 10,
    }

    await service.tokenize("request-obfuscated-chat", "v1/chat/completions", request)

    render_request = render_client.render.await_args.args[1]
    assert "chat_template_kwargs" not in render_request


@pytest.mark.parametrize(
    "request_override,expected",
    [
        ({"chat_template_kwargs": {"enable_thinking": True, "custom": 1}}, True),
        ({"thinking": {"type": "enabled"}}, True),
        ({"thinking": {"type": "disabled"}}, False),
        ({"reasoning_effort": "high"}, True),
        ({"reasoning_effort": "none"}, False),
    ],
)
async def test_obfuscated_render_respects_explicit_thinking_mode(render_client, local, request_override, expected):
    service = TokenizationService(
        RenderConfig(enable=True),
        render_client,
        local,
        obfuscation_service=MagicMock(),
    )
    request = {
        "model": "model",
        "messages": [{"role": "user", "content": "hello"}],
        "documents": [{"title": "doc"}],
        "max_tokens": 10,
        **request_override,
    }

    await service.tokenize("request-obfuscated-chat", "v1/chat/completions", request)

    template_kwargs = render_client.render.await_args.args[1]["chat_template_kwargs"]
    assert template_kwargs["enable_thinking"] is expected
    assert "documents" not in template_kwargs
    assert "reasoning_effort" not in template_kwargs


async def test_render_success_keeps_semantic_tokens_and_obfuscates_engine_tokens(render_client, local):
    obfuscation_service = MagicMock()
    obfuscation_service.obfuscate.return_value = [111, 112]
    service = TokenizationService(
        RenderConfig(enable=True),
        render_client,
        local,
        obfuscation_service=obfuscation_service,
    )

    result = await service.tokenize("request-obfuscated", "v1/completions", {"model": "model", "prompt": "hi"})

    assert result[0].prompt_token_ids == [11, 12]
    assert result[0].engine_prompt_token_ids == [111, 112]
    obfuscation_service.obfuscate.assert_called_once_with([11, 12])


async def test_render_failure_is_fail_closed_when_obfuscation_is_enabled(render_client, local):
    render_client.render.side_effect = RenderTimeoutError("timeout")
    service = TokenizationService(
        RenderConfig(enable=True),
        render_client,
        local,
        obfuscation_service=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="Render is required"):
        await service.tokenize("request-obfuscated", "v1/completions", {"model": "model", "prompt": "hi"})
    local.encode.assert_not_called()


async def test_render_failure_is_fail_closed_when_image_obfuscation_is_enabled(render_client, local):
    """Image-only obfuscation must not fall back to the local tokenizer either."""
    render_client.render.side_effect = RenderTimeoutError("timeout")
    service = TokenizationService(
        RenderConfig(enable=True),
        render_client,
        local,
        image_obfuscation_service=MagicMock(),
    )

    with pytest.raises(ImageObfuscationError, match="Render is required for image-obfuscated inference"):
        await service.tokenize("request-image-obfuscated", "v1/chat/completions", {"model": "model", "prompt": "hi"})
    local.encode.assert_not_called()


def test_render_fallback_loads_local_tokenizer_lazily_without_conductor(tokenizer_manager_config):
    """Context budget reuses Render token IDs and defers local loading until fallback."""
    tokenizer = MagicMock()
    with patch("transformers.AutoTokenizer.from_pretrained", return_value=tokenizer) as load_tokenizer:
        manager = TokenizerManager(tokenizer_manager_config)
        assert os.environ["TORCH_DEVICE_BACKEND_AUTOLOAD"] == "0"
        assert manager.tokenizer is None
        load_tokenizer.assert_not_called()
        loaded_tokenizer = manager.get_tokenizer()
    assert loaded_tokenizer is manager.tokenizer is tokenizer
    load_tokenizer.assert_called_once_with("/path/to/model", trust_remote_code=True)


async def test_render_failure_falls_back_to_local_chat_tokenizer(service_factory, render_client, local):
    render_client.render.side_effect = RenderTimeoutError("timeout")
    request = {
        "model": "model",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"type": "function"}],
        "max_completion_tokens": 10,
    }
    result = await service_factory(context_budget_mode="on").tokenize("request-2", "v1/chat/completions", request)
    assert (result[0].prompt_token_ids, result[0].tokenizer_source) == (
        [21, 22],
        TokenizerSource.LOCAL,
    )
    assert request["max_completion_tokens"] == 10
    assert render_client.render.await_args.args[1]["max_completion_tokens"] == 1
    local.apply_chat_template.assert_called_once_with(
        request["messages"],
        request["tools"],
        req_data=request,
    )


async def test_render_request_error_does_not_fallback(service_factory, render_client, local):
    render_client.render.side_effect = RenderRequestError("Render", 422, "invalid request")
    request = {"model": "model", "prompt": "hello"}

    with pytest.raises(HTTPException) as exc_info:
        await service_factory().tokenize("request-invalid", "v1/completions", request)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "invalid request"
    local.encode.assert_not_called()


async def test_unsupported_render_api_uses_local_chat_tokenizer(service_factory, render_client, local):
    request = {
        "model": "model",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 10,
    }

    result = await service_factory().tokenize("request-messages", "v1/messages", request)

    assert (result[0].prompt_token_ids, result[0].tokenizer_source) == (
        [21, 22],
        TokenizerSource.LOCAL,
    )
    render_client.render.assert_not_awaited()
    local.apply_chat_template.assert_called_once_with(request["messages"], None, req_data=request)


@pytest.mark.parametrize(
    "prompt,side_effect,expected,encode_calls",
    [
        pytest.param("hello", None, [[31, 32]], [call("hello")], id="text"),
        pytest.param([41, 42], None, [[41, 42]], [], id="token-ids"),
        pytest.param(
            ["first", "second"],
            [[31], [41, 42]],
            [[31], [41, 42]],
            [call("first"), call("second")],
            id="text-batch",
        ),
    ],
)
async def test_disabled_render_uses_local_completion_tokenizer(
    prompt,
    side_effect,
    expected,
    encode_calls,
    service_factory,
    render_client,
    local,
):
    local.encode.side_effect = side_effect
    result = await service_factory(False).tokenize("request", "v1/completions", {"model": "model", "prompt": prompt})
    assert [item.prompt_token_ids for item in result] == expected
    assert all(item.tokenizer_source == TokenizerSource.LOCAL for item in result)
    render_client.render.assert_not_awaited()
    assert local.encode.call_args_list == encode_calls


async def test_local_tokenizer_unavailable_keeps_tokenization_optional(
    service_factory,
    render_client,
    local,
):
    render_client.render.side_effect = RenderTimeoutError("timeout")
    local.encode.return_value = []
    assert (
        await service_factory().tokenize("request-6", "v1/completions", {"model": "model", "prompt": "hello"}) is None
    )


@pytest.mark.parametrize("budget_field", ["max_tokens", "max_completion_tokens"])
async def test_context_budget_preflight_uses_one_token_without_mutating_request(
    budget_field,
    service_factory,
    render_client,
):
    messages = [{"role": "user", "content": "hello"}]
    request = {"model": "model", "messages": messages, budget_field: 10}
    await service_factory(context_budget_mode="on").tokenize("request-7", "v1/chat/completions", request)
    assert request[budget_field] == 10
    render_request = render_client.render.await_args.args[1]
    assert (
        render_request is not request and render_request["messages"] is messages and render_request[budget_field] == 1
    )


@pytest.mark.parametrize(
    "request_data,expected",
    [
        ({"messages": [], "max_tokens": 8}, 8),
        ({"messages": [], "max_completion_tokens": 7, "max_tokens": 8}, 7),
    ],
)
def test_sync_sampling_params_uses_effective_request_budget(request_data, expected):
    result = _render_result()
    TokenizationService(RenderConfig(enable=True), AsyncMock()).sync_sampling_params(request_data, result)
    assert result[0].metadata["sampling_params"]["max_tokens"] == expected


def test_sync_sampling_params_greedy_for_obfuscated_thinking():
    obfuscation_service = MagicMock()
    service = TokenizationService(
        RenderConfig(enable=True),
        AsyncMock(),
        obfuscation_service=obfuscation_service,
    )
    result = [
        TokenizedRequest(
            prompt_token_ids=[1],
            tokenizer_source=TokenizerSource.RENDER,
            metadata={"sampling_params": {"max_tokens": 10, "temperature": 0.6}},
        )
    ]
    request_data = {
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 8,
        "chat_template_kwargs": {"enable_thinking": True},
    }

    service.sync_sampling_params(request_data, result)

    assert result[0].metadata["sampling_params"]["temperature"] == 0.0


def test_sync_sampling_params_keeps_explicit_temperature_for_obfuscated_thinking():
    service = TokenizationService(
        RenderConfig(enable=True),
        AsyncMock(),
        obfuscation_service=MagicMock(),
    )
    result = [
        TokenizedRequest(
            prompt_token_ids=[1],
            tokenizer_source=TokenizerSource.RENDER,
            metadata={"sampling_params": {"max_tokens": 10, "temperature": 0.6}},
        )
    ]
    request_data = {
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.7,
        "chat_template_kwargs": {"enable_thinking": True},
    }

    service.sync_sampling_params(request_data, result)

    assert result[0].metadata["sampling_params"]["temperature"] == 0.6
