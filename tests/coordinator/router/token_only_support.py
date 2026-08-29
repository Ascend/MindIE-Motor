# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import AsyncMock

import httpx

from motor.common.resources.instance import PDRole
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.render.models import TokenizedRequest, TokenizerSource


def make_tokenized_requests(prompts, model, max_tokens):
    return [
        TokenizedRequest(
            prompt_token_ids=ids,
            tokenizer_source=TokenizerSource.RENDER,
            metadata={"model": model, "sampling_params": {"max_tokens": max_tokens}},
        )
        for ids in prompts
    ]


def make_render_request_info(request_id, request_data, api, prompt_token_ids, *, req_len=10):
    prompts = [prompt_token_ids] if prompt_token_ids and isinstance(prompt_token_ids[0], int) else prompt_token_ids
    tokenized_requests = make_tokenized_requests(
        prompts, request_data["model"], request_data.get("max_tokens", request_data.get("max_completion_tokens", 1))
    )
    return RequestInfo(
        req_id=request_id,
        req_data=request_data,
        api=api,
        entry_api=api,
        req_len=req_len,
        token_ids=list(max(tokenized_requests, key=lambda x: len(x.prompt_token_ids)).prompt_token_ids),
        tokenized_requests=tokenized_requests,
    )


def make_prefill_response(body):
    return {
        "kv_transfer_params": {
            "do_remote_prefill": True,
            "remote_request_id": body["request_id"],
            "remote_host": "10.0.0.1",
            "remote_port": 9000,
            "connector_private": {"opaque": "kv"},
        },
        "usage": {"prompt_tokens": len(body.get("token_ids") or []), "prompt_tokens_details": {"cached_tokens": 0}},
    }


def make_generate_response(body, output_token_ids=None):
    token_ids = list(output_token_ids or [30, 31])
    prompt_tokens = len(body.get("token_ids") or [])
    return {
        "request_id": body["request_id"],
        "choices": [{"index": 0, "token_ids": token_ids, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(token_ids),
            "total_tokens": prompt_tokens + len(token_ids),
        },
    }


def make_render_client(*, completion_count=1):
    client = AsyncMock()
    client.derender.return_value = {
        "object": "text_completion",
        "choices": [{"index": i, "text": f"result-{i}"} for i in range(completion_count)],
    }
    return client


class TokenOnlyEngineClient:
    def __init__(self, role: PDRole, unsupported_status=None, native_response=None):
        self.role = role
        self.unsupported_status = unsupported_status
        self.native_response = native_response or {
            "choices": [{"message": {"role": "assistant", "content": "fallback response"}}]
        }
        self.paths, self.requests, self.headers = [], [], []
        self.base_url = f"http://{role.value.lower()}"
        self.timeout = 1

    async def post(self, path, json=None, headers=None, timeout=None):
        del timeout
        body = json or {}
        self.paths.append(str(path))
        self.requests.append(body)
        self.headers.append(headers or {})
        request = httpx.Request("POST", path, headers=headers or {}, json=body)
        is_generate = str(path).endswith("/inference/v1/generate")
        if is_generate and self.unsupported_status is not None:
            return httpx.Response(self.unsupported_status, json={"detail": "unsupported"}, request=request)
        if self.role == PDRole.ROLE_P:
            return httpx.Response(200, json=make_prefill_response(body), request=request)
        if is_generate:
            return httpx.Response(200, json=make_generate_response(body), request=request)
        return httpx.Response(200, json=self.native_response, request=request)


def assert_generate_requests(requests, *, request_ids, prompt_token_ids):
    assert [request["request_id"] for request in requests] == request_ids
    assert [request["token_ids"] for request in requests] == prompt_token_ids


def assert_completion_derender(render_client, *, prompt_lengths, response_count, original_request):
    render_client.derender.assert_awaited_once()
    payload = render_client.derender.await_args.args[1]
    assert payload["prompt_tokens"] == prompt_lengths
    assert len(payload["generate_responses"]) == response_count
    assert payload["completion_request"] == original_request
