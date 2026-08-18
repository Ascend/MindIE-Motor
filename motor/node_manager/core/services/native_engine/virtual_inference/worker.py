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
import time

import httpx

from motor.common.http.http_client import AsyncSafeHTTPSClient
from motor.common.logger import get_logger
from motor.node_manager.core.services.native_engine.virtual_inference.ai_cube import (
    get_ai_cube_usage,
    is_ai_cube_usage_watch_supported,
)
from motor.common.utils.net import format_address
from motor.node_manager.core.services.native_engine.virtual_inference.requesters import VllmCompletionsRequester
from motor.node_manager.core.services.native_engine.virtual_inference.spec import VirtualInferenceSpec

logger = get_logger(__name__)

# Warmup uses a fixed 180s timeout; periodic requests use spec.request_timeout_seconds.
_VIRTUAL_WARMUP_TIMEOUT_SEC = 180.0
_AI_CUBE_SAMPLE_WINDOW_SEC = 5.0
_AI_CUBE_HIGH_LOAD_THRESHOLD = 80
_VIRTUAL_LOOP_INTERVAL_SEC = 5
_VIRTUAL_HIGH_LOAD_INTERVAL_SEC = 20
_SHUTDOWN_JOIN_TIMEOUT_SEC = 5.0
_AI_CUBE_MAX_CHECK_COUNT = 4


class VirtualInferenceWorker:
    """vLLM virtual inference probes; abnormal state degrades runtime only (never kills the engine)."""

    def __init__(self, spec: VirtualInferenceSpec) -> None:
        normalized_engine = str(spec.engine_type or "").strip().lower()
        if normalized_engine != "vllm":
            raise ValueError(f"Unsupported engine type for virtual inference: {spec.engine_type}")
        self._spec = spec
        self._requester: VllmCompletionsRequester = VllmCompletionsRequester(spec)
        self._lock = threading.Lock()
        self._abnormal_lock = threading.Lock()
        self._is_abnormal = False
        self._failure_count = 0
        self._started = False
        self._stopped = False
        self._runtime_supported: bool | None = None
        self._virtual_warmup_done = False
        self._sim_sleep = _VIRTUAL_LOOP_INTERVAL_SEC
        self._stop_event = threading.Event()
        self._health_check_task: asyncio.Task | None = None
        self._health_check_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: httpx.AsyncClient | None = None
        self._client_address = format_address(spec.host, spec.port)

        self._shared_data_lock = threading.Lock()
        self._max_ai_cube_usage = 0
        self._ai_cube_usage_available = False
        self._ai_cube_check_condition = threading.Condition()
        self._ai_cube_check_active = False
        self._ai_cube_sample_done = threading.Event()
        self._ai_cube_stop_event = threading.Event()
        self._ai_cube_thread: threading.Thread | None = None
        self._ai_cube_sample_generation = 0
        self._ai_cube_requested_generation = 0
        self._ai_cube_completed_generation = 0

    def start(self) -> bool:
        """Idempotently start the loop; one-shot (stop() or failed start cannot be retried on this instance)."""
        with self._lock:
            if self._stopped:
                logger.info(
                    "Virtual inference worker for endpoint %s is stopped and cannot be restarted",
                    self._spec.endpoint_id,
                )
                return False
            if self._started:
                return False
            self._started = True

        if not self._spec.enabled:
            logger.info("Virtual inference is disabled for endpoint %s", self._spec.endpoint_id)
            return False

        if not self._ensure_runtime_supported():
            return False

        if not self._ai_cube_thread or not self._ai_cube_thread.is_alive():
            self._ai_cube_thread = threading.Thread(
                target=self.check_ai_cube_usage_worker,
                daemon=True,
                name=f"virtual_ai_cube_{self._spec.endpoint_id}",
            )
            self._ai_cube_thread.start()
            logger.info("AI Cube usage check thread started for endpoint %s", self._spec.endpoint_id)

        if not self._health_check_thread or not self._health_check_thread.is_alive():
            self._health_check_thread = threading.Thread(
                target=self._run_health_check_thread,
                daemon=True,
                name=f"virtual_inference_{self._spec.endpoint_id}",
            )
            self._health_check_thread.start()
            logger.info(
                "Virtual inference started for endpoint %s, first virtual request warmup timeout is %ss, "
                "periodic request timeout is %ss, "
                "interval is %ss by default (%ss when AI Cube peak >= %s%%), "
                "npu_usage_threshold=%s%%",
                self._spec.endpoint_id,
                _VIRTUAL_WARMUP_TIMEOUT_SEC,
                self._spec.request_timeout_seconds,
                _VIRTUAL_LOOP_INTERVAL_SEC,
                _VIRTUAL_HIGH_LOAD_INTERVAL_SEC,
                _AI_CUBE_HIGH_LOAD_THRESHOLD,
                self._spec.npu_usage_threshold,
            )
        return True

    def stop(self) -> None:
        """Stop threads and close the HTTP client; idempotent and one-shot."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True

        self._ai_cube_stop_event.set()
        with self._ai_cube_check_condition:
            self._ai_cube_check_condition.notify_all()
        self._stop_event.set()

        task = self._health_check_task
        loop = self._loop
        if task is not None and not task.done() and loop is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                logger.debug("Virtual inference event loop is closed, skip task cancellation")

        with self._lock:
            health_thread = self._health_check_thread
            ai_cube_thread = self._ai_cube_thread

        if health_thread is not None and health_thread.is_alive():
            health_thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_SEC)
        if ai_cube_thread is not None and ai_cube_thread.is_alive():
            ai_cube_thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_SEC)

        if self._client is not None and not self._client.is_closed:
            logger.warning(
                "Virtual inference HTTP client still open after thread join for endpoint %s; "
                "client should be closed by the health check thread",
                self._spec.endpoint_id,
            )
        logger.info("Virtual inference stopped for endpoint %s", self._spec.endpoint_id)

    def is_abnormal(self) -> bool:
        """Return whether the endpoint is marked abnormal."""
        with self._abnormal_lock:
            return self._is_abnormal

    def set_abnormal_status(self) -> None:
        """Mark the endpoint abnormal."""
        with self._abnormal_lock:
            self._is_abnormal = True
        logger.warning("Virtual inference marked endpoint %s abnormal", self._spec.endpoint_id)

    def reset_abnormal_status(self) -> None:
        """Clear the abnormal flag."""
        with self._abnormal_lock:
            self._is_abnormal = False
        logger.info("Virtual inference abnormal flag reset for endpoint %s", self._spec.endpoint_id)

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def _increment_failure_count(self) -> int:
        """Increment and return the consecutive failure count."""
        with self._lock:
            self._failure_count += 1
            return self._failure_count

    def _reset_failure_count(self) -> None:
        """Reset the consecutive failure count to zero."""
        with self._lock:
            self._failure_count = 0

    @property
    def sim_sleep(self) -> int:
        return self._sim_sleep

    @property
    def spec(self) -> VirtualInferenceSpec:
        return self._spec

    def _run_health_check_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            task = loop.create_task(self.health_check_loop())
            self._health_check_task = task
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            logger.info("Virtual inference task cancelled for endpoint %s", self._spec.endpoint_id)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Virtual inference task error for endpoint %s: %s", self._spec.endpoint_id, e)
        finally:
            self._close_http_client_on_loop(loop)
            if not loop.is_closed():
                loop.close()

    async def health_check_loop(self) -> None:
        """Periodic virtual inference loop (5s default, 20s when AI Cube peak >= 80%)."""
        if not await self._run_virtual_warmup():
            return
        self._sim_sleep = _VIRTUAL_LOOP_INTERVAL_SEC
        while not self._stop_event.is_set() and not self.is_abnormal():
            try:
                timeout = httpx.Timeout(self._spec.request_timeout_seconds)
                generation = self._trigger_ai_cube_sample()

                sim_inference_success, sample_finished = await asyncio.gather(
                    self._send_virtual_request_safe(timeout),
                    asyncio.to_thread(self._ai_cube_sample_done.wait, _AI_CUBE_SAMPLE_WINDOW_SEC),
                )

                if not sample_finished:
                    logger.warning(
                        "AI Cube usage check did not finish within %s seconds for endpoint %s",
                        _AI_CUBE_SAMPLE_WINDOW_SEC,
                        self._spec.endpoint_id,
                    )

                max_usage, ai_cube_available = self._read_ai_cube_sample(generation)

                if ai_cube_available:
                    logger.info(
                        "AI Cube usage rate: %s%%, virtual request: %s (endpoint %s)",
                        max_usage,
                        "successful" if sim_inference_success else "failed",
                        self._spec.endpoint_id,
                    )
                else:
                    logger.info(
                        "AI Cube usage unavailable, virtual request: %s (endpoint %s)",
                        "successful" if sim_inference_success else "failed",
                        self._spec.endpoint_id,
                    )

                if ai_cube_available:
                    if max_usage >= _AI_CUBE_HIGH_LOAD_THRESHOLD and self._sim_sleep != _VIRTUAL_HIGH_LOAD_INTERVAL_SEC:
                        logger.info(
                            "AI Cube usage is beyond %s%%, virtual inference sleeps longer (%ss) for endpoint %s",
                            _AI_CUBE_HIGH_LOAD_THRESHOLD,
                            _VIRTUAL_HIGH_LOAD_INTERVAL_SEC,
                            self._spec.endpoint_id,
                        )
                        self._sim_sleep = _VIRTUAL_HIGH_LOAD_INTERVAL_SEC
                    elif max_usage < self._spec.npu_usage_threshold and self._sim_sleep != _VIRTUAL_LOOP_INTERVAL_SEC:
                        logger.info(
                            "AI Cube usage is below %s%%, virtual inference sleeps default %ss for endpoint %s",
                            self._spec.npu_usage_threshold,
                            _VIRTUAL_LOOP_INTERVAL_SEC,
                            self._spec.endpoint_id,
                        )
                        self._sim_sleep = _VIRTUAL_LOOP_INTERVAL_SEC

                if ai_cube_available and max_usage < self._spec.npu_usage_threshold and not sim_inference_success:
                    logger.warning(
                        "AI Cube usage (%s%%) < threshold (%s%%) and virtual request failed for endpoint %s",
                        max_usage,
                        self._spec.npu_usage_threshold,
                        self._spec.endpoint_id,
                    )
                    new_count = self._increment_failure_count()
                    logger.warning(
                        "Current failure count: %s/%s for endpoint %s",
                        new_count,
                        self._spec.max_failure_count,
                        self._spec.endpoint_id,
                    )
                    if new_count >= self._spec.max_failure_count:
                        logger.warning(
                            "Reach maximum failure count for endpoint %s, set abnormal status",
                            self._spec.endpoint_id,
                        )
                        self.set_abnormal_status()
                elif not sim_inference_success and not ai_cube_available:
                    logger.warning(
                        "Virtual request failed but AI Cube usage unavailable, skip failure count for endpoint %s",
                        self._spec.endpoint_id,
                    )
                elif sim_inference_success or (ai_cube_available and max_usage >= self._spec.npu_usage_threshold):
                    previous = self.failure_count
                    if previous > 0:
                        logger.info(
                            "Resetting failure count from %s to 0 for endpoint %s",
                            previous,
                            self._spec.endpoint_id,
                        )
                        self._reset_failure_count()
            except Exception:  # pylint: disable=broad-except
                # Internal loop errors are not engine failures: log, backoff, keep probing.
                logger.exception(
                    "Unexpected error in virtual inference loop for endpoint %s; "
                    "retrying after backoff without marking abnormal",
                    self._spec.endpoint_id,
                )

            await asyncio.sleep(self._sim_sleep)

    async def _run_virtual_warmup(self) -> bool:
        """First virtual request uses fixed 180s timeout; periodic requests use virtual_inference_timeout."""
        if self._virtual_warmup_done:
            return True

        warmup_timeout = httpx.Timeout(_VIRTUAL_WARMUP_TIMEOUT_SEC)
        logger.info(
            "Virtual inference warmup in progress for endpoint %s, request timeout is %s seconds",
            self._spec.endpoint_id,
            _VIRTUAL_WARMUP_TIMEOUT_SEC,
        )
        if await self._send_virtual_request_safe(warmup_timeout):
            self._virtual_warmup_done = True
            logger.info("Virtual inference warmup completed successfully for endpoint %s", self._spec.endpoint_id)
            return True

        self._virtual_warmup_done = True
        logger.warning(
            "Virtual inference warmup request failed for endpoint %s, set abnormal status and stop health check",
            self._spec.endpoint_id,
        )
        self.set_abnormal_status()
        return False

    async def _send_virtual_request_safe(self, timeout: httpx.Timeout) -> bool:
        try:
            await self.send_virtual_request_async(timeout)
            return True
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Virtual request failed for endpoint %s: %s", self._spec.endpoint_id, e)
            return False

    async def send_virtual_request_async(self, timeout: httpx.Timeout) -> None:
        """Send one virtual request via the vLLM completions requester."""
        try:
            if self._client is None or self._client.is_closed:
                logger.debug("Initializing HTTP client for address: %s", self._client_address)
                self._client = AsyncSafeHTTPSClient.create_client(
                    address=self._client_address,
                    tls_config=self._spec.tls_config,
                    timeout=timeout,
                )

            await self._requester.send(self._client, timeout)
            logger.debug("Virtual health check request successful for endpoint %s", self._spec.endpoint_id)
        except httpx.HTTPStatusError as e:
            logger.error("HTTP error in virtual request for endpoint %s: %s", self._spec.endpoint_id, e)
            raise
        except httpx.RequestError as e:
            logger.error("Request error in virtual request for endpoint %s: %s", self._spec.endpoint_id, e)
            raise
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Unexpected error in virtual request for endpoint %s: %s", self._spec.endpoint_id, e)
            raise

    def _close_http_client_on_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._client is None or self._client.is_closed:
            return
        try:
            loop.run_until_complete(self._client.aclose())
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Failed to close virtual inference HTTP client: %s", e)
        finally:
            self._client = None

    def _ensure_runtime_supported(self) -> bool:
        """Return whether the current HDK supports AI Cube usage sampling."""
        with self._lock:
            if self._runtime_supported is not None:
                return self._runtime_supported
        supported = is_ai_cube_usage_watch_supported()
        with self._lock:
            self._runtime_supported = supported
        if not supported:
            logger.info(
                "Virtual inference disabled for endpoint %s: HDK does not support AI Cube usage sampling",
                self._spec.endpoint_id,
            )
        return supported

    def _sample_ai_cube_usage(self, generation: int | None = None) -> tuple[int, bool]:
        """Sample peak AI Cube usage within the sampling window."""
        max_usage = 0
        usage_available = False
        end_time = time.time() + _AI_CUBE_SAMPLE_WINDOW_SEC
        check_count = 0

        while time.time() < end_time and check_count < _AI_CUBE_MAX_CHECK_COUNT:
            check_count += 1
            try:
                usage = get_ai_cube_usage()
            except Exception as e:  # pylint: disable=broad-except
                logger.error("Error checking AI Cube usage: %s", e)
                break
            usage_available = True
            max_usage = max(max_usage, usage)
            if generation is not None:
                with self._shared_data_lock:
                    self._max_ai_cube_usage = max_usage
                    self._ai_cube_usage_available = True
                    self._ai_cube_completed_generation = generation
            logger.debug("AI Cube usage check: %s%%, current max: %s%%", usage, max_usage)
            if time.time() >= end_time:
                break
            time.sleep(0.5)

        logger.debug(
            "Max AI Cube usage in %s seconds: %s%%, available=%s",
            _AI_CUBE_SAMPLE_WINDOW_SEC,
            max_usage,
            usage_available,
        )
        return max_usage, usage_available

    def _trigger_ai_cube_sample(self) -> int:
        with self._shared_data_lock:
            self._ai_cube_sample_generation += 1
            generation = self._ai_cube_sample_generation
            self._ai_cube_requested_generation = generation
        self._ai_cube_sample_done.clear()
        with self._ai_cube_check_condition:
            self._ai_cube_check_active = True
            self._ai_cube_check_condition.notify_all()
        return generation

    def _read_ai_cube_sample(self, generation: int) -> tuple[int, bool]:
        with self._shared_data_lock:
            if self._ai_cube_completed_generation != generation:
                return 0, False
            if self._ai_cube_usage_available:
                return self._max_ai_cube_usage, True
            return 0, False

    def check_ai_cube_usage_worker(self) -> None:
        """AI Cube sampler thread; exits when stop is signaled."""
        while not self._ai_cube_stop_event.is_set():
            with self._ai_cube_check_condition:
                while not self._ai_cube_check_active and not self._ai_cube_stop_event.is_set():
                    self._ai_cube_check_condition.wait(timeout=1.0)

                if self._ai_cube_stop_event.is_set():
                    break

                self._ai_cube_check_active = False

            with self._shared_data_lock:
                requested_gen = self._ai_cube_requested_generation

            max_usage, usage_available = self._sample_ai_cube_usage(requested_gen)

            with self._shared_data_lock:
                self._max_ai_cube_usage = max_usage
                self._ai_cube_usage_available = usage_available
                self._ai_cube_completed_generation = requested_gen

            self._ai_cube_sample_done.set()
