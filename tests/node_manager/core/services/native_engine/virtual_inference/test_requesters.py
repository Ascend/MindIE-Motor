# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import time
from unittest import mock

import httpx
import pytest

from motor.common.resources.dispatch import DispatchProfile
from motor.common.resources.instance import PDRole
from motor.node_manager.core.services.native_engine.virtual_inference.requesters import (
    VllmCompletionsRequester,
    generate_request_id,
)
from motor.node_manager.core.services.native_engine.virtual_inference.spec import VirtualInferenceSpec

# pylint: disable=redefined-outer-name


def _make_spec(**overrides) -> VirtualInferenceSpec:
    values = {
        "instance_id": 1,
        "endpoint_id": 0,
        "host": "127.0.0.1",
        "port": 8000,
        "role": PDRole.ROLE_U,
        "engine_type": "vllm",
        "model_name": "test-model",
        "dispatch_profile": DispatchProfile.UNKNOWN,
        "tls_config": None,
        "enabled": True,
        "npu_usage_threshold": 3,
        "max_failure_count": 6,
    }
    values.update(overrides)
    return VirtualInferenceSpec(**values)


def _fake_client():
    client = mock.MagicMock()
    client.is_closed = False
    response = mock.MagicMock()
    response.raise_for_status.return_value = None
    return client, response


@pytest.mark.asyncio
async def test_vllm_requester_sends_light_completion():
    requester = VllmCompletionsRequester(_make_spec())
    client, response = _fake_client()
    client.post = mock.AsyncMock(return_value=response)

    await requester.send(client, httpx.Timeout(5.0))

    client.post.assert_called_once()
    call_args = client.post.call_args
    assert call_args[0][0] == "/v1/completions"
    assert call_args[1]["json"] == {"model": "test-model", "prompt": "1", "max_tokens": 1}
    assert call_args[1]["headers"]["Content-Type"] == "application/json"
    assert call_args[1]["headers"]["X-Request-Id"].endswith("_virtual")
    assert call_args[1]["timeout"] == httpx.Timeout(5.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role, dispatch_profile, expect_kv_params",
    [
        (PDRole.ROLE_D, DispatchProfile.TRIGGER, True),
        (PDRole.ROLE_D, DispatchProfile.HANDOFF, False),
        (PDRole.ROLE_P, DispatchProfile.TRIGGER, False),
    ],
    ids=["decode_trigger_has_kv", "decode_handoff_no_kv", "prefill_trigger_no_kv"],
)
async def test_vllm_requester_kv_transfer_params(role, dispatch_profile, expect_kv_params):
    requester = VllmCompletionsRequester(_make_spec(role=role, dispatch_profile=dispatch_profile))
    client, response = _fake_client()
    client.post = mock.AsyncMock(return_value=response)

    await requester.send(client, httpx.Timeout(5.0))

    body = client.post.call_args[1]["json"]
    if expect_kv_params:
        assert body["kv_transfer_params"] == {
            "do_remote_decode": False,
            "do_remote_prefill": True,
            "do_virtual": True,
        }
    else:
        assert "kv_transfer_params" not in body


def _http_status_error(msg):
    def _make(response):
        return httpx.HTTPStatusError(msg, request=mock.MagicMock(), response=response)

    return _make


def _request_error(msg):
    def _make(_response):
        return httpx.RequestError(msg)

    return _make


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory, exc_type, inject_via_raise_for_status",
    [
        (_http_status_error("500 Internal Server Error"), httpx.HTTPStatusError, True),
        (_request_error("Connection refused"), httpx.RequestError, False),
    ],
    ids=["http_status_error", "request_error"],
)
async def test_vllm_requester_propagates_errors(exc_factory, exc_type, inject_via_raise_for_status):
    requester = VllmCompletionsRequester(_make_spec())
    client, response = _fake_client()
    exc = exc_factory(response)
    if inject_via_raise_for_status:
        response.raise_for_status.side_effect = exc
        client.post = mock.AsyncMock(return_value=response)
    else:
        client.post = mock.AsyncMock(side_effect=exc)

    with pytest.raises(exc_type):
        await requester.send(client, httpx.Timeout(5.0))


def test_generate_request_id_format():
    request_id = generate_request_id()
    assert isinstance(request_id, str)
    assert "_virtual" in request_id
    assert request_id.split("_", maxsplit=1)[0].isdigit()

    with mock.patch("time.time", return_value=1234567890.123456):
        assert generate_request_id() == "1234567890123456_virtual"

    first = generate_request_id()
    time.sleep(0.001)
    assert generate_request_id() != first
