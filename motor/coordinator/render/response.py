# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Shared non-streaming response assembly for token-only vLLM requests."""

from http import HTTPStatus
from typing import Any

from fastapi import HTTPException

from motor.coordinator.render.api_spec import get_render_api_spec
from motor.coordinator.render.models import TokenizedRequest
from motor.coordinator.render.vllm_render_client import (
    RenderInvalidResponseError,
    RenderTimeoutError,
    RenderUnavailableError,
    RenderUnsupportedError,
    VLLMRenderClient,
)


async def derender_response(
    render_client: VLLMRenderClient,
    *,
    api: str,
    request_id: str,
    model: str | None,
    tokenized_requests: list[TokenizedRequest],
    request_data: dict[str, Any],
    generate_responses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Convert ordered token-only GenerateResponses into one OpenAI response."""
    spec = get_render_api_spec(api)
    if spec is None:
        raise RuntimeError(f"Unsupported Derender API: {api}")
    prompt_token_ids = [list(item.prompt_token_ids) for item in tokenized_requests]
    if spec.supports_prompt_batch:
        generate_payload: dict[str, Any] | list[dict[str, Any]] = generate_responses
        prompt_tokens: int | list[int] = [len(token_ids) for token_ids in prompt_token_ids]
        response_prompt_token_ids: list[int] | list[list[int]] = prompt_token_ids
    else:
        if len(prompt_token_ids) != 1 or len(generate_responses) != 1:
            raise RuntimeError("Chat Derender requires exactly one prompt and GenerateResponse")
        generate_payload = generate_responses[0]
        prompt_tokens = len(prompt_token_ids[0])
        response_prompt_token_ids = prompt_token_ids[0]
    payload = {
        "stream": False,
        "model": model,
        spec.generate_payload_field: generate_payload,
        "prompt_tokens": prompt_tokens,
        spec.request_payload_field: request_data.copy(),
    }

    try:
        response = await render_client.derender(api, payload)
    except RenderTimeoutError as error:
        raise HTTPException(status_code=HTTPStatus.GATEWAY_TIMEOUT, detail=str(error)) from error
    except RenderUnsupportedError as error:
        raise HTTPException(status_code=HTTPStatus.NOT_IMPLEMENTED, detail=str(error)) from error
    except (RenderUnavailableError, RenderInvalidResponseError) as error:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(error)) from error

    response["id"] = request_id
    response["prompt_token_ids"] = response_prompt_token_ids
    generated_choices = [
        choice for generate_response in generate_responses for choice in (generate_response.get("choices") or [])
    ]
    for index, choice in enumerate(response.get("choices") or []):
        if index < len(generated_choices) and isinstance(choice, dict):
            choice["token_ids"] = list(generated_choices[index]["token_ids"])
    return response
