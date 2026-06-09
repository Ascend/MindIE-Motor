# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest import mock
import asyncio
import httpx
import time
import pytest
from motor.engine_server.core.sim_inference import SimInference
from motor.engine_server.constants import constants

# pylint: disable=redefined-outer-name


@pytest.fixture
def mock_args():
    """Create mock arguments"""
    args = mock.MagicMock()
    args.host = "localhost"
    args.port = 8000
    args.served_model_name = ["test-model"]
    return args


@pytest.fixture
def mock_tls_config():
    """Create mock TLS configuration"""
    tls_config = mock.MagicMock()
    tls_config.tls_enable = False
    return tls_config


@pytest.fixture
def sim_inference(mock_args, mock_tls_config):
    """Create SimInference instance with health check enabled"""
    # Create a mock health_check_config with enable_virtual_inference=True
    mock_health_config = mock.MagicMock()
    mock_health_config.npu_usage_threshold = 3
    mock_health_config.enable_virtual_inference = True
    return SimInference(mock_args, mock_tls_config, mock_health_config)


def test_init(sim_inference, mock_args, mock_tls_config):
    """Test initialization functionality"""
    assert sim_inference.args == mock_args
    assert sim_inference.infer_tls_config == mock_tls_config
    assert sim_inference._status == constants.INIT_STATUS
    assert not sim_inference.is_abnormal()
    assert sim_inference._health_check_task is None


def test_set_status(sim_inference):
    """Test status setting functionality"""
    sim_inference.set_status(constants.NORMAL_STATUS)
    assert sim_inference._status == constants.NORMAL_STATUS

    sim_inference.set_status(constants.ABNORMAL_STATUS)
    assert sim_inference._status == constants.ABNORMAL_STATUS


def test_is_abnormal_initial(sim_inference):
    """Test if initial status is normal"""
    assert not sim_inference.is_abnormal()


def test_set_abnormal_status(sim_inference):
    """Test abnormal status setting functionality"""
    sim_inference.set_abnormal_status()
    assert sim_inference.is_abnormal()


def test_reset_abnormal_status(sim_inference):
    """Test abnormal status reset functionality"""
    sim_inference.set_abnormal_status()
    assert sim_inference.is_abnormal()

    sim_inference.reset_abnormal_status()
    assert not sim_inference.is_abnormal()


@pytest.mark.asyncio
@mock.patch('motor.common.http.http_client.AsyncSafeHTTPSClient.create_client')
async def test_send_virtual_request_async_success(mock_create_client, sim_inference):
    """Test successful virtual request sending"""
    # Mock client and response
    mock_client = mock.MagicMock()
    mock_response = mock.MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {
        "id": "test-id",
        "object": "text_completion",
        "created": 1234567890,
        "model": "test-model",
        "choices": [{"text": "Hello", "index": 0, "logprobs": None, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    # 使用AsyncMock来模拟异步方法
    mock_client.post = mock.AsyncMock(return_value=mock_response)
    mock_client.is_closed = False

    # Make create_client return the mock client directly
    mock_create_client.return_value = mock_client

    timeout = httpx.Timeout(5.0)
    await sim_inference.send_virtual_request_async(timeout)

    # Verify client creation and post method call
    mock_create_client.assert_called_once_with(
        address="localhost:8000", tls_config=sim_inference.infer_tls_config, timeout=timeout
    )
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "/v1/completions"
    assert call_args[1]["json"] == {"model": "test-model", "prompt": "1", "max_tokens": 1}
    assert 'Content-Type' in call_args[1]["headers"]
    assert call_args[1]["headers"]['Content-Type'] == 'application/json'
    assert 'X-Request-Id' in call_args[1]["headers"]
    assert call_args[1]["timeout"] == timeout


@pytest.mark.asyncio
@mock.patch('motor.common.http.http_client.AsyncSafeHTTPSClient.create_client')
async def test_send_virtual_request_async_http_error(mock_create_client, sim_inference):
    """Test virtual request sending with HTTP error"""
    # Mock HTTP error
    mock_client = mock.MagicMock()
    mock_response = mock.MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404 Not Found", request=mock.MagicMock(), response=mock_response
    )

    # 使用AsyncMock来模拟异步方法
    mock_client.post = mock.AsyncMock(return_value=mock_response)
    mock_client.is_closed = False

    # Make create_client return the mock client directly
    mock_create_client.return_value = mock_client

    timeout = httpx.Timeout(5.0)
    with pytest.raises(httpx.HTTPStatusError):
        await sim_inference.send_virtual_request_async(timeout)


@pytest.mark.asyncio
@mock.patch('motor.common.http.http_client.AsyncSafeHTTPSClient.create_client')
async def test_send_virtual_request_async_request_error(mock_create_client, sim_inference):
    """Test virtual request sending with request error"""
    # Mock request error
    mock_client = mock.MagicMock()

    # 使用AsyncMock来模拟异步方法并设置异常
    mock_client.post = mock.AsyncMock(side_effect=httpx.RequestError("Connection error"))
    mock_client.is_closed = False

    # Make create_client return the mock client directly
    mock_create_client.return_value = mock_client

    timeout = httpx.Timeout(5.0)
    with pytest.raises(httpx.RequestError):
        await sim_inference.send_virtual_request_async(timeout)


@mock.patch('motor.engine_server.core.sim_inference.asyncio.create_task')
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
def test_start_health_check(mock_thread, mock_create_task, sim_inference):
    """Test health check task startup functionality"""
    # Mock create_task return value
    mock_task = mock.MagicMock()
    mock_task.done.return_value = False
    mock_create_task.return_value = mock_task

    # Mock thread creation
    mock_thread_instance = mock.MagicMock()
    mock_thread.return_value = mock_thread_instance

    # Start health check
    sim_inference.start_health_check()

    # Verify thread was created and started
    assert mock_thread.call_count == 2


def test_stop_health_check(sim_inference):
    """Test health check task stop functionality"""
    # Create a mock task
    mock_task = mock.MagicMock()
    mock_task.done.return_value = False
    sim_inference._health_check_task = mock_task

    # Create a mock client
    mock_client = mock.MagicMock()
    mock_client.is_closed = False
    mock_client.aclose = mock.AsyncMock()
    sim_inference._client = mock_client

    # Stop health check
    sim_inference.stop_health_check()

    # Verify task was canceled
    mock_task.cancel.assert_called_once()
    # Verify abnormal status was reset
    assert not sim_inference.is_abnormal()


def test_generate_request_id(sim_inference):
    """Test request ID generation functionality"""
    # Test that the function returns a string
    request_id = sim_inference.generate_request_id()
    assert isinstance(request_id, str)

    # Test that the request ID contains '_virtual' suffix
    assert '_virtual' in request_id

    # Test that the request ID starts with a numeric timestamp
    timestamp_part = request_id.split('_')[0]
    assert timestamp_part.isdigit()

    # Test that two consecutive calls generate different IDs (due to timestamp)
    request_id1 = sim_inference.generate_request_id()
    time.sleep(0.001)  # Wait for a short time to ensure timestamp changes
    request_id2 = sim_inference.generate_request_id()
    assert request_id1 != request_id2


def test_generate_request_id_format(sim_inference):
    """Test request ID format"""
    with mock.patch('time.time', return_value=1234567890.123456):
        request_id = sim_inference.generate_request_id()
        assert request_id == '1234567890123456_virtual'


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch.object(SimInference, 'send_virtual_request_async')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_normal(mock_sleep, mock_send_request, mock_thread, sim_inference):
    """Test health check loop - normal case"""
    # Set status to normal
    sim_inference.set_status(constants.NORMAL_STATUS)

    # Mock thread creation and join
    mock_thread_instance = mock.MagicMock()
    mock_thread.return_value = mock_thread_instance
    mock_thread_instance.is_alive.return_value = False

    # Mock successful request sending
    mock_send_request.return_value = None

    # Mock sleep to raise exception to end loop
    mock_sleep.side_effect = asyncio.CancelledError

    # Verify loop executes normally
    with pytest.raises(asyncio.CancelledError):
        await sim_inference.health_check_loop()

    # Verify request was sent
    mock_send_request.assert_called_once()


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch.object(SimInference, 'send_virtual_request_async')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_abnormal(mock_sleep, mock_send_request, mock_thread, sim_inference):
    """Test health check loop - abnormal case"""
    # Set status to normal
    sim_inference.set_status(constants.NORMAL_STATUS)

    # Set max_failure_count to 1 for this test to avoid infinite loop
    sim_inference._max_failure_count = 1

    sim_inference._count_failure_flag = True

    # Mock thread creation and join
    mock_thread_instance = mock.MagicMock()
    mock_thread.return_value = mock_thread_instance
    mock_thread_instance.is_alive.return_value = False

    # Mock failed request sending
    mock_send_request.side_effect = Exception("Request failed")

    # Mock low AICore usage
    with sim_inference._shared_data_lock:
        sim_inference._max_aicore_usage = 2  # < 10%

    # Mock sleep to raise exception to end loop
    mock_sleep.side_effect = asyncio.CancelledError

    # Execute loop
    with pytest.raises(asyncio.CancelledError):
        await sim_inference.health_check_loop()

    # Verify abnormal status was set
    assert sim_inference.is_abnormal()


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch.object(SimInference, 'send_virtual_request_async')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_reset_abnormal(mock_sleep, mock_send_request, mock_thread, sim_inference):
    """Test health check loop - reset abnormal status"""
    # Set status to normal
    sim_inference.set_status(constants.NORMAL_STATUS)
    # First set to abnormal status
    sim_inference.set_abnormal_status()

    # Mock thread creation and join
    mock_thread_instance = mock.MagicMock()
    mock_thread.return_value = mock_thread_instance
    mock_thread_instance.is_alive.return_value = False

    # Mock successful request sending
    mock_send_request.return_value = None

    # Mock normal AICore usage
    with sim_inference._shared_data_lock:
        sim_inference._max_aicore_usage = 15  # > 10%

    # Mock sleep to raise exception to end loop
    mock_sleep.side_effect = asyncio.CancelledError

    # Execute loop
    with pytest.raises(asyncio.CancelledError):
        await sim_inference.health_check_loop()

    # Verify abnormal status was reset
    assert not sim_inference.is_abnormal()


def _mock_health_check_loop_thread(mock_thread):
    mock_thread_instance = mock.MagicMock()
    mock_thread.return_value = mock_thread_instance
    mock_thread_instance.is_alive.return_value = False


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_high_aicore_extends_sleep(mock_sleep, mock_thread, sim_inference):
    """AICore >= 80% should extend virtual inference sleep to 20 seconds."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    _mock_health_check_loop_thread(mock_thread)

    async def set_high_aicore_usage(timeout):
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 85

    with mock.patch.object(SimInference, 'send_virtual_request_async', side_effect=set_high_aicore_usage):
        mock_sleep.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await sim_inference.health_check_loop()

    assert sim_inference.sim_sleep == 20


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_low_aicore_keeps_default_sleep(mock_sleep, mock_thread, sim_inference):
    """AICore < 80% should keep virtual inference sleep at 5 seconds."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    _mock_health_check_loop_thread(mock_thread)

    async def set_low_aicore_usage(timeout):
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 50

    with mock.patch.object(SimInference, 'send_virtual_request_async', side_effect=set_low_aicore_usage):
        mock_sleep.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await sim_inference.health_check_loop()

    assert sim_inference.sim_sleep == 5


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_aicore_sleep_keeps_extended_after_moderate_load(
    mock_sleep, mock_thread, sim_inference
):
    """After high AICore, sleep stays at 20s when usage is between threshold and 80%."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    _mock_health_check_loop_thread(mock_thread)

    iteration = 0

    async def set_aicore_usage_by_iteration(timeout):
        nonlocal iteration
        iteration += 1
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 85 if iteration == 1 else 50

    with mock.patch.object(SimInference, 'send_virtual_request_async', side_effect=set_aicore_usage_by_iteration):
        mock_sleep.side_effect = [None, asyncio.CancelledError]
        with pytest.raises(asyncio.CancelledError):
            await sim_inference.health_check_loop()

    assert sim_inference.sim_sleep == 20


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_aicore_sleep_reverts_below_threshold(mock_sleep, mock_thread, sim_inference):
    """After high AICore, sleep reverts to 5s when usage drops below npu_usage_threshold."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    _mock_health_check_loop_thread(mock_thread)

    iteration = 0

    async def set_aicore_usage_by_iteration(timeout):
        nonlocal iteration
        iteration += 1
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 85 if iteration == 1 else 1

    with mock.patch.object(SimInference, 'send_virtual_request_async', side_effect=set_aicore_usage_by_iteration):
        mock_sleep.side_effect = [None, asyncio.CancelledError]
        with pytest.raises(asyncio.CancelledError):
            await sim_inference.health_check_loop()

    assert sim_inference.sim_sleep == 5
