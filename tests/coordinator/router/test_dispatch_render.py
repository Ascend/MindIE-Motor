# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

# Keep the same package initialization order as existing router tests.
from motor.coordinator.domain import InstanceReadiness  # noqa: F401
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.render.models import TokenizedRequest, TokenizerSource
from motor.coordinator.render.tokenization_service import TokenizationService
from motor.coordinator.router import dispatch


def _tokenized(token_ids, source=TokenizerSource.RENDER, **kwargs):
    return TokenizedRequest(prompt_token_ids=token_ids, tokenizer_source=source, **kwargs)


@pytest.fixture
def dispatch_request(monkeypatch):
    captured = {}

    class CapturingRouter:
        def __init__(self, req_info, config, **kwargs):
            captured["routed"] = req_info

        def set_render_client(self, client):
            captured["render_client"] = client

        async def handle_request(self):
            return JSONResponse({"ok": True})

    async def select_router(_scheduler, req_info=None, config=None):
        captured["selected"] = req_info
        return CapturingRouter

    monkeypatch.setattr(dispatch, "select_router_class", select_router)

    def send(
        results,
        body=None,
        config=None,
        render_client=None,
        obfuscation_service=None,
        image_obfuscation_service=None,
        path="/v1/completions",
        expect_status=HTTPStatus.OK,
    ):
        config = config or CoordinatorConfig()
        sync_owner = TokenizationService(
            config.render_config,
            local_tokenizer=MagicMock(),
            obfuscation_service=obfuscation_service,
        )
        service = MagicMock()
        service.tokenize = AsyncMock(return_value=results)
        service.sync_sampling_params = MagicMock(wraps=sync_owner.sync_sampling_params)
        service.render_client = render_client
        app = FastAPI()
        app.state.tokenization_service = service
        app.state.token_obfuscation_service = obfuscation_service
        app.state.image_obfuscation_service = image_obfuscation_service
        request_manager = RequestManager(config)

        @app.post(path)
        async def entry(request: Request):
            return await dispatch.handle_request(
                request, config, scheduler=AsyncMock(), request_manager=request_manager
            )

        response = TestClient(app).post(path, json=body or {"model": "model", "prompt": "hello"})
        captured["response"] = response
        assert response.status_code == expect_status
        captured["service"] = service
        return captured

    return send


@pytest.mark.parametrize("source", [TokenizerSource.RENDER, TokenizerSource.LOCAL])
def test_dispatch_populates_token_ids_before_router_selection(dispatch_request, source):
    result = _tokenized([10, 20, 30], source)
    captured = dispatch_request([result])

    req_info = captured["selected"]
    assert req_info.token_ids == [10, 20, 30]
    assert req_info.tokenized_requests == [result]
    assert "tokenized_requests" not in req_info.model_dump()
    assert captured["routed"].tokenized_requests[0].tokenizer_source == source


def test_dispatch_keeps_batch_and_uses_longest_prompt_for_scheduler(dispatch_request):
    results = [_tokenized([10]), _tokenized([20, 21, 22])]
    captured = dispatch_request(results, {"model": "model", "prompt": ["a", "longer"]})

    assert captured["routed"].token_ids == [20, 21, 22]
    assert captured["routed"].tokenized_requests == results


def test_dispatch_keeps_semantic_and_engine_token_views_separate(dispatch_request):
    obfuscation_service = MagicMock()
    result = _tokenized([10, 20], engine_prompt_token_ids=[110, 120])

    captured = dispatch_request([result], obfuscation_service=obfuscation_service)

    assert captured["routed"].token_ids == [10, 20]
    assert captured["routed"].engine_token_ids == [110, 120]
    assert captured["routed"]._token_obfuscation_service is obfuscation_service


def test_dispatch_allows_streaming_for_obfuscated_inference(dispatch_request):
    captured = dispatch_request(
        [_tokenized([10, 20])],
        {"model": "model", "prompt": "hello", "stream": True},
        obfuscation_service=MagicMock(),
    )

    assert captured["routed"].req_data["stream"] is True
    assert captured["routed"]._token_obfuscation_service is not None


def test_dispatch_rejects_render_less_api_for_obfuscated_inference(dispatch_request):
    """Routes without a Render contract must never forward a plaintext prompt to a protected model."""
    captured = dispatch_request(
        [_tokenized([10, 20])],
        {"model": "model", "input": "hello"},
        obfuscation_service=MagicMock(),
        path="/v1/responses",
        expect_status=HTTPStatus.NOT_IMPLEMENTED,
    )

    assert "v1/responses" in captured["response"].json()["detail"]


def test_dispatch_rejects_render_less_api_for_image_only_obfuscation(dispatch_request):
    """Image-only obfuscation needs the same Render contract guards as token obfuscation."""
    captured = dispatch_request(
        [_tokenized([10, 20])],
        {"model": "model", "input": "hello"},
        image_obfuscation_service=MagicMock(),
        path="/v1/responses",
        expect_status=HTTPStatus.NOT_IMPLEMENTED,
    )

    assert "v1/responses" in captured["response"].json()["detail"]


def test_dispatch_allows_streaming_for_image_only_obfuscation(dispatch_request):
    captured = dispatch_request(
        [_tokenized([10, 20])],
        {"model": "model", "prompt": "hello", "stream": True},
        image_obfuscation_service=MagicMock(),
    )

    assert captured["routed"].req_data["stream"] is True


def test_dispatch_allows_render_less_api_without_obfuscation(dispatch_request):
    """The Render-less guard only applies to obfuscated inference."""
    captured = dispatch_request(
        [_tokenized([10, 20], TokenizerSource.LOCAL)],
        {"model": "model", "input": "hello"},
        path="/v1/responses",
    )

    assert captured["routed"].token_ids == [10, 20]


def test_dispatch_continues_when_tokenization_is_unavailable(dispatch_request):
    captured = dispatch_request(None)

    assert captured["routed"].token_ids is None
    assert captured["routed"].tokenized_requests == []


def test_dispatch_applies_master_context_budget_after_render(dispatch_request):
    result = _tokenized([10, 20, 30], metadata={"sampling_params": {"max_tokens": 10}})
    config = CoordinatorConfig()
    config.context_budget_mode = "on"
    config.aigw_model = {"p_max_seqlen": 5, "d_max_seqlen": 6}
    captured = dispatch_request(
        [result],
        {"model": "model", "prompt": "hello", "max_tokens": 10},
        config,
    )

    assert captured["routed"].req_data["max_tokens"] == 2
    synced_request = captured["service"].sync_sampling_params.call_args.args[0]
    assert synced_request["max_tokens"] == 2
    assert result.metadata["sampling_params"]["max_tokens"] == 2


def test_dispatch_injects_render_client_only_into_unified_pd(dispatch_request):
    render_client = MagicMock()
    result = _tokenized([10, 20], metadata={"sampling_params": {"max_tokens": 8}})
    captured = dispatch_request(
        [result],
        {"model": "model", "prompt": "hello", "max_tokens": 8},
        render_client=render_client,
    )

    assert captured["render_client"] is render_client
