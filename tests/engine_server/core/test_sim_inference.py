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
import contextlib
import httpx
import sys
import threading
import time
import types
import pytest
from motor.common.resources.dispatch import DispatchProfile
from motor.engine_server.core.sim_inference import (
    SimInference,
    _AICORE_SAMPLE_WINDOW_SEC,
    _VIRTUAL_WARMUP_TIMEOUT_SEC,
    _is_virtual_metrics_request,
)
from motor.engine_server.constants import constants

# pylint: disable=redefined-outer-name


@pytest.fixture
def finish_reason(monkeypatch):
    """Provide FinishReason without requiring vllm to be installed."""

    class FinishReason:
        LENGTH = 1
        STOP = 2

    fake_engine = types.ModuleType("vllm.v1.engine")
    fake_engine.FinishReason = FinishReason
    fake_v1 = types.ModuleType("vllm.v1")
    fake_v1.engine = fake_engine
    fake_vllm = types.ModuleType("vllm")
    fake_vllm.v1 = fake_v1
    monkeypatch.setitem(sys.modules, "vllm", fake_vllm)
    monkeypatch.setitem(sys.modules, "vllm.v1", fake_v1)
    monkeypatch.setitem(sys.modules, "vllm.v1.engine", fake_engine)
    return FinishReason


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
async def test_send_virtual_request_async_decode_layerwise_includes_kv_params(
    mock_create_client, mock_args, mock_tls_config
):
    """Layerwise decode virtual warmup injects trigger kv_transfer_params without metaserver."""
    mock_health_config = mock.MagicMock()
    mock_health_config.npu_usage_threshold = 3
    mock_health_config.enable_virtual_inference = True
    decode_sim_inference = SimInference(
        mock_args,
        mock_tls_config,
        mock_health_config,
        role=constants.DECODE_ROLE,
        dispatch_profile=DispatchProfile.TRIGGER,
    )

    mock_client = mock.MagicMock()
    mock_response = mock.MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"choices": [{"text": "Hello"}]}
    mock_client.post = mock.AsyncMock(return_value=mock_response)
    mock_client.is_closed = False
    mock_create_client.return_value = mock_client

    timeout = httpx.Timeout(5.0)
    await decode_sim_inference.send_virtual_request_async(timeout)

    call_args = mock_client.post.call_args
    assert call_args[1]["json"] == {
        "model": "test-model",
        "prompt": "1",
        "max_tokens": 1,
        "kv_transfer_params": {
            "do_remote_decode": False,
            "do_remote_prefill": True,
            "do_virtual": True,
        },
    }


@pytest.mark.asyncio
@mock.patch('motor.common.http.http_client.AsyncSafeHTTPSClient.create_client')
async def test_send_virtual_request_async_decode_handoff_skips_kv_transfer_params(
    mock_create_client, mock_args, mock_tls_config
):
    """Handoff decode virtual warmup must send a plain completion without kv_transfer_params."""
    mock_health_config = mock.MagicMock()
    mock_health_config.npu_usage_threshold = 3
    mock_health_config.enable_virtual_inference = True
    decode_sim_inference = SimInference(
        mock_args,
        mock_tls_config,
        mock_health_config,
        role=constants.DECODE_ROLE,
        dispatch_profile=DispatchProfile.HANDOFF,
    )

    mock_client = mock.MagicMock()
    mock_response = mock.MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_response.json.return_value = {"choices": [{"text": "Hello"}]}
    mock_client.post = mock.AsyncMock(return_value=mock_response)
    mock_client.is_closed = False
    mock_create_client.return_value = mock_client

    timeout = httpx.Timeout(5.0)
    await decode_sim_inference.send_virtual_request_async(timeout)

    call_args = mock_client.post.call_args
    assert call_args[1]["json"] == {"model": "test-model", "prompt": "1", "max_tokens": 1}


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

    _mock_health_check_loop_thread(mock_thread, sim_inference)

    # Mock successful request sending
    mock_send_request.return_value = None

    # Mock sleep to raise exception to end loop
    mock_sleep.side_effect = asyncio.CancelledError

    with _patched_health_check_loop(mock_thread, sim_inference):
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

    _mock_health_check_loop_thread(mock_thread, sim_inference)

    # Mock failed request sending and low AICore usage
    async def set_low_aicore_and_fail(timeout):
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 2  # < 10%
            sim_inference._aicore_usage_available = True
            sim_inference._aicore_completed_generation = sim_inference._aicore_requested_generation
        raise RuntimeError("Request failed")

    mock_send_request.side_effect = set_low_aicore_and_fail

    # Mock sleep to raise exception to end loop
    mock_sleep.side_effect = asyncio.CancelledError

    # Execute loop
    with _patched_health_check_loop(mock_thread, sim_inference):
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

    _mock_health_check_loop_thread(mock_thread, sim_inference)

    async def set_normal_aicore_on_virtual_request(timeout):
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 15  # > 10%
            sim_inference._aicore_usage_available = True
            sim_inference._aicore_completed_generation = sim_inference._aicore_requested_generation

    mock_send_request.side_effect = set_normal_aicore_on_virtual_request

    # Mock sleep to raise exception to end loop
    mock_sleep.side_effect = asyncio.CancelledError

    # Execute loop
    with _patched_health_check_loop(mock_thread, sim_inference):
        with pytest.raises(asyncio.CancelledError):
            await sim_inference.health_check_loop()

    # Verify abnormal status was reset
    assert not sim_inference.is_abnormal()


def _mock_health_check_loop_thread(mock_thread, sim_inference=None):
    mock_thread_instance = mock.MagicMock()
    mock_thread.return_value = mock_thread_instance
    mock_thread_instance.is_alive.return_value = False
    if sim_inference is not None:
        return _patch_parallel_aicore_wait(sim_inference)
    return None


def _patch_parallel_aicore_wait(sim_inference, sample_finished=True):
    sim_inference._virtual_warmup_done = True

    def sample_wait(timeout=None):
        if sample_finished:
            with sim_inference._shared_data_lock:
                sim_inference._aicore_completed_generation = sim_inference._aicore_requested_generation
        return sample_finished

    sim_inference._aicore_sample_done.wait = sample_wait

    async def fake_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    return fake_to_thread


@contextlib.contextmanager
def _patched_health_check_loop(mock_thread, sim_inference):
    fake_to_thread = _mock_health_check_loop_thread(mock_thread, sim_inference)
    if fake_to_thread is None:
        yield
    else:
        with mock.patch('motor.engine_server.core.sim_inference.asyncio.to_thread', fake_to_thread):
            yield


@mock.patch('motor.engine_server.core.sim_inference.get_aicore_usage')
def test_sample_aicore_usage_stops_after_first_error(mock_get_aicore, sim_inference):
    """A failed npu-smi read should not retry for the entire sample window."""
    mock_get_aicore.side_effect = RuntimeError("AI Core usage not found in npu-smi watch output (timeout)")

    result, available = sim_inference._sample_aicore_usage()

    assert result == 0
    assert available is False
    mock_get_aicore.assert_called_once()


@mock.patch('motor.engine_server.core.sim_inference.time.sleep')
@mock.patch('motor.engine_server.core.sim_inference.get_aicore_usage')
def test_sample_aicore_usage_reports_zero_when_idle(mock_get_aicore, _mock_sleep, sim_inference):
    """A successful npu-smi read of 0% should be treated as available."""
    mock_get_aicore.return_value = 0

    result, available = sim_inference._sample_aicore_usage()

    assert result == 0
    assert available is True
    assert mock_get_aicore.call_count >= 1


@pytest.mark.asyncio
@mock.patch.object(SimInference, 'send_virtual_request_async')
async def test_health_check_loop_does_not_block_beyond_sample_window(mock_send_request, sim_inference):
    """Virtual inference should wait at most the bounded AICore sample window."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    sim_inference._virtual_warmup_done = True
    mock_send_request.return_value = None

    observed_timeout = None

    def sample_wait(timeout=None):
        nonlocal observed_timeout
        observed_timeout = timeout
        return False

    sim_inference._aicore_sample_done.wait = sample_wait

    async def fake_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    start = time.monotonic()
    with mock.patch('motor.engine_server.core.sim_inference.asyncio.to_thread', fake_to_thread):
        with mock.patch(
            'motor.engine_server.core.sim_inference.asyncio.sleep',
            side_effect=asyncio.CancelledError,
        ):
            with pytest.raises(asyncio.CancelledError):
                await sim_inference.health_check_loop()

    elapsed = time.monotonic() - start
    assert elapsed < 1.0
    assert observed_timeout == _AICORE_SAMPLE_WINDOW_SEC


def test_read_aicore_sample_ignores_stale_generation(sim_inference):
    """Stale worker results must not be read after a newer sample was requested."""
    with sim_inference._shared_data_lock:
        sim_inference._max_aicore_usage = 42
        sim_inference._aicore_usage_available = True
        sim_inference._aicore_completed_generation = 1

    max_usage, available = sim_inference._read_aicore_sample(generation=2, sample_finished=True)

    assert max_usage == 0
    assert available is False


def test_read_aicore_sample_available_with_partial_sample(sim_inference):
    """Partial successful samples should be readable before the sample window finishes."""
    with sim_inference._shared_data_lock:
        sim_inference._max_aicore_usage = 15
        sim_inference._aicore_usage_available = True
        sim_inference._aicore_completed_generation = 2

    max_usage, available = sim_inference._read_aicore_sample(generation=2, sample_finished=False)

    assert max_usage == 15
    assert available is True


def test_read_aicore_sample_unavailable_when_window_timeout_without_data(sim_inference):
    """Finished sample window with no successful reads should remain unavailable."""
    with sim_inference._shared_data_lock:
        sim_inference._max_aicore_usage = 0
        sim_inference._aicore_usage_available = False
        sim_inference._aicore_completed_generation = 2

    max_usage, available = sim_inference._read_aicore_sample(generation=2, sample_finished=True)

    assert max_usage == 0
    assert available is False


def test_read_aicore_sample_returns_zero_when_waiting_for_data(sim_inference):
    """Waiting for first sample must not expose stale peak usage when unavailable."""
    with sim_inference._shared_data_lock:
        sim_inference._max_aicore_usage = 42
        sim_inference._aicore_usage_available = False
        sim_inference._aicore_completed_generation = 2

    max_usage, available = sim_inference._read_aicore_sample(generation=2, sample_finished=False)

    assert max_usage == 0
    assert available is False


@pytest.mark.asyncio
async def test_run_virtual_warmup_uses_3min_timeout(sim_inference):
    """Warmup should send the first virtual request with a 3 minute timeout."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    observed_timeouts = []

    async def capture_timeout(timeout):
        observed_timeouts.append(timeout)
        return True

    sim_inference._send_virtual_request_safe = capture_timeout

    result = await sim_inference._run_virtual_warmup()

    assert result is True
    assert sim_inference._virtual_warmup_done is True
    assert len(observed_timeouts) == 1
    assert observed_timeouts[0].read == _VIRTUAL_WARMUP_TIMEOUT_SEC


@pytest.mark.asyncio
async def test_run_virtual_warmup_marks_abnormal_on_failure(sim_inference):
    """Warmup failure should mark abnormal and stop before the regular loop."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    call_count = {"count": 0}

    async def fail_once(_timeout):
        call_count["count"] += 1
        return False

    sim_inference._send_virtual_request_safe = fail_once

    result = await sim_inference._run_virtual_warmup()

    assert result is False
    assert call_count["count"] == 1
    assert sim_inference._virtual_warmup_done is True
    assert sim_inference.is_abnormal()


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
async def test_health_check_loop_stops_after_warmup_failure(mock_thread, sim_inference):
    """Failed warmup should mark abnormal and not start the regular health check loop."""
    sim_inference.set_status(constants.NORMAL_STATUS)

    async def fail_warmup(_timeout):
        return False

    sim_inference._send_virtual_request_safe = fail_warmup

    with mock.patch.object(
        SimInference,
        '_trigger_aicore_sample',
        return_value=1,
    ) as mock_trigger:
        sim_inference._virtual_warmup_done = False
        await sim_inference.health_check_loop()

    assert sim_inference.is_abnormal()
    mock_trigger.assert_not_called()


@pytest.mark.asyncio
async def test_run_virtual_warmup_does_not_trigger_aicore_sampling(sim_inference):
    """Warmup must not trigger AICore sampling before the first successful request."""
    sim_inference.set_status(constants.NORMAL_STATUS)

    with mock.patch.object(
        SimInference,
        '_trigger_aicore_sample',
        autospec=True,
    ) as mock_trigger:
        sim_inference._send_virtual_request_safe = mock.AsyncMock(return_value=True)
        result = await sim_inference._run_virtual_warmup()

    assert result is True
    mock_trigger.assert_not_called()


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_triggers_aicore_after_warmup(mock_sleep, mock_thread, sim_inference):
    """Normal health check loop should trigger AICore sampling after warmup completes."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    sim_inference._virtual_warmup_done = True
    _mock_health_check_loop_thread(mock_thread, sim_inference)

    with mock.patch.object(SimInference, 'send_virtual_request_async', return_value=None):
        with mock.patch.object(
            SimInference,
            '_trigger_aicore_sample',
            return_value=1,
        ) as mock_trigger:
            mock_sleep.side_effect = asyncio.CancelledError
            with _patched_health_check_loop(mock_thread, sim_inference):
                with pytest.raises(asyncio.CancelledError):
                    await sim_inference.health_check_loop()

    mock_trigger.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_loop_runs_http_and_sampling_in_parallel(sim_inference):
    """HTTP and AICore sampling should overlap within a single health-check cycle."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    sim_inference._virtual_warmup_done = True

    activity_log = []
    sample_wait_scheduled = threading.Event()
    original_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, /, *args, **kwargs):
        sample_wait_scheduled.set()
        return await original_to_thread(func, *args, **kwargs)

    async def slow_virtual_request(_self, _timeout):
        activity_log.append(("http_start", time.monotonic()))
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                if sample_wait_scheduled.is_set():
                    break
                await asyncio.sleep(0.005)
            else:
                pytest.fail("AICore wait was never scheduled concurrently with HTTP")

            await asyncio.sleep(0.2)
        finally:
            activity_log.append(("http_end", time.monotonic()))
        return True

    def sample_wait_while_http_in_flight(timeout=None):
        activity_log.append(("wait_start", time.monotonic()))
        try:
            time.sleep(0.15)
        finally:
            activity_log.append(("wait_end", time.monotonic()))
        return True

    async def bound_slow_virtual_request(timeout):
        return await slow_virtual_request(sim_inference, timeout)

    sim_inference._send_virtual_request_safe = bound_slow_virtual_request
    sim_inference._aicore_sample_done.wait = sample_wait_while_http_in_flight

    real_sleep = asyncio.sleep

    async def cancel_only_sim_interval_sleep(delay):
        if delay >= sim_inference.sim_sleep:
            raise asyncio.CancelledError
        await real_sleep(delay)

    start = time.monotonic()
    with mock.patch('motor.engine_server.core.sim_inference.asyncio.to_thread', tracking_to_thread):
        with mock.patch(
            'motor.engine_server.core.sim_inference.asyncio.sleep',
            side_effect=cancel_only_sim_interval_sleep,
        ):
            with pytest.raises(asyncio.CancelledError):
                await sim_inference.health_check_loop()

    elapsed = time.monotonic() - start
    windows = dict(activity_log)
    assert {"http_start", "http_end", "wait_start", "wait_end"} <= windows.keys()
    assert windows["wait_start"] < windows["http_end"], "AICore wait must start before HTTP finishes"
    assert windows["http_start"] < windows["wait_end"], "HTTP must start before AICore wait finishes"
    assert elapsed < 0.35, "Serial execution would exceed the parallel time bound"


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch.object(SimInference, 'send_virtual_request_async')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_skip_failure_count_when_aicore_unavailable(
    mock_sleep, mock_send_request, mock_thread, sim_inference
):
    """Virtual request failure should not count when AICore sampling is unavailable."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    sim_inference._count_failure_flag = True
    sim_inference._max_failure_count = 1

    _mock_health_check_loop_thread(mock_thread, sim_inference)
    mock_send_request.side_effect = Exception("Request failed")
    mock_sleep.side_effect = asyncio.CancelledError

    with _patched_health_check_loop(mock_thread, sim_inference):
        with pytest.raises(asyncio.CancelledError):
            await sim_inference.health_check_loop()

    assert not sim_inference.is_abnormal()
    assert sim_inference._failure_count == 0


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_high_aicore_extends_sleep(mock_sleep, mock_thread, sim_inference):
    """AICore >= 80% should extend virtual inference sleep to 20 seconds."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    _mock_health_check_loop_thread(mock_thread, sim_inference)

    async def set_high_aicore_usage(timeout):
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 85
            sim_inference._aicore_usage_available = True
            sim_inference._aicore_completed_generation = sim_inference._aicore_requested_generation

    with mock.patch.object(SimInference, 'send_virtual_request_async', side_effect=set_high_aicore_usage):
        mock_sleep.side_effect = asyncio.CancelledError
        with _patched_health_check_loop(mock_thread, sim_inference):
            with pytest.raises(asyncio.CancelledError):
                await sim_inference.health_check_loop()

    assert sim_inference.sim_sleep == 20


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_low_aicore_keeps_default_sleep(mock_sleep, mock_thread, sim_inference):
    """AICore < 80% should keep virtual inference sleep at 5 seconds."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    _mock_health_check_loop_thread(mock_thread, sim_inference)

    async def set_low_aicore_usage(timeout):
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 50
            sim_inference._aicore_usage_available = True
            sim_inference._aicore_completed_generation = sim_inference._aicore_requested_generation

    with mock.patch.object(SimInference, 'send_virtual_request_async', side_effect=set_low_aicore_usage):
        mock_sleep.side_effect = asyncio.CancelledError
        with _patched_health_check_loop(mock_thread, sim_inference):
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
    _mock_health_check_loop_thread(mock_thread, sim_inference)

    iteration = 0

    async def set_aicore_usage_by_iteration(timeout):
        nonlocal iteration
        iteration += 1
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 85 if iteration == 1 else 50
            sim_inference._aicore_usage_available = True
            sim_inference._aicore_completed_generation = sim_inference._aicore_requested_generation

    with mock.patch.object(SimInference, 'send_virtual_request_async', side_effect=set_aicore_usage_by_iteration):
        mock_sleep.side_effect = [None, asyncio.CancelledError]
        with _patched_health_check_loop(mock_thread, sim_inference):
            with pytest.raises(asyncio.CancelledError):
                await sim_inference.health_check_loop()

    assert sim_inference.sim_sleep == 20


@pytest.mark.asyncio
@mock.patch('motor.engine_server.core.sim_inference.threading.Thread')
@mock.patch('motor.engine_server.core.sim_inference.asyncio.sleep')
async def test_health_check_loop_aicore_sleep_reverts_below_threshold(mock_sleep, mock_thread, sim_inference):
    """After high AICore, sleep reverts to 5s when usage drops below npu_usage_threshold."""
    sim_inference.set_status(constants.NORMAL_STATUS)
    _mock_health_check_loop_thread(mock_thread, sim_inference)

    iteration = 0

    async def set_aicore_usage_by_iteration(timeout):
        nonlocal iteration
        iteration += 1
        with sim_inference._shared_data_lock:
            sim_inference._max_aicore_usage = 85 if iteration == 1 else 1
            sim_inference._aicore_usage_available = True
            sim_inference._aicore_completed_generation = sim_inference._aicore_requested_generation

    with mock.patch.object(SimInference, 'send_virtual_request_async', side_effect=set_aicore_usage_by_iteration):
        mock_sleep.side_effect = [None, asyncio.CancelledError]
        with _patched_health_check_loop(mock_thread, sim_inference):
            with pytest.raises(asyncio.CancelledError):
                await sim_inference.health_check_loop()

    assert sim_inference.sim_sleep == 5


class _FakeStats:
    def __init__(self, num_generation_tokens: int):
        self.num_generation_tokens = num_generation_tokens


class _FakeReqState:
    def __init__(
        self,
        *,
        external_req_id: str | None = None,
        max_tokens_param: int | None = None,
        prompt_len: int = 1,
        num_generation_tokens: int = 1,
        request_id: str | None = None,
        lora_name: str | None = None,
        parent_req: object | None = None,
    ):
        self.external_req_id = external_req_id
        self.max_tokens_param = max_tokens_param
        self.prompt_len = prompt_len
        self.stats = _FakeStats(num_generation_tokens)
        self.request_id = request_id
        self.lora_name = lora_name
        self.parent_req = parent_req


def test_is_virtual_metrics_request_by_external_req_id():
    assert _is_virtual_metrics_request(_FakeReqState(external_req_id="cmpl-123_virtual"))
    assert not _is_virtual_metrics_request(_FakeReqState(external_req_id="cmpl-123-normal"))


def test_is_virtual_metrics_request_rejects_missing_external_req_id():
    assert not _is_virtual_metrics_request(_FakeReqState(external_req_id=None))


@mock.patch("motor.engine_server.core.sim_inference.importlib.import_module")
def test_patch_vllm_metrics_skips_virtual_update(mock_import_module, sim_inference, finish_reason):
    original_update = mock.MagicMock()
    mock_lora_states = mock.MagicMock()
    mock_parent_observe = mock.MagicMock()

    mock_module = mock.MagicMock()
    mock_module.OutputProcessor._update_stats_from_finished = original_update
    mock_module.ParentRequest.observe_finished_request = mock_parent_observe
    mock_import_module.return_value = mock_module

    sim_inference.patch_vllm_metrics()

    patched = mock_module.OutputProcessor._update_stats_from_finished
    processor = mock.MagicMock()
    processor.lora_states = mock_lora_states
    req_state = _FakeReqState(
        external_req_id="cmpl-42_virtual",
        request_id="internal-42",
        lora_name=None,
        parent_req=None,
    )
    iteration_stats = mock.MagicMock()

    patched(processor, req_state, finish_reason.LENGTH, iteration_stats)

    original_update.assert_not_called()
    mock_lora_states.request_finished.assert_called_once_with("internal-42", None)
    mock_parent_observe.assert_called_once()


@mock.patch("motor.engine_server.core.sim_inference.importlib.import_module")
def test_patch_vllm_metrics_delegates_non_virtual_request(mock_import_module, sim_inference, finish_reason):
    original_update = mock.MagicMock()
    mock_module = mock.MagicMock()
    mock_module.OutputProcessor._update_stats_from_finished = original_update
    mock_import_module.return_value = mock_module

    sim_inference.patch_vllm_metrics()

    patched = mock_module.OutputProcessor._update_stats_from_finished
    processor = mock.MagicMock()
    req_state = _FakeReqState(
        external_req_id="cmpl-real-request",
        max_tokens_param=16,
        prompt_len=10,
        num_generation_tokens=8,
    )
    iteration_stats = mock.MagicMock()

    patched(processor, req_state, finish_reason.STOP, iteration_stats)

    original_update.assert_called_once_with(processor, req_state, finish_reason.STOP, iteration_stats)


@mock.patch("motor.engine_server.core.sim_inference.importlib.import_module")
def test_patch_vllm_metrics_delegates_when_iteration_stats_is_none(mock_import_module, sim_inference, finish_reason):
    original_update = mock.MagicMock()
    mock_lora_states = mock.MagicMock()
    mock_parent_observe = mock.MagicMock()

    mock_module = mock.MagicMock()
    mock_module.OutputProcessor._update_stats_from_finished = original_update
    mock_module.ParentRequest.observe_finished_request = mock_parent_observe
    mock_import_module.return_value = mock_module

    sim_inference.patch_vllm_metrics()

    patched = mock_module.OutputProcessor._update_stats_from_finished
    processor = mock.MagicMock()
    processor.lora_states = mock_lora_states
    req_state = _FakeReqState(external_req_id="cmpl-42_virtual")

    patched(processor, req_state, finish_reason.LENGTH, None)

    original_update.assert_called_once_with(processor, req_state, finish_reason.LENGTH, None)
    mock_lora_states.request_finished.assert_not_called()
    mock_parent_observe.assert_not_called()
