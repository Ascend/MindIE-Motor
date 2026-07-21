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
from copy import copy
import httpx
from motor.common.resources.dispatch import DispatchProfile, infer_vllm_dispatch_profile_from_config
from motor.common.http.http_client import AsyncSafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.utils.net import format_address
from motor.engine_server.core.config import IConfig
from motor.engine_server.utils.ai_cube import get_ai_cube_usage, is_ai_cube_usage_watch_supported
from motor.engine_server.constants import constants
from motor.engine_server.utils.ip import build_endpoint
from motor.common.utils.snapshot_utils import is_restored_from_host_side_snapshot, get_pod_ip

logger = get_logger(__name__)

_VIRTUAL_REQUEST_TIMEOUT_SEC = 5.0
_VIRTUAL_WARMUP_TIMEOUT_SEC = 180.0
_AI_CUBE_SAMPLE_WINDOW_SEC = 5.0
_SHUTDOWN_JOIN_TIMEOUT_SEC = 5.0
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
        self._health_check_task: asyncio.Task | None = None
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
        self._max_ai_cube_usage = 0
        self._ai_cube_usage_available = False
        self._max_check_count = 4

        # add _max_failure_count to measure consecutive failure times
        self._failure_count = 0
        self.sim_sleep = 5
        self._virtual_warmup_done = False

        # Condition variable to control AI Cube usage check execution
        self._ai_cube_check_condition = threading.Condition()
        self._ai_cube_check_active = False
        self._ai_cube_sample_done = threading.Event()
        self._ai_cube_stop_event = threading.Event()
        self._ai_cube_thread = None
        self._health_check_thread = None
        self._ai_cube_sample_generation = 0
        self._ai_cube_requested_generation = 0
        self._ai_cube_completed_generation = 0

        # init http client
        self._client = None
        self._client_address = format_address(self.args.host, self.args.port)

    @staticmethod
    def _resolve_health_check_config(endpoint_config, args):
        """Apply runtime overrides for virtual inference on a copied HealthCheckConfig."""
        health_check_config = copy(endpoint_config.deploy_config.health_check_config)

        if getattr(args, "headless", False):
            health_check_config.enable_virtual_inference = False
        if endpoint_config.dp_rank != 0:
            health_check_config.enable_virtual_inference = False
            logger.info(
                "Virtual inference is disabled on DP rank %s (only DP0 performs virtual inference)",
                endpoint_config.dp_rank,
            )
        if getattr(endpoint_config, "engine_type", "vllm") == "sglang":
            health_check_config.enable_virtual_inference = False
            logger.info("Virtual inference is disabled for SGLang engine (not supported)")
        return health_check_config

    @classmethod
    def from_config(cls, engine_config: IConfig) -> "SimInference":
        endpoint_config = engine_config.get_endpoint_config()
        args = engine_config.get_args()
        infer_tls_config = endpoint_config.deploy_config.infer_tls_config
        health_check_config = cls._resolve_health_check_config(endpoint_config, args)
        return cls(
            args,
            infer_tls_config,
            health_check_config,
            endpoint_config.role,
            dispatch_profile=infer_vllm_dispatch_profile_from_config(engine_config),
        )

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

        if self.npu_usage_threshold <= 0 or self.npu_usage_threshold > 100:
            logger.info(
                "Health check is disabled because npu_usage_threshold %s is abnormal",
                self.npu_usage_threshold,
            )
            return

        if not is_ai_cube_usage_watch_supported():
            self.enable_virtual_inference = False
            return

        # refresh host when restored from host-side snapshot
        if is_restored_from_host_side_snapshot():
            self._client_address = build_endpoint(get_pod_ip(), self.args.port)

        # Patch vLLM metrics for virtual inference only (filter per-request metrics of virtual requests)
        self.patch_vllm_metrics()
        self._ai_cube_stop_event.clear()

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
                    self._close_http_client_on_loop(loop)
                    if not loop.is_closed():
                        loop.close()

            self._health_check_thread = threading.Thread(target=_run_in_thread, daemon=True)
            self._health_check_thread.start()
            logger.info(
                "Health check task started, first virtual request warmup timeout is %ss, "
                "then interval is 5s by default (20s when AI Cube peak >= 80%%), "
                "npu_usage_threshold=%s%%",
                _VIRTUAL_WARMUP_TIMEOUT_SEC,
                self.npu_usage_threshold,
            )

        # Start AI Cube usage check thread if not already running
        if not self._ai_cube_thread or not self._ai_cube_thread.is_alive():
            self._ai_cube_thread = threading.Thread(target=self.check_ai_cube_usage_worker, daemon=True)
            self._ai_cube_thread.start()
            logger.info("AI Cube usage check thread started")

    def _sample_ai_cube_usage(self, generation: int | None = None) -> tuple[int, bool]:
        """Sample peak AI Cube usage within a bounded time window."""
        max_usage = 0
        usage_available = False
        end_time = time.time() + _AI_CUBE_SAMPLE_WINDOW_SEC
        check_count = 0

        while time.time() < end_time and check_count < self._max_check_count:
            check_count += 1
            try:
                usage = get_ai_cube_usage()
            except Exception as e:
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

    def _read_ai_cube_sample(self, generation: int, sample_finished: bool) -> tuple[int, bool]:
        with self._shared_data_lock:
            if self._ai_cube_completed_generation != generation:
                return 0, False
            if self._ai_cube_usage_available:
                return self._max_ai_cube_usage, True
            return 0, False

    def check_ai_cube_usage_worker(self):
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
        """Regular virtual inference loop; default 5s interval, 20s when AI Cube peak >= 80%."""
        if not await self._run_virtual_warmup():
            return
        self.sim_sleep = 5
        while self._status == constants.NORMAL_STATUS and not self.is_abnormal():
            try:
                timeout = httpx.Timeout(_VIRTUAL_REQUEST_TIMEOUT_SEC)
                generation = self._trigger_ai_cube_sample()

                sim_inference_success, sample_finished = await asyncio.gather(
                    self._send_virtual_request_safe(timeout),
                    asyncio.to_thread(self._ai_cube_sample_done.wait, _AI_CUBE_SAMPLE_WINDOW_SEC),
                )

                logger.debug(
                    "Virtual request %s",
                    "successful" if sim_inference_success else "failed",
                )

                if not sample_finished:
                    logger.warning(
                        "AI Cube usage check did not finish within %s seconds",
                        _AI_CUBE_SAMPLE_WINDOW_SEC,
                    )

                max_usage, ai_cube_available = self._read_ai_cube_sample(generation, sample_finished)

                if ai_cube_available:
                    logger.info(
                        "AI Cube usage rate: %s%%, virtual request: %s",
                        max_usage,
                        "successful" if sim_inference_success else "failed",
                    )
                else:
                    logger.info(
                        "AI Cube usage unavailable, virtual request: %s",
                        "successful" if sim_inference_success else "failed",
                    )

                if ai_cube_available:
                    if max_usage >= 80 and self.sim_sleep != 20:
                        logger.info("AI Cube usage is beyond 80%, Simulate Inference sleep longer time 20 seconds")
                        self.sim_sleep = 20
                    elif max_usage < self.npu_usage_threshold and self.sim_sleep != 5:
                        logger.info(
                            "AI Cube usage is below %s%%, Simulate Inference sleep default time 5 seconds",
                            self.npu_usage_threshold,
                        )
                        self.sim_sleep = 5

                if ai_cube_available and max_usage < self.npu_usage_threshold and not sim_inference_success:
                    logger.warning(
                        "AI Cube usage (%s%%) < threshold (%s%%) and virtual request failed",
                        max_usage,
                        self.npu_usage_threshold,
                    )
                    self._failure_count += 1
                    logger.warning(
                        "Current failure count: %s/%s",
                        self._failure_count,
                        self._max_failure_count,
                    )
                    if self._failure_count >= self._max_failure_count:
                        logger.warning("Reach maximum failure count, set abnormal status")
                        self.set_abnormal_status()
                elif not sim_inference_success and not ai_cube_available:
                    logger.warning("Virtual request failed but AI Cube usage unavailable, skip failure count")
                elif sim_inference_success or (ai_cube_available and max_usage >= self.npu_usage_threshold):
                    if self._failure_count > 0:
                        logger.info("Resetting failure count from %s to 0", self._failure_count)
                        self._failure_count = 0
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

    def _close_http_client_on_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._client is None or self._client.is_closed:
            return
        try:
            loop.run_until_complete(self._client.aclose())
        except Exception as e:
            logger.error("Failed to close virtual inference HTTP client: %s", e)
        finally:
            self._client = None

    def stop_health_check(self):
        """Stop health check task"""
        self._ai_cube_stop_event.set()
        with self._ai_cube_check_condition:
            self._ai_cube_check_condition.notify_all()

        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            logger.info("Health check task stopped")

        if self._health_check_thread and self._health_check_thread.is_alive():
            self._health_check_thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_SEC)

        if self._ai_cube_thread and self._ai_cube_thread.is_alive():
            self._ai_cube_thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_SEC)

        if self._client is not None and not self._client.is_closed:
            logger.warning(
                "Virtual inference HTTP client still open after health check thread join; "
                "client should be closed by the health check thread"
            )
