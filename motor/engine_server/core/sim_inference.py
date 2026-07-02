# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
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
import importlib
from typing import Optional
import httpx
from motor.common.resources.dispatch import DispatchProfile
from motor.common.http.http_client import AsyncSafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.utils.net import format_address
from motor.engine_server.utils.aicore import get_aicore_usage
from motor.engine_server.constants import constants
from motor.engine_server.utils.ip import build_endpoint
from motor.common.utils.snapshot_utils import is_restored_from_host_side_snapshot, get_pod_ip

logger = get_logger(__name__)

_VIRTUAL_REQUEST_TIMEOUT_SEC = 5.0
_VIRTUAL_WARMUP_TIMEOUT_SEC = 180.0
_AICORE_SAMPLE_WINDOW_SEC = 5.0
VIRTUAL_REQUEST_ID_MARKER = "_virtual"


def _is_virtual_metrics_request(req_state) -> bool:
    external_req_id = getattr(req_state, "external_req_id", None) or ""
    return VIRTUAL_REQUEST_ID_MARKER in external_req_id


class SimInference:
    """Virtual inference utility class for sending virtual health check requests"""

    def __init__(
        self,
        args,
        infer_tls_config,
        health_check_config=None,
        role=None,
        dispatch_profile: DispatchProfile | None = None,
    ):
        """Initialize virtual inference utility

        Args:
            args: Command line arguments
            infer_tls_config: TLS configuration for inference service
            health_check_config: Health check configuration, including npu_usage_threshold and other parameters
            role: Engine role (prefill/decode/union)
            dispatch_profile: vLLM P/D coordination profile inferred from kv_transfer_config
        """
        self.args = args
        self.infer_tls_config = infer_tls_config
        self._status = constants.INIT_STATUS
        self._health_check_task: Optional[asyncio.Task] = None
        self._abnormal_status_lock = threading.Lock()
        self._is_abnormal = False
        self.role = role
        self._dispatch_profile = dispatch_profile or DispatchProfile.UNKNOWN

        self.health_check_config = health_check_config or None
        # Get npu_usage_threshold with default value
        if self.health_check_config:
            self.npu_usage_threshold = getattr(self.health_check_config, "npu_usage_threshold", 3)
            self.enable_virtual_inference = getattr(self.health_check_config, "enable_virtual_inference", False)
            self._max_failure_count = getattr(self.health_check_config, "max_failure_count", 6)
        else:
            self.npu_usage_threshold = 3
            self.enable_virtual_inference = False
            self._max_failure_count = 6

        self._shared_data_lock = threading.Lock()
        self._max_aicore_usage = 0
        self._aicore_usage_available = False
        self._max_check_count = 4

        # add _max_failure_count to measure consecutive failure times
        self._failure_count = 0
        # add flag to control failure counting, initially false
        self._count_failure_flag = False
        self.sim_sleep = 5
        self._virtual_warmup_done = False

        # Condition variable to control aicore usage check execution
        self._aicore_check_condition = threading.Condition()
        self._aicore_check_active = False
        self._aicore_sample_done = threading.Event()
        self._aicore_thread = None
        self._aicore_sample_generation = 0
        self._aicore_requested_generation = 0
        self._aicore_completed_generation = 0

        # init http client
        self._client = None
        self._client_address = format_address(self.args.host, self.args.port)

    @staticmethod
    def generate_request_id() -> str:
        """
        Generate globally unique request ID (async, does not block event loop).
        Returns: Pure ID string in format: timestamp(16 digits) + counter(4 digits) + random(8 chars)
        """
        current_timestamp = int(time.time() * 1000000)
        request_id = f"{current_timestamp}_virtual"
        logger.debug("Generated virtual request ID: %s", request_id)
        return request_id

    def set_status(self, status):
        self._status = status

    def patch_vllm_metrics(self):
        """Patch vLLM output stats to skip virtual inference per-request metrics (v0.18+)."""
        if not self.enable_virtual_inference:
            return

        try:
            output_processor_module = importlib.import_module("vllm.v1.engine.output_processor")
            original_update = output_processor_module.OutputProcessor._update_stats_from_finished
            parent_request_cls = output_processor_module.ParentRequest

            def patched_update_stats_from_finished(
                processor_self,
                req_state,
                finish_reason,
                iteration_stats,
            ):
                if iteration_stats is None:
                    original_update(processor_self, req_state, finish_reason, iteration_stats)
                    return

                assert finish_reason is not None
                assert req_state.stats is not None

                if _is_virtual_metrics_request(req_state):
                    logger.debug(
                        "Skipped virtual inference request from per-request metrics: %s",
                        req_state.external_req_id,
                    )
                    processor_self.lora_states.request_finished(req_state.request_id, req_state.lora_name)
                    parent_request_cls.observe_finished_request(
                        req_state.parent_req,
                        iteration_stats,
                        req_state.stats.num_generation_tokens,
                    )
                    return

                original_update(processor_self, req_state, finish_reason, iteration_stats)

            output_processor_module.OutputProcessor._update_stats_from_finished = patched_update_stats_from_finished
            logger.info("Successfully patched vLLM OutputProcessor to skip virtual inference per-request metrics")

        except ImportError as e:
            logger.debug("Failed to import vLLM modules for metrics patching: %s", e)
        except Exception as e:
            logger.error("Failed to patch vLLM output processor metrics hook: %s", e)

    def start_health_check(self):
        # only start virtual inference when enable_virtual_inference is True and npu_usage_threshold is above 0
        if not self.enable_virtual_inference:
            logger.info("Health check is disabled")
            return

        # refresh host when restored from host-side snapshot
        if is_restored_from_host_side_snapshot():
            self._client_address = build_endpoint(get_pod_ip(), self.args.port)

        # Patch vLLM metrics if needed
        self.patch_vllm_metrics()

        if self.npu_usage_threshold <= 0 or self.npu_usage_threshold > 100:
            logger.info(
                "Health check is disabled because npu_usage_threshold %s is abnormal",
                self.npu_usage_threshold,
            )
            return

        if not self._health_check_task or self._health_check_task.done():

            def _run_in_thread():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

                try:
                    # start and run health check task
                    task = loop.create_task(self.health_check_loop())
                    self._health_check_task = task
                    loop.run_until_complete(task)
                except asyncio.CancelledError:
                    logger.info("Health check task cancelled")
                except Exception as e:
                    logger.error("Health check task error: %s", e)
                finally:
                    if not loop.is_closed():
                        loop.close()

            thread = threading.Thread(target=_run_in_thread, daemon=True)
            thread.start()
            logger.info(
                "Health check task started, first virtual request warmup timeout is %ss, "
                "then interval is 5s by default (20s when AICore peak >= 80%%), "
                "npu_usage_threshold=%s%%",
                _VIRTUAL_WARMUP_TIMEOUT_SEC,
                self.npu_usage_threshold,
            )

        # Start aicore usage check thread if not already running
        if not self._aicore_thread or not self._aicore_thread.is_alive():
            self._aicore_thread = threading.Thread(target=self.check_aicore_usage_worker, daemon=True)
            self._aicore_thread.start()
            logger.info("AICore usage check thread started")

    def _sample_aicore_usage(self, generation: int | None = None) -> tuple[int, bool]:
        """Sample peak AICore usage within a bounded time window."""
        max_usage = 0
        usage_available = False
        end_time = time.time() + _AICORE_SAMPLE_WINDOW_SEC
        check_count = 0

        while time.time() < end_time and check_count < self._max_check_count:
            check_count += 1
            try:
                usage = get_aicore_usage()
            except Exception as e:
                logger.error("Error checking AICore usage: %s", e)
                break
            usage_available = True
            max_usage = max(max_usage, usage)
            if generation is not None:
                with self._shared_data_lock:
                    self._max_aicore_usage = max_usage
                    self._aicore_usage_available = True
                    self._aicore_completed_generation = generation
            logger.debug("Aicore usage check: %s%%, current max: %s%%", usage, max_usage)
            if time.time() >= end_time:
                break
            time.sleep(0.5)

        logger.debug(
            "Max Aicore usage in %s seconds: %s%%, available=%s",
            _AICORE_SAMPLE_WINDOW_SEC,
            max_usage,
            usage_available,
        )
        return max_usage, usage_available

    def _trigger_aicore_sample(self) -> int:
        with self._shared_data_lock:
            self._aicore_sample_generation += 1
            generation = self._aicore_sample_generation
            self._aicore_requested_generation = generation
        self._aicore_sample_done.clear()
        with self._aicore_check_condition:
            self._aicore_check_active = True
            self._aicore_check_condition.notify_all()
        return generation

    def _read_aicore_sample(self, generation: int, sample_finished: bool) -> tuple[int, bool]:
        with self._shared_data_lock:
            if self._aicore_completed_generation != generation:
                return 0, False
            if self._aicore_usage_available:
                return self._max_aicore_usage, True
            return 0, False

    def check_aicore_usage_worker(self):
        while True:
            with self._aicore_check_condition:
                while not self._aicore_check_active:
                    self._aicore_check_condition.wait()

                self._aicore_check_active = False

            with self._shared_data_lock:
                requested_gen = self._aicore_requested_generation

            max_usage, usage_available = self._sample_aicore_usage(requested_gen)

            with self._shared_data_lock:
                self._max_aicore_usage = max_usage
                self._aicore_usage_available = usage_available
                self._aicore_completed_generation = requested_gen

            self._aicore_sample_done.set()

    async def init_client(self, timeout):
        if self._client is None or self._client.is_closed:
            logger.debug("Initializing HTTP client for address: %s", self._client_address)
            self._client = AsyncSafeHTTPSClient.create_client(
                address=self._client_address, tls_config=self.infer_tls_config, timeout=timeout
            )

    async def send_virtual_request_async(self, timeout):
        # construct virtual request
        virtual_request = {"model": self.args.served_model_name[0], "prompt": "1", "max_tokens": 1}
        if self.role == constants.DECODE_ROLE and self._dispatch_profile == DispatchProfile.TRIGGER:
            logger.debug("make virtual request for layerwise decode")
            virtual_request["kv_transfer_params"] = {
                "do_remote_decode": False,
                "do_remote_prefill": True,
                "do_virtual": True,
            }

        logger.debug(
            "Sending virtual health check request %s to %s/v1/completions",
            virtual_request,
            self._client_address,
        )
        try:
            await self.init_client(timeout)

            req_id = self.generate_request_id()
            response = await self._client.post(
                "/v1/completions",
                json=virtual_request,
                headers={'Content-Type': 'application/json', 'X-Request-Id': req_id},
                timeout=timeout,
            )
            response.raise_for_status()

            response_data = response.json()
            logger.debug("Received health check response: %s", response_data)
            logger.debug("Health check request successful")
        except httpx.HTTPStatusError as e:
            logger.error("HTTP error in virtual request: %s", e)
            raise
        except httpx.RequestError as e:
            logger.error("Request error in virtual request: %s", e)
            raise
        except Exception as e:
            logger.error("Unexpected error in virtual request: %s", e)
            raise

    async def _send_virtual_request_safe(self, timeout: httpx.Timeout) -> bool:
        try:
            await self.send_virtual_request_async(timeout)
            return True
        except Exception as e:
            logger.error("Virtual request failed: %s", e)
            return False

    async def _run_virtual_warmup(self) -> bool:
        """Wait for the first successful virtual request before regular health checks."""
        if self._virtual_warmup_done:
            return True
        if self._status != constants.NORMAL_STATUS:
            return False

        warmup_timeout = httpx.Timeout(_VIRTUAL_WARMUP_TIMEOUT_SEC)
        logger.info(
            "Virtual inference warmup in progress, request timeout is %s seconds",
            _VIRTUAL_WARMUP_TIMEOUT_SEC,
        )
        if await self._send_virtual_request_safe(warmup_timeout):
            self._virtual_warmup_done = True
            logger.info("Virtual inference warmup completed successfully")
            return True

        self._virtual_warmup_done = True
        logger.warning("Virtual inference warmup request failed, set abnormal status and stop health check")
        self.set_abnormal_status()
        return False

    async def health_check_loop(self):
        """Regular virtual inference loop; default 5s interval, 20s when AICore peak >= 80%."""
        if not await self._run_virtual_warmup():
            return
        self.sim_sleep = 5
        while self._status == constants.NORMAL_STATUS:
            try:
                timeout = httpx.Timeout(_VIRTUAL_REQUEST_TIMEOUT_SEC)
                generation = self._trigger_aicore_sample()

                sim_inference_success, sample_finished = await asyncio.gather(
                    self._send_virtual_request_safe(timeout),
                    asyncio.to_thread(self._aicore_sample_done.wait, _AICORE_SAMPLE_WINDOW_SEC),
                )

                logger.debug(
                    "Virtual request %s",
                    "successful" if sim_inference_success else "failed",
                )

                if not sample_finished:
                    logger.warning(
                        "AICore usage check did not finish within %s seconds",
                        _AICORE_SAMPLE_WINDOW_SEC,
                    )

                max_usage, aicore_available = self._read_aicore_sample(generation, sample_finished)

                if aicore_available:
                    logger.info(
                        "Aicore usage rate: %s%%, virtual request: %s",
                        max_usage,
                        "successful" if sim_inference_success else "failed",
                    )
                else:
                    logger.info(
                        "Aicore usage unavailable, virtual request: %s",
                        "successful" if sim_inference_success else "failed",
                    )

                if aicore_available:
                    if max_usage >= 80 and self.sim_sleep != 20:
                        logger.info("AICore usage is beyond 80%, Simulate Inference sleep longer time 20 seconds")
                        self.sim_sleep = 20
                    elif max_usage < self.npu_usage_threshold and self.sim_sleep != 5:
                        logger.info(
                            "AICore usage is below %s%%, Simulate Inference sleep default time 5 seconds",
                            self.npu_usage_threshold,
                        )
                        self.sim_sleep = 5

                if aicore_available and max_usage < self.npu_usage_threshold and not sim_inference_success:
                    logger.warning(
                        "AICore usage (%s%%) < threshold (%s%%) and virtual request failed",
                        max_usage,
                        self.npu_usage_threshold,
                    )
                    if self._count_failure_flag:
                        self._failure_count += 1
                        logger.warning(
                            "Current failure count: %s/%s",
                            self._failure_count,
                            self._max_failure_count,
                        )
                        if self._failure_count >= self._max_failure_count:
                            logger.warning("Reach maximum failure count, set abnormal status")
                            self.set_abnormal_status()
                elif not sim_inference_success and not aicore_available:
                    logger.warning("Virtual request failed but AICore usage unavailable, skip failure count")
                elif sim_inference_success or (aicore_available and max_usage >= self.npu_usage_threshold):
                    self._count_failure_flag = True
                    logger.debug("count_failure_flag set to True")
                    if self._failure_count > 0:
                        logger.info("Resetting failure count from %s to 0", self._failure_count)
                        self._failure_count = 0
                    if self.is_abnormal():
                        self.reset_abnormal_status()
            except Exception as e:
                logger.error("Error in health check loop: %s", e)
                self.set_abnormal_status()
                logger.warning("Status changed to ABNORMAL_STATUS due to health check failure")

            await asyncio.sleep(self.sim_sleep)

    def set_abnormal_status(self):
        """Set abnormal status (thread-safe)"""
        with self._abnormal_status_lock:
            self._is_abnormal = True
        logger.warning("Abnormal status flag set to True")

    def is_abnormal(self) -> bool:
        """Check if in abnormal status (thread-safe)"""
        with self._abnormal_status_lock:
            return self._is_abnormal

    def reset_abnormal_status(self):
        """Reset abnormal status (thread-safe)"""
        with self._abnormal_status_lock:
            self._is_abnormal = False
        logger.info("Abnormal status flag set to False")

    def stop_health_check(self):
        """Stop health check task"""
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            logger.info("Health check task stopped")
        self.reset_abnormal_status()
