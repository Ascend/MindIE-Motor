# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import threading
from unittest import mock

import httpx
import pytest

from motor.common.resources.dispatch import DispatchProfile
from motor.common.resources.instance import PDRole
from motor.node_manager.core.services.native_engine.virtual_inference.spec import VirtualInferenceSpec
from motor.node_manager.core.services.native_engine.virtual_inference.worker import VirtualInferenceWorker

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


@pytest.fixture
def worker() -> VirtualInferenceWorker:
    return VirtualInferenceWorker(_make_spec())


# ------------------------------------------------------------------
# construction boundary (vLLM-only after requester factory removal)
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "engine_type",
    ["sglang", "SGLANG", " unknown ", None, ""],
    ids=["sglang", "sglang_upper", "unknown_padded", "none", "empty"],
)
def test_worker_rejects_non_vllm_engine_type(engine_type):
    """Deleting the requester factory must not silently POST /v1/completions
    to non-vLLM engines; construction fails early instead.
    """
    with pytest.raises(ValueError, match="Unsupported engine type for virtual inference"):
        VirtualInferenceWorker(_make_spec(engine_type=engine_type))


def test_worker_accepts_vllm_engine_type_case_insensitive():
    worker = VirtualInferenceWorker(_make_spec(engine_type=" VLLM "))
    assert worker.spec.engine_type == " VLLM "


# ------------------------------------------------------------------
# virtual request sending (worker delegates to the engine requester)
# ------------------------------------------------------------------


@mock.patch("motor.common.http.http_client.AsyncSafeHTTPSClient.create_client")
async def test_send_virtual_request_success(mock_create_client, worker):
    mock_client = mock.MagicMock()
    mock_response = mock.MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_client.post = mock.AsyncMock(return_value=mock_response)
    mock_client.is_closed = False
    mock_create_client.return_value = mock_client

    timeout = httpx.Timeout(5.0)
    await worker.send_virtual_request_async(timeout)

    mock_create_client.assert_called_once_with(
        address="127.0.0.1:8000",
        tls_config=None,
        timeout=timeout,
    )
    mock_client.post.assert_called_once()
    assert mock_client.post.call_args[0][0] == "/v1/completions"


@mock.patch("motor.common.http.http_client.AsyncSafeHTTPSClient.create_client")
@pytest.mark.parametrize(
    "exc_factory, exc_type, inject_via_raise_for_status",
    [
        (
            lambda resp: httpx.HTTPStatusError("404 Not Found", request=mock.MagicMock(), response=resp),
            httpx.HTTPStatusError,
            True,
        ),
        (lambda _resp: httpx.RequestError("Connection error"), httpx.RequestError, False),
    ],
    ids=["http_status_error", "request_error"],
)
async def test_send_virtual_request_error_raises(
    mock_create_client, worker, exc_factory, exc_type, inject_via_raise_for_status
):
    mock_client = mock.MagicMock()
    if inject_via_raise_for_status:
        mock_response = mock.MagicMock()
        mock_response.raise_for_status.side_effect = exc_factory(mock_response)
        mock_client.post = mock.AsyncMock(return_value=mock_response)
    else:
        mock_client.post = mock.AsyncMock(side_effect=exc_factory(None))
    mock_client.is_closed = False
    mock_create_client.return_value = mock_client

    with pytest.raises(exc_type):
        await worker.send_virtual_request_async(httpx.Timeout(5.0))


# ------------------------------------------------------------------
# start / stop lifecycle
# ------------------------------------------------------------------


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.is_ai_cube_usage_watch_supported")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.threading.Thread")
def test_start_launches_loop_and_sampler_threads(mock_thread, mock_support, worker):
    mock_support.return_value = True
    mock_thread_instance = mock.MagicMock()
    mock_thread_instance.is_alive.return_value = False
    mock_thread.return_value = mock_thread_instance

    assert worker.start() is True
    assert worker.start() is False

    assert mock_thread.call_count == 2
    names = sorted(call.kwargs["name"] for call in mock_thread.call_args_list)
    assert names == ["virtual_ai_cube_0", "virtual_inference_0"]
    assert mock_thread_instance.start.call_count == 2


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.is_ai_cube_usage_watch_supported")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.threading.Thread")
def test_start_disabled_worker_skips_threads(mock_thread, mock_support):
    worker = VirtualInferenceWorker(_make_spec(enabled=False))

    assert worker.start() is False

    mock_thread.assert_not_called()
    mock_support.assert_not_called()


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.is_ai_cube_usage_watch_supported")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.threading.Thread")
def test_start_skips_threads_when_hdk_unsupported(mock_thread, mock_support, worker):
    mock_support.return_value = False

    assert worker.start() is False

    mock_thread.assert_not_called()


def test_start_after_stop_is_rejected(worker):
    with mock.patch.object(worker, "_ensure_runtime_supported", return_value=True) as mock_support:
        assert worker.start() is True
        mock_support.assert_called_once()

    worker.stop()
    with mock.patch.object(worker, "_ensure_runtime_supported") as mock_support:
        assert worker.start() is False
        mock_support.assert_not_called()


def test_stop_sets_stop_events_and_cancels_task(worker):
    worker._ai_cube_stop_event.clear()
    worker._stop_event.clear()

    mock_task = mock.MagicMock()
    mock_task.done.return_value = False
    worker._health_check_task = mock_task

    loop = asyncio.new_event_loop()
    worker._loop = loop

    mock_health_thread = mock.MagicMock()
    mock_health_thread.is_alive.return_value = True
    mock_ai_cube_thread = mock.MagicMock()
    mock_ai_cube_thread.is_alive.return_value = True
    worker._health_check_thread = mock_health_thread
    worker._ai_cube_thread = mock_ai_cube_thread

    worker.stop()

    assert worker._ai_cube_stop_event.is_set()
    assert worker._stop_event.is_set()
    assert worker._stopped is True
    mock_health_thread.join.assert_called_once()
    mock_ai_cube_thread.join.assert_called_once()
    loop.close()


def test_stop_is_idempotent(worker):
    worker.stop()
    worker.stop()


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.is_ai_cube_usage_watch_supported")
def test_ai_cube_worker_stops_on_shutdown_event(mock_support, worker):
    """stop() must wake the AI Cube sampler that is blocked in condition.wait."""
    mock_support.return_value = True
    worker._ai_cube_stop_event.clear()
    worker._ai_cube_check_active = False

    entered_wait = threading.Event()
    real_wait = worker._ai_cube_check_condition.wait

    def wait_observably(timeout=None):
        # Thread has entered the idle wait path; then block on the real condition.
        entered_wait.set()
        return real_wait(timeout=timeout)

    with mock.patch.object(worker._ai_cube_check_condition, "wait", side_effect=wait_observably):
        worker._ai_cube_thread = threading.Thread(target=worker.check_ai_cube_usage_worker, daemon=True)
        worker._ai_cube_thread.start()
        assert entered_wait.wait(timeout=5), "AI Cube worker did not enter condition.wait"

        worker.stop()
        worker._ai_cube_thread.join(timeout=5)

    assert not worker._ai_cube_thread.is_alive()


# ------------------------------------------------------------------
# warmup and loop behaviour
# ------------------------------------------------------------------


async def test_warmup_uses_180s_and_loop_uses_configured_timeout():
    """Warmup keeps the fixed 180s; periodic requests use virtual_inference_timeout."""
    worker = VirtualInferenceWorker(_make_spec(request_timeout_seconds=12.5))
    observed_timeouts = []

    async def capture_timeout(timeout):
        observed_timeouts.append(timeout)
        return True

    worker._send_virtual_request_safe = capture_timeout

    await worker._run_virtual_warmup()

    with mock.patch(
        "motor.node_manager.core.services.native_engine.virtual_inference.worker.asyncio.sleep",
        side_effect=asyncio.CancelledError,
    ):
        with _patched_health_check_loop(worker):
            with pytest.raises(asyncio.CancelledError):
                await worker.health_check_loop()

    assert observed_timeouts[0] == httpx.Timeout(180.0)
    assert observed_timeouts[1] == httpx.Timeout(12.5)


async def test_health_check_loop_stops_after_warmup_failure(worker):
    worker._send_virtual_request_safe = mock.AsyncMock(return_value=False)

    with mock.patch.object(worker, "_trigger_ai_cube_sample", return_value=1) as mock_trigger:
        await worker.health_check_loop()

    assert worker.is_abnormal()
    mock_trigger.assert_not_called()


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.threading.Thread")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.asyncio.sleep")
async def test_health_check_loop_internal_error_does_not_mark_abnormal(mock_sleep, mock_thread, worker):
    with mock.patch.object(worker, "_send_virtual_request_safe", new=mock.AsyncMock(return_value=True)):
        assert await worker._run_virtual_warmup() is True

    # A monitor-loop internal error (here _read_ai_cube_sample) must not mark
    # the endpoint abnormal nor alter the failure count; it backs off once and
    # the loop can be cancelled on the following iteration.
    with mock.patch.object(worker, "_read_ai_cube_sample", side_effect=RuntimeError("internal error")):
        mock_sleep.side_effect = asyncio.CancelledError
        with _patched_health_check_loop(worker):
            with pytest.raises(asyncio.CancelledError):
                await worker.health_check_loop()

    assert not worker.is_abnormal()
    assert worker.failure_count == 0
    mock_sleep.assert_called_once()


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.threading.Thread")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.asyncio.sleep")
async def test_health_check_loop_exits_after_max_failure_count(mock_sleep, mock_thread, worker):
    worker = VirtualInferenceWorker(_make_spec(npu_usage_threshold=10, max_failure_count=1))
    with mock.patch.object(worker, "_send_virtual_request_safe", new=mock.AsyncMock(return_value=True)):
        assert await worker._run_virtual_warmup() is True

    request_calls = []

    async def set_low_ai_cube_and_fail(timeout):
        request_calls.append(1)
        with worker._shared_data_lock:
            worker._max_ai_cube_usage = 2
            worker._ai_cube_usage_available = True
            worker._ai_cube_completed_generation = worker._ai_cube_requested_generation
        raise RuntimeError("Request failed")

    with mock.patch.object(worker, "send_virtual_request_async", side_effect=set_low_ai_cube_and_fail):
        with _patched_health_check_loop(worker):
            await worker.health_check_loop()

    assert worker.is_abnormal()
    assert len(request_calls) == 1


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.threading.Thread")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.asyncio.sleep")
async def test_health_check_loop_resets_failure_count_on_success(mock_sleep, mock_thread, worker):
    with mock.patch.object(worker, "_send_virtual_request_safe", new=mock.AsyncMock(return_value=True)):
        assert await worker._run_virtual_warmup() is True

    worker._failure_count = 3
    iteration = {"count": 0}

    async def succeed_with_high_ai_cube(timeout):
        iteration["count"] += 1
        with worker._shared_data_lock:
            worker._max_ai_cube_usage = 50
            worker._ai_cube_usage_available = True
            worker._ai_cube_completed_generation = worker._ai_cube_requested_generation

    with mock.patch.object(worker, "send_virtual_request_async", side_effect=succeed_with_high_ai_cube):
        mock_sleep.side_effect = asyncio.CancelledError
        with _patched_health_check_loop(worker):
            with pytest.raises(asyncio.CancelledError):
                await worker.health_check_loop()

    assert worker.failure_count == 0
    assert not worker.is_abnormal()


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.threading.Thread")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.asyncio.sleep")
async def test_health_check_loop_skips_failure_count_when_ai_cube_unavailable(mock_sleep, mock_thread, worker):
    worker = VirtualInferenceWorker(_make_spec(max_failure_count=1))
    with mock.patch.object(worker, "_send_virtual_request_safe", new=mock.AsyncMock(return_value=True)):
        assert await worker._run_virtual_warmup() is True

    async def fail_without_ai_cube(timeout):
        raise RuntimeError("Request failed")

    with mock.patch.object(worker, "send_virtual_request_async", side_effect=fail_without_ai_cube):
        mock_sleep.side_effect = asyncio.CancelledError
        with _patched_health_check_loop(worker, sample_finished=False):
            with pytest.raises(asyncio.CancelledError):
                await worker.health_check_loop()

    assert not worker.is_abnormal()
    assert worker.failure_count == 0


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.threading.Thread")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.asyncio.sleep")
@pytest.mark.parametrize(
    "spec_kwargs, initial_sleep, ai_cube_usage, expected_sleep",
    [
        ({}, 5, 85, 20),
        ({"npu_usage_threshold": 10}, 20, 1, 5),
    ],
    ids=["high_extends", "low_reverts"],
)
async def test_health_check_loop_ai_cube_adjusts_sleep(
    mock_sleep, mock_thread, spec_kwargs, initial_sleep, ai_cube_usage, expected_sleep
):
    worker = VirtualInferenceWorker(_make_spec(**spec_kwargs))
    worker._sim_sleep = initial_sleep
    with mock.patch.object(worker, "_send_virtual_request_safe", new=mock.AsyncMock(return_value=True)):
        assert await worker._run_virtual_warmup() is True

    async def set_ai_cube_usage(timeout):
        with worker._shared_data_lock:
            worker._max_ai_cube_usage = ai_cube_usage
            worker._ai_cube_usage_available = True
            worker._ai_cube_completed_generation = worker._ai_cube_requested_generation

    with mock.patch.object(worker, "send_virtual_request_async", side_effect=set_ai_cube_usage):
        mock_sleep.side_effect = asyncio.CancelledError
        with _patched_health_check_loop(worker):
            with pytest.raises(asyncio.CancelledError):
                await worker.health_check_loop()

    assert worker.sim_sleep == expected_sleep


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.threading.Thread")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.asyncio.sleep")
async def test_health_check_loop_exits_on_stop_event(mock_sleep, mock_thread, worker):
    with mock.patch.object(worker, "_send_virtual_request_safe", new=mock.AsyncMock(return_value=True)):
        assert await worker._run_virtual_warmup() is True

    async def set_high_ai_cube_usage(timeout):
        with worker._shared_data_lock:
            worker._max_ai_cube_usage = 85
            worker._ai_cube_usage_available = True
            worker._ai_cube_completed_generation = worker._ai_cube_requested_generation

    with mock.patch.object(worker, "send_virtual_request_async", side_effect=set_high_ai_cube_usage):
        mock_sleep.side_effect = asyncio.CancelledError
        with _patched_health_check_loop(worker):
            worker.stop()
            await worker.health_check_loop()

    assert not worker.is_abnormal()


# ------------------------------------------------------------------
# ai cube sampling
# ------------------------------------------------------------------


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.get_ai_cube_usage")
def test_sample_ai_cube_usage_stops_after_first_error(mock_get_ai_cube, worker):
    mock_get_ai_cube.side_effect = RuntimeError("AI Cube usage not found in npu-smi watch output (timeout)")

    result, available = worker._sample_ai_cube_usage()

    assert result == 0
    assert available is False
    mock_get_ai_cube.assert_called_once()


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.time.sleep")
@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.get_ai_cube_usage")
def test_sample_ai_cube_usage_reports_zero_when_idle(mock_get_ai_cube, _mock_sleep, worker):
    mock_get_ai_cube.return_value = 0

    result, available = worker._sample_ai_cube_usage()

    assert result == 0
    assert available is True
    assert mock_get_ai_cube.call_count >= 1


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.get_ai_cube_usage")
def test_read_ai_cube_sample_ignores_stale_generation(mock_get_ai_cube, worker):
    with worker._shared_data_lock:
        worker._max_ai_cube_usage = 42
        worker._ai_cube_usage_available = True
        worker._ai_cube_completed_generation = 1

    max_usage, available = worker._read_ai_cube_sample(generation=2)

    assert max_usage == 0
    assert available is False


@mock.patch("motor.node_manager.core.services.native_engine.virtual_inference.worker.get_ai_cube_usage")
def test_read_ai_cube_sample_available_with_partial_sample(mock_get_ai_cube, worker):
    with worker._shared_data_lock:
        worker._max_ai_cube_usage = 15
        worker._ai_cube_usage_available = True
        worker._ai_cube_completed_generation = 2

    max_usage, available = worker._read_ai_cube_sample(generation=2)

    assert max_usage == 15
    assert available is True


# ------------------------------------------------------------------
# http client cleanup and failure-count atomics
# ------------------------------------------------------------------


def test_health_check_thread_closes_client_on_exit(worker):
    mock_client = mock.MagicMock()
    mock_client.is_closed = False
    mock_client.aclose = mock.AsyncMock()
    worker._client = mock_client

    async def quick_loop():
        return

    with mock.patch.object(worker, "health_check_loop", side_effect=quick_loop):
        thread = threading.Thread(target=worker._run_health_check_thread)
        thread.start()
        thread.join(timeout=2)

    assert not thread.is_alive()
    mock_client.aclose.assert_awaited_once()
    assert worker._client is None


def _patched_health_check_loop(worker, sample_finished=True):
    worker._virtual_warmup_done = True

    def sample_wait(timeout=None):
        if sample_finished:
            with worker._shared_data_lock:
                worker._ai_cube_completed_generation = worker._ai_cube_requested_generation
        return sample_finished

    worker._ai_cube_sample_done.wait = sample_wait

    async def fake_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    return mock.patch(
        "motor.node_manager.core.services.native_engine.virtual_inference.worker.asyncio.to_thread",
        fake_to_thread,
    )


def test_failure_count_increment_and_reset_are_atomic(worker):
    worker = VirtualInferenceWorker(_make_spec(max_failure_count=3))

    assert worker._increment_failure_count() == 1
    assert worker._increment_failure_count() == 2
    assert worker.failure_count == 2
    worker._reset_failure_count()
    assert worker.failure_count == 0
