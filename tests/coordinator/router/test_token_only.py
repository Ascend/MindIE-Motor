# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for shared token-only execution helpers."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from motor.coordinator.render.models import TokenizedRequest, TokenizerSource
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.router.adapters.pd_protocol import (
    EngineEndpointMetadata,
    EngineLegSpec,
    EnginePhase,
    EngineProtocolError,
    LegContext,
    VllmProtocolAdapter,
)
from motor.coordinator.router.token_only import (
    active_token_only_request,
    build_response_ready_callbacks,
    build_token_only_batch,
    build_trigger_token_only_decode_request,
    build_trigger_token_only_prefill_request,
    finish_token_only_response,
    merge_token_only_streams,
    require_kv_transfer,
    run_token_only_or_fallback,
    token_only_requests_for_attempt,
)
from motor.coordinator.router.rescheduler.rescheduler import RetryRequestPlan
from motor.coordinator.router.upstream_error import UpstreamHTTPError


def test_build_token_only_batch_preserves_order_and_keeps_topology_in_leg_factory() -> None:
    tokenized_requests = [
        TokenizedRequest(
            prompt_token_ids=[index],
            engine_prompt_token_ids=[index + 100],
            tokenizer_source=TokenizerSource.RENDER,
            metadata={"sampling_params": {"max_tokens": 4}},
        )
        for index in (10, 20)
    ]

    def leg_factory(index: int) -> EngineLegSpec:
        return EngineLegSpec(
            context=LegContext(
                engine_request_id=f"request#p{index}",
                pair_id="pair",
                attempt_seq=1,
                api="v1/completions",
                endpoint=EngineEndpointMetadata(host="engine.local"),
            ),
            phase=EnginePhase.DECODE,
        )

    requests = build_token_only_batch(VllmProtocolAdapter(), tokenized_requests, leg_factory)

    assert [request.body["token_ids"] for request in requests] == [[110], [120]]
    assert [request.body["request_id"] for request in requests] == [
        "request#p0",
        "request#p1",
    ]


@pytest.mark.parametrize(
    ("obfuscate", "expected_engine_ids"),
    [(False, None), (True, [110, 120, 130, 131])],
)
def test_token_only_retry_replays_tokens_reduces_budget_and_obfuscates_engine_input(
    obfuscate: bool,
    expected_engine_ids: list[int] | None,
) -> None:
    req_info = Mock()
    req_info.req_data = {"stream": True}
    req_info.tokenized_requests = [
        TokenizedRequest(
            prompt_token_ids=[10, 20],
            tokenizer_source=TokenizerSource.RENDER,
            metadata={"sampling_params": {"max_tokens": 8, "temperature": 0.5}},
        )
    ]
    req_info.effective_entry_api.return_value = "v1/completions"
    req_info._token_obfuscation_service = Mock() if obfuscate else None
    if obfuscate:
        req_info._token_obfuscation_service.obfuscate.return_value = expected_engine_ids
    plan = RetryRequestPlan(
        prompt_token_ids=(10, 20, 30, 31),
        api="v1/completions",
        remove_chat_fields=False,
        cached_output_tokens=2,
    )

    replay = token_only_requests_for_attempt(req_info, AsyncMock(), allow_streaming=True, retry_plan=plan)[0]

    assert replay.prompt_token_ids == [10, 20, 30, 31]
    assert replay.engine_prompt_token_ids == expected_engine_ids
    assert replay.metadata["sampling_params"] == {"max_tokens": 6, "temperature": 0.5}
    assert req_info.tokenized_requests[0].metadata["sampling_params"]["max_tokens"] == 8
    if obfuscate:
        req_info._token_obfuscation_service.obfuscate.assert_called_once_with([10, 20, 30, 31])


def test_response_ready_callbacks_wait_for_every_stream_and_are_idempotent() -> None:
    on_all_ready = Mock()
    callbacks = build_response_ready_callbacks(2, on_all_ready)

    callbacks[0]()
    callbacks[0]()
    on_all_ready.assert_not_called()

    callbacks[1]()
    callbacks[1]()
    on_all_ready.assert_called_once_with()


def test_active_token_only_request_uses_explicit_prompt_index() -> None:
    requests = [
        TokenizedRequest(prompt_token_ids=[index], tokenizer_source=TokenizerSource.RENDER) for index in (10, 20)
    ]

    assert active_token_only_request(requests) is None
    assert active_token_only_request(requests, 1) is requests[1]
    assert active_token_only_request(requests, 2) is None
    assert active_token_only_request(requests[:1]) is requests[0]


@pytest.mark.asyncio
async def test_merge_token_only_streams_closes_inner_stream(monkeypatch) -> None:
    closed = asyncio.Event()

    async def inner_stream(*_args, **_kwargs):
        try:
            yield b"chunk"
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr("motor.coordinator.router.token_only.merge_derender_streams", inner_stream)
    req_info = Mock(req_id="request", req_data={"model": "model"})
    req_info.effective_entry_api.return_value = "v1/completions"
    session = Mock(processors=[Mock()])
    merged = merge_token_only_streams(
        req_info,
        AsyncMock(),
        [Mock()],
        session=session,
        tokenized_requests=[TokenizedRequest(prompt_token_ids=[10], tokenizer_source=TokenizerSource.RENDER)],
    )

    assert await anext(merged) == b"chunk"
    await merged.aclose()

    assert closed.is_set()


@pytest.mark.parametrize(
    ("builder", "expected_kv", "expected_max_tokens"),
    [
        (
            lambda adapter, tokenized, context: build_trigger_token_only_decode_request(
                adapter,
                tokenized,
                context,
                "http://coordinator/v1/metaserver",
                stream=True,
            ),
            {
                "do_remote_decode": False,
                "do_remote_prefill": True,
                "metaserver": "http://coordinator/v1/metaserver",
            },
            8,
        ),
        (
            lambda adapter, tokenized, context: build_trigger_token_only_prefill_request(
                adapter,
                tokenized,
                context,
                {
                    "do_remote_decode": False,
                    "do_remote_prefill": True,
                    "metaserver": "http://coordinator/v1/metaserver",
                    "remote_block_ids": [1, 2],
                },
            ),
            {
                "do_remote_decode": True,
                "do_remote_prefill": False,
                "remote_block_ids": [1, 2],
            },
            1,
        ),
    ],
)
def test_trigger_token_only_wrappers_build_shared_engine_contract(builder, expected_kv, expected_max_tokens) -> None:
    tokenized = TokenizedRequest(
        prompt_token_ids=[10, 20],
        tokenizer_source=TokenizerSource.RENDER,
        metadata={
            "sampling_params": {
                "max_tokens": 8,
                "extra_args": {"render_flag": "keep"},
            }
        },
    )
    context = LegContext(
        engine_request_id="request",
        pair_id="pair",
        attempt_seq=1,
        api="v1/chat/completions",
        endpoint=EngineEndpointMetadata(host="engine.local"),
    )

    request = builder(VllmProtocolAdapter(), tokenized, context)

    assert request.body["token_ids"] == [10, 20]
    assert request.body["sampling_params"]["max_tokens"] == expected_max_tokens
    assert request.body["kv_transfer_params"] == expected_kv
    assert request.body["stream"] is (expected_max_tokens == 8)


@pytest.mark.parametrize("params", [None, {}])
def test_required_kv_transfer_rejects_incomplete_descriptor(params) -> None:
    with pytest.raises(EngineProtocolError, match="Missing kv_transfer_params"):
        require_kv_transfer(params, phase=EnginePhase.DECODE)


def _upstream_error(status_code: int) -> UpstreamHTTPError:
    return UpstreamHTTPError(status_code=status_code, body=b"error", headers={}, phase="decode")


@pytest.mark.asyncio
async def test_run_token_only_returns_primary_result_without_fallback() -> None:
    send = AsyncMock(side_effect=lambda request: {"source": request})

    result, used_token_only = await run_token_only_or_fallback("token-only", "native", send)

    assert result == {"source": "token-only"}
    assert used_token_only is True
    send.assert_awaited_once_with("token-only")


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 501])
async def test_run_token_only_falls_back_only_when_endpoint_is_unsupported(
    status_code,
) -> None:
    send = AsyncMock(side_effect=[_upstream_error(status_code), {"source": "native"}])
    on_unsupported = Mock()

    result, used_token_only = await run_token_only_or_fallback(
        "token-only",
        "native",
        send,
        on_unsupported=on_unsupported,
    )

    assert result == {"source": "native"}
    assert used_token_only is False
    assert send.await_args_list[0].args == ("token-only",)
    assert send.await_args_list[1].args == ("native",)
    on_unsupported.assert_called_once()


@pytest.mark.asyncio
async def test_run_token_only_propagates_non_unsupported_errors() -> None:
    error = _upstream_error(500)
    send = AsyncMock(side_effect=error)

    with pytest.raises(UpstreamHTTPError) as raised:
        await run_token_only_or_fallback("token-only", "native", send)

    assert raised.value is error
    send.assert_awaited_once_with("token-only")


@pytest.mark.asyncio
async def test_run_token_only_does_not_fallback_for_protected_tokens() -> None:
    error = _upstream_error(404)
    send = AsyncMock(side_effect=error)

    with pytest.raises(UpstreamHTTPError) as raised:
        await run_token_only_or_fallback("token-only", "native", send, allow_fallback=False)

    assert raised.value is error
    send.assert_awaited_once_with("token-only")


@pytest.mark.asyncio
async def test_finish_token_only_deobfuscates_before_derender() -> None:
    render_client = AsyncMock()
    render_client.derender.return_value = {"choices": [{"index": 0, "text": "ok"}]}
    obfuscation_service = Mock()
    obfuscation_service.deobfuscate_generate_responses.return_value = [
        {"choices": [{"index": 0, "token_ids": [11, 12]}]}
    ]
    api_path = "/v1/completions".strip("/")
    req_info = RequestInfo(
        req_id="request",
        req_data={"model": "model", "prompt": "hello"},
        req_len=10,
        api=api_path,
        entry_api=api_path,
        tokenized_requests=[
            TokenizedRequest(
                prompt_token_ids=[1, 2],
                engine_prompt_token_ids=[101, 102],
                tokenizer_source=TokenizerSource.RENDER,
            )
        ],
    )
    req_info._token_obfuscation_service = obfuscation_service
    generated = [{"choices": [{"index": 0, "token_ids": [111, 112]}]}]

    response = await finish_token_only_response(render_client, req_info, generated)

    obfuscation_service.deobfuscate_generate_responses.assert_called_once_with(generated)
    derender_payload = render_client.derender.await_args.args[1]
    assert derender_payload["generate_responses"][0]["choices"][0]["token_ids"] == [
        11,
        12,
    ]
    assert response["choices"][0]["token_ids"] == [11, 12]


@pytest.mark.asyncio
async def test_finish_token_only_preserves_chat_template_kwargs_for_derender() -> None:
    render_client = AsyncMock()
    render_client.derender.return_value = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}
    api_path = "v1/chat/completions"
    req_info = RequestInfo(
        req_id="request",
        req_data={
            "model": "model",
            "messages": [{"role": "user", "content": "hello"}],
            "chat_template_kwargs": {"enable_thinking": False},
        },
        req_len=2,
        api=api_path,
        entry_api=api_path,
        tokenized_requests=[
            TokenizedRequest(
                prompt_token_ids=[1, 2],
                tokenizer_source=TokenizerSource.RENDER,
            )
        ],
    )

    await finish_token_only_response(
        render_client,
        req_info,
        [{"choices": [{"index": 0, "token_ids": [11, 12]}]}],
    )

    derender_payload = render_client.derender.await_args.args[1]
    assert derender_payload["chat_request"]["chat_template_kwargs"] == {"enable_thinking": False}
