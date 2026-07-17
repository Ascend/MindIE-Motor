# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from motor.engine_server.core.health_collector import HealthCollector


def _make_endpoint_config(
    host: str = "127.0.0.1",
    port: int = 8000,
    timeout: int = 5,
    max_attempts: int = 3,
):
    health_check_config = SimpleNamespace(
        health_collector_timeout=timeout,
        health_collector_timeout_retry_attempts=max_attempts,
    )
    deploy_config = SimpleNamespace(
        infer_tls_config=None,
        health_check_config=health_check_config,
    )
    return SimpleNamespace(host=host, port=port, deploy_config=deploy_config)


def _mock_async_client(mock_create_client, *, get_side_effect=None, get_return_value=None):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=get_side_effect, return_value=get_return_value)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_create_client.return_value = mock_client
    return mock_client


def _healthy_response(body: bytes = b"true"):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.aread = AsyncMock(return_value=body)
    return response


@pytest.mark.asyncio
@patch("motor.engine_server.core.health_collector.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.health_collector.AsyncSafeHTTPSClient.create_client")
async def test_is_healthy_success_first_attempt(mock_create_client, _mock_restored):
    mock_client = _mock_async_client(mock_create_client, get_return_value=_healthy_response(b"true"))
    collector = HealthCollector(_make_endpoint_config())

    assert await collector.is_healthy() is True
    assert collector._has_connected is True
    assert mock_client.get.await_count == 1
    mock_create_client.assert_called_once_with(
        address="127.0.0.1:8000",
        tls_config=None,
        timeout=5,
    )


@pytest.mark.asyncio
@patch("motor.engine_server.core.health_collector.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.health_collector.AsyncSafeHTTPSClient.create_client")
async def test_is_healthy_false_body(mock_create_client, _mock_restored):
    _mock_async_client(mock_create_client, get_return_value=_healthy_response(b"false"))
    collector = HealthCollector(_make_endpoint_config())

    assert await collector.is_healthy() is False
    assert collector._has_connected is True


@pytest.mark.asyncio
@patch("motor.engine_server.core.health_collector.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.health_collector.AsyncSafeHTTPSClient.create_client")
async def test_is_healthy_retries_on_timeout_then_succeeds(mock_create_client, _mock_restored):
    mock_client = _mock_async_client(
        mock_create_client,
        get_side_effect=[
            httpx.TimeoutException("timeout-1"),
            httpx.TimeoutException("timeout-2"),
            _healthy_response(b"true"),
        ],
    )
    collector = HealthCollector(_make_endpoint_config(max_attempts=3))

    assert await collector.is_healthy() is True
    assert collector._has_connected is True
    assert mock_client.get.await_count == 3


@pytest.mark.asyncio
@patch("motor.engine_server.core.health_collector.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.health_collector.AsyncSafeHTTPSClient.create_client")
async def test_is_healthy_timeout_exhausted_never_connected_raises(mock_create_client, _mock_restored):
    mock_client = _mock_async_client(
        mock_create_client,
        get_side_effect=httpx.TimeoutException("timeout"),
    )
    collector = HealthCollector(_make_endpoint_config(max_attempts=3))

    with pytest.raises(httpx.TimeoutException):
        await collector.is_healthy()
    assert collector._has_connected is False
    assert mock_client.get.await_count == 3


@pytest.mark.asyncio
@patch("motor.engine_server.core.health_collector.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.health_collector.AsyncSafeHTTPSClient.create_client")
async def test_is_healthy_timeout_exhausted_after_connected_returns_false(mock_create_client, _mock_restored):
    mock_client = _mock_async_client(
        mock_create_client,
        get_side_effect=[
            _healthy_response(b"true"),
            httpx.TimeoutException("timeout"),
            httpx.TimeoutException("timeout"),
            httpx.TimeoutException("timeout"),
        ],
    )
    collector = HealthCollector(_make_endpoint_config(max_attempts=3))

    assert await collector.is_healthy() is True
    assert await collector.is_healthy() is False
    assert collector._has_connected is True
    # 1 success + 3 timeout attempts
    assert mock_client.get.await_count == 4


@pytest.mark.asyncio
@patch("motor.engine_server.core.health_collector.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.health_collector.AsyncSafeHTTPSClient.create_client")
async def test_is_healthy_respects_custom_max_attempts(mock_create_client, _mock_restored):
    mock_client = _mock_async_client(
        mock_create_client,
        get_side_effect=httpx.TimeoutException("timeout"),
    )
    collector = HealthCollector(_make_endpoint_config(max_attempts=1))

    with pytest.raises(httpx.TimeoutException):
        await collector.is_healthy()
    assert collector.max_attempts == 1
    assert mock_client.get.await_count == 1


@pytest.mark.asyncio
@patch("motor.engine_server.core.health_collector.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.health_collector.AsyncSafeHTTPSClient.create_client")
async def test_is_healthy_non_timeout_error_does_not_retry(mock_create_client, _mock_restored):
    mock_client = _mock_async_client(
        mock_create_client,
        get_side_effect=httpx.ConnectError("connection refused"),
    )
    collector = HealthCollector(_make_endpoint_config())

    with pytest.raises(httpx.ConnectError):
        await collector.is_healthy()
    assert mock_client.get.await_count == 1
    assert collector._has_connected is False


@pytest.mark.asyncio
@patch("motor.engine_server.core.health_collector.is_restored_from_host_side_snapshot", return_value=False)
@patch("motor.engine_server.core.health_collector.AsyncSafeHTTPSClient.create_client")
async def test_is_healthy_non_timeout_error_after_connected_returns_false(mock_create_client, _mock_restored):
    mock_client = _mock_async_client(
        mock_create_client,
        get_side_effect=[
            _healthy_response(b"true"),
            httpx.ConnectError("connection refused"),
        ],
    )
    collector = HealthCollector(_make_endpoint_config())

    assert await collector.is_healthy() is True
    assert await collector.is_healthy() is False
    assert mock_client.get.await_count == 2


@pytest.mark.asyncio
@patch("motor.engine_server.core.health_collector.get_pod_ip", return_value="10.0.0.8")
@patch("motor.engine_server.core.health_collector.is_restored_from_host_side_snapshot", return_value=True)
@patch("motor.engine_server.core.health_collector.AsyncSafeHTTPSClient.create_client")
async def test_is_healthy_refreshes_address_after_snapshot_restore(mock_create_client, _mock_restored, _mock_pod_ip):
    _mock_async_client(mock_create_client, get_return_value=_healthy_response(b"true"))
    collector = HealthCollector(_make_endpoint_config(host="127.0.0.1", port=8000))

    assert await collector.is_healthy() is True
    assert collector.address == "10.0.0.8:8000"
    assert collector._has_refreshed_after_restored is True
    mock_create_client.assert_called_once_with(
        address="10.0.0.8:8000",
        tls_config=None,
        timeout=5,
    )
