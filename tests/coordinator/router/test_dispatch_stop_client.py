# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from motor.common.resources.dispatch import DispatchStopReason, DispatchStopState
from motor.common.resources.endpoint import Endpoint, EndpointStatus
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.router.dispatch_session import AttemptContext
from motor.coordinator.router.stop_client import DispatchStopClient


def _config():
    cfg = MagicMock()
    cfg.infer_tls_config = None
    return cfg


def _resource(engine_type: str) -> ScheduledResource:
    endpoint = Endpoint(
        id=1,
        ip="127.0.0.1",
        business_port="8000",
        mgmt_port="1026",
        status=EndpointStatus.NORMAL,
    )
    instance = Instance(
        job_name="job-1",
        model_name="m",
        engine_type=engine_type,
        id=1,
        role=PDRole.ROLE_D,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
        endpoints={endpoint.ip: {endpoint.id: endpoint}},
    )
    return ScheduledResource(instance=instance, endpoint=endpoint)


@pytest.mark.asyncio
async def test_dispatch_stop_uses_sglang_abort_request():
    client = DispatchStopClient(_config())
    attempt = AttemptContext(root_request_id="r1", attempt_seq=1, pair_id="p1")
    mock_http = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_http.post = AsyncMock(return_value=mock_response)

    with patch("motor.coordinator.router.stop_client.HTTPClientPool") as pool_cls:
        pool_cls.return_value.get_client = AsyncMock(return_value=mock_http)
        result = await client.stop(
            _resource("sglang"),
            attempt,
            DispatchStopReason.PEER_FAILED,
        )

    assert result is not None
    assert result.accepted is True
    assert result.state == DispatchStopState.STOPPED
    mock_http.post.assert_awaited_once()
    assert mock_http.post.await_args.args[0] == "/abort_request"
    assert mock_http.post.await_args.kwargs["json"] == {"rid": "r1#a1"}


@pytest.mark.asyncio
async def test_dispatch_stop_posts_for_non_sglang():
    client = DispatchStopClient(_config())
    attempt = AttemptContext(root_request_id="r1", attempt_seq=1, pair_id="p1")
    mock_http = AsyncMock()
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "root_request_id": "r1",
        "attempt_seq": 1,
        "accepted": True,
        "state": DispatchStopState.STOPPED.value,
        "message": "",
    }
    mock_http.post = AsyncMock(return_value=mock_response)

    with patch("motor.coordinator.router.stop_client.HTTPClientPool") as pool_cls:
        pool_cls.return_value.get_client = AsyncMock(return_value=mock_http)
        result = await client.stop(
            _resource("vllm"),
            attempt,
            DispatchStopReason.PEER_FAILED,
        )

    assert result is not None
    mock_http.post.assert_awaited_once()
    assert mock_http.post.await_args.args[0] == "/v1/dispatch/stop"
