# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Shared vLLM token-only request selection and response handling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, TypeVar

from motor.common.logger import get_logger
from motor.coordinator.render.api_spec import get_render_api_spec
from motor.coordinator.render.models import TokenizedRequest, TokenizerSource
from motor.coordinator.render.response import derender_response
from motor.coordinator.render.vllm_render_client import VLLMRenderClient
from motor.coordinator.router.adapters.pd_protocol import (
    EngineLegSpec,
    EnginePhase,
    EngineProtocolError,
    EngineRequest,
    GenerationConstraint,
    KVTransferDescriptor,
    LegContext,
    VllmProtocolAdapter,
)
from motor.coordinator.router.upstream_error import UpstreamHTTPError

if TYPE_CHECKING:
    from motor.coordinator.models.request import RequestInfo

logger = get_logger(__name__)

_TOKEN_ONLY_UNSUPPORTED_STATUS = frozenset({HTTPStatus.NOT_FOUND, HTTPStatus.NOT_IMPLEMENTED})
_T = TypeVar("_T")
_R = TypeVar("_R")


def select_token_only_requests(
    req_info: RequestInfo,
    render_client: VLLMRenderClient | None,
    *,
    allow_streaming: bool = False,
    require_render_client: bool = True,
) -> list[TokenizedRequest]:
    """Return the ordered Render batch when the requested token-only path is supported."""
    requests = req_info.tokenized_requests
    if (require_render_client and render_client is None) or not requests:
        return []
    spec = get_render_api_spec(req_info.effective_entry_api())
    if spec is None:
        return []
    if any(item.tokenizer_source is not TokenizerSource.RENDER for item in requests):
        return []
    if not spec.supports_prompt_batch and len(requests) != 1:
        return []
    if req_info.req_data.get("stream", False) and not allow_streaming:
        return []
    return requests


def active_token_only_request(
    requests: Sequence[TokenizedRequest],
    prompt_index: int | None = None,
) -> TokenizedRequest | None:
    """Return the indexed batch item, or the only item for a singular request."""
    if prompt_index is not None:
        return requests[prompt_index] if 0 <= prompt_index < len(requests) else None
    return requests[0] if len(requests) == 1 else None


def token_only_request_id(
    request_id: str,
    *,
    prompt_index: int | None = None,
    attempt_seq: int | None = None,
) -> str:
    """Build a stable per-prompt engine request ID."""
    if attempt_seq is not None:
        request_id = f"{request_id}#a{attempt_seq}"
    if prompt_index is not None:
        request_id = f"{request_id}#p{prompt_index}"
    return request_id


def build_token_only_batch(
    adapter: VllmProtocolAdapter,
    tokenized_requests: list[TokenizedRequest],
    leg_factory: Callable[[int], EngineLegSpec],
) -> list[EngineRequest]:
    """Build an ordered engine batch without exposing topology names to Render data."""
    return [
        adapter.build_tokenized_request(
            tokenized.prompt_token_ids,
            tokenized.metadata,
            leg_factory(index),
        )
        for index, tokenized in enumerate(tokenized_requests)
    ]


def require_kv_transfer(
    params: Mapping[str, Any] | None,
    *,
    phase: EnginePhase,
) -> KVTransferDescriptor:
    """Build a required KV descriptor without allowing an incomplete engine leg."""
    if not params:
        raise EngineProtocolError(
            engine_type=VllmProtocolAdapter.engine_type,
            phase=phase.value,
            message="Missing kv_transfer_params",
        )
    return KVTransferDescriptor(params)


def build_trigger_token_only_decode_request(
    adapter: VllmProtocolAdapter,
    tokenized: TokenizedRequest,
    context: LegContext,
    metaserver_url: str,
) -> EngineRequest:
    """Translate Trigger decode state into the shared token-only engine contract."""
    return adapter.build_tokenized_request(
        tokenized.prompt_token_ids,
        tokenized.metadata,
        EngineLegSpec(
            context=context,
            phase=EnginePhase.DECODE,
            kv_transfer=KVTransferDescriptor(
                {
                    "do_remote_decode": False,
                    "do_remote_prefill": True,
                    "metaserver": metaserver_url,
                }
            ),
        ),
    )


def build_trigger_token_only_prefill_request(
    adapter: VllmProtocolAdapter,
    tokenized: TokenizedRequest,
    context: LegContext,
    kv_transfer_params: Mapping[str, Any],
) -> EngineRequest:
    """Translate a Trigger callback into the shared token-only engine contract."""
    params = deepcopy(dict(kv_transfer_params))
    params["do_remote_decode"] = True
    params["do_remote_prefill"] = False
    params.pop("metaserver", None)
    return adapter.build_tokenized_request(
        tokenized.prompt_token_ids,
        tokenized.metadata,
        EngineLegSpec(
            context=context,
            phase=EnginePhase.PREFILL,
            generation=GenerationConstraint(max_tokens=1, min_tokens=1),
            kv_transfer=KVTransferDescriptor(params),
        ),
    )


def is_token_only_unsupported(error: UpstreamHTTPError) -> bool:
    """Return whether the engine explicitly lacks the token-only endpoint."""
    return error.status_code in _TOKEN_ONLY_UNSUPPORTED_STATUS


async def run_token_only_or_fallback(
    token_only: _T | None,
    fallback: _T,
    send: Callable[[_T], Awaitable[_R]],
    *,
    on_unsupported: Callable[[UpstreamHTTPError], None] | None = None,
) -> tuple[_R, bool]:
    """Run token-only first and replay natively only for an explicit unsupported response."""
    if token_only is None:
        return await send(fallback), False
    try:
        return await send(token_only), True
    except UpstreamHTTPError as error:
        if not is_token_only_unsupported(error):
            raise
        if on_unsupported is not None:
            on_unsupported(error)
        return await send(fallback), False


async def gather_generate_responses(
    requests: list[EngineRequest],
    send: Callable[[EngineRequest], Awaitable[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Submit one GenerateRequest per prompt, cancelling siblings on failure."""
    tasks = [asyncio.create_task(send(request)) for request in requests]
    if not tasks:
        return []
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done)
        return [task.result() for task in tasks]
    finally:
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)


async def finish_token_only_response(
    render_client: VLLMRenderClient,
    req_info: RequestInfo,
    generate_responses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate ordered GenerateResponses and convert them into one OpenAI response."""
    adapter = VllmProtocolAdapter()
    validated = [adapter.validate_tokenized_decode_response(response) for response in generate_responses]
    response = await derender_response(
        render_client,
        api=req_info.effective_entry_api(),
        request_id=req_info.req_id,
        model=req_info.req_data.get("model"),
        tokenized_requests=req_info.tokenized_requests,
        request_data=req_info.req_data,
        generate_responses=validated,
    )
    logger.info(
        "token-only generate and derender success request_id=%s prompt_count=%d output_count=%d",
        req_info.req_id,
        len(req_info.tokenized_requests),
        sum(len(item.get("choices") or []) for item in validated),
    )
    return response
