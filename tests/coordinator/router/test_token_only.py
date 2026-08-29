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

from unittest.mock import AsyncMock, Mock

import pytest

from motor.coordinator.render.models import TokenizedRequest, TokenizerSource
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
    build_token_only_batch,
    build_trigger_token_only_decode_request,
    build_trigger_token_only_prefill_request,
    require_kv_transfer,
    run_token_only_or_fallback,
)
from motor.coordinator.router.upstream_error import UpstreamHTTPError


def test_build_token_only_batch_preserves_order_and_keeps_topology_in_leg_factory() -> None:
    tokenized_requests = [
        TokenizedRequest(
            prompt_token_ids=[index],
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

    assert [request.body["token_ids"] for request in requests] == [[10], [20]]
    assert [request.body["request_id"] for request in requests] == ["request#p0", "request#p1"]


def test_active_token_only_request_uses_explicit_prompt_index() -> None:
    requests = [
        TokenizedRequest(prompt_token_ids=[index], tokenizer_source=TokenizerSource.RENDER) for index in (10, 20)
    ]

    assert active_token_only_request(requests) is None
    assert active_token_only_request(requests, 1) is requests[1]
    assert active_token_only_request(requests, 2) is None
    assert active_token_only_request(requests[:1]) is requests[0]


@pytest.mark.parametrize(
    ("builder", "expected_kv", "expected_max_tokens"),
    [
        (
            lambda adapter, tokenized, context: build_trigger_token_only_decode_request(
                adapter,
                tokenized,
                context,
                "http://coordinator/v1/metaserver",
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
async def test_run_token_only_falls_back_only_when_endpoint_is_unsupported(status_code) -> None:
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
