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

import asyncio
import threading
import time
import importlib
from typing import Optional
import httpx
from motor.common.http.http_client import AsyncSafeHTTPSClient
from motor.common.logger import get_logger
from motor.engine_server.utils.aicore import get_aicore_usage
from motor.engine_server.constants import constants

logger = get_logger(__name__)


class SimInference:
    """Virtual inference utility class for sending virtual health check requests"""

    def __init__(self, args, infer_tls_config, health_check_config=None, role=None):
        """Initialize virtual inference utility

        Args:
            args: Command line arguments
            infer_tls_config: TLS configuration for inference service
            health_check_config: Health check configuration, including npu_usage_threshold and other parameters
        """
        self.args = args
        self.infer_tls_config = infer_tls_config
        self._status = constants.INIT_STATUS
        self._health_check_task: Optional[asyncio.Task] = None
        self._abnormal_status_lock = threading.Lock()
        self._is_abnormal = False
        self.role = role

        self.health_check_config = health_check_config or None
        # Get npu_usage_threshold with default value
        if self.health_check_config:
            self.npu_usage_threshold = getattr(self.health_check_config, 'npu_usage_threshold', 3)
            self.enable_virtual_inference = getattr(self.health_check_config, 'enable_virtual_inference', False)
            self._max_failure_count = getattr(self.health_check_config, 'max_failure_count', 6)
        else:
            self.npu_usage_threshold = 3
            self.enable_virtual_inference = False
            self._max_failure_count = 6

        self._shared_data_lock = threading.Lock()
        self._max_aicore_usage = 0
        self._check_count = 0
        self._max_check_count = 4

        # add _max_failure_count to measure consecutive failure times
        self._failure_count = 0
        # add flag to control failure counting, initially false
        self._count_failure_flag = False
        self.sim_sleep = 5

        # Condition variable to control aicore usage check execution
        self._aicore_check_condition = threading.Condition()
        self._aicore_check_active = False
        self._aicore_thread = None

        # init http client
        self._client = None
        self._client_address = f"{self.args.host}:{self.args.port}"

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
        """
        Patch vLLM's metrics logger to skip virtual inference requests when:
        1. enable_virtual_inference is True
        2. VLLM version is 0.18.0
        """
        if not self.enable_virtual_inference:
            return

        try:
            # Import the metrics logger module
            metrics_loggers_module = importlib.import_module("vllm.v1.metrics.loggers")

            # Get the original record method
            original_record = metrics_loggers_module.PrometheusStatLogger.record

            # Create a patched version of record method
            def patched_record(self, scheduler_stats, iteration_stats, mm_cache_stats=None, engine_idx=0):
                """Patched record method to skip virtual inference requests."""
                if iteration_stats is None:
                    return original_record(self, scheduler_stats, iteration_stats, mm_cache_stats, engine_idx)

                # Filter out virtual inference requests
                original_finished_requests = iteration_stats.finished_requests
                filtered_finished_requests = []
                filtered_count = 0

                for finished_request in original_finished_requests:
                    # Skip requests with num_prompt_tokens=1 and num_generation_tokens=1
                    if finished_request.num_prompt_tokens == 2 and finished_request.num_generation_tokens == 1:
                        filtered_count += 1
                        continue
                    filtered_finished_requests.append(finished_request)

                # Log the number of filtered virtual inference requests
                if filtered_count > 0:
                    logger.debug("Filtered out %s virtual inference requests from metrics", filtered_count)

                # Replace finished_requests with filtered list
                iteration_stats.finished_requests = filtered_finished_requests

                # Call original method with filtered requests
                return original_record(self, scheduler_stats, iteration_stats, mm_cache_stats, engine_idx)

            # Apply the patch
            metrics_loggers_module.PrometheusStatLogger.record = patched_record
            logger.info("Successfully patched vLLM metrics logger to skip virtual inference requests")

        except ImportError as e:
            logger.debug("Failed to import vLLM modules for patching: %s", e)
        except Exception as e:
            logger.error("Failed to patch vLLM metrics logger: %s", e)

    def start_health_check(self):
        # only start virtual inference when enable_virtual_inference is True and npu_usage_threshold is above 0
        if not self.enable_virtual_inference:
            logger.info("Health check is disabled")
            return

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
                "Health check task started, virtual request interval is 5s by default "
                "(20s when AICore peak >= 80%%), npu_usage_threshold=%s%%",
                self.npu_usage_threshold,
            )

        # Start aicore usage check thread if not already running
        if not self._aicore_thread or not self._aicore_thread.is_alive():
            self._aicore_thread = threading.Thread(target=self.check_aicore_usage_worker, daemon=True)
            self._aicore_thread.start()
            logger.info("AICore usage check thread started")

    def check_aicore_usage_worker(self):
        while True:
            # Wait for signal to start checking
            with self._aicore_check_condition:
                while not self._aicore_check_active:
                    self._aicore_check_condition.wait()

                # NPU aicore check
                max_usage = 0
                end_time = time.time() + 3
                self._check_count = 0

                while time.time() < end_time or self._check_count <= self._max_check_count:
                    self._check_count += 1
                    try:
                        usage = get_aicore_usage()
                    except Exception as e:
                        logger.error("Error checking AICore usage: %s", e)
                        time.sleep(0.5)
                        continue
                    max_usage = max(max_usage, usage)
                    logger.debug("Aicore usage check: %s%%, current max: %s%%", usage, max_usage)
                    time.sleep(0.5)
                with self._shared_data_lock:
                    self._max_aicore_usage = max_usage

                logger.debug("Max Aicore usage in 3 seconds: %s%%", max_usage)

                # Reset active flag after checking
                self._aicore_check_active = False
                self._aicore_check_condition.notify_all()

    async def init_client(self, timeout):
        if self._client is None or self._client.is_closed:
            logger.debug("Initializing HTTP client for address: %s", self._client_address)
            self._client = AsyncSafeHTTPSClient.create_client(
                address=self._client_address, tls_config=self.infer_tls_config, timeout=timeout
            )

    async def send_virtual_request_async(self, timeout):
        # construct virtual request
        virtual_request = {"model": self.args.served_model_name[0], "prompt": "1", "max_tokens": 1}
        if self.role == constants.DECODE_ROLE:
            logger.debug("make virtual request for decode")
            virtual_request["kv_transfer_params"] = {
                "do_remote_decode": False,
                "do_remote_prefill": True,
                "metaserver": f"http://{self.args.host}:{self.args.port}/v1/metaserver",
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

    async def health_check_loop(self):
        """Regular virtual inference loop; default 5s interval, 20s when AICore peak >= 80%."""
        self.sim_sleep = 5
        while self._status == constants.NORMAL_STATUS:
            try:
                with self._shared_data_lock:
                    self._max_aicore_usage = 0
                    self._check_count = 0

                timeout = httpx.Timeout(5.0)
                sim_inference_success = True
                try:
                    await self.send_virtual_request_async(timeout)
                except Exception as e:
                    logger.error("Virtual request failed: %s", e)
                    sim_inference_success = False

                logger.debug(
                    "Virtual request %s",
                    "successful" if sim_inference_success else "failed",
                )

                # Signal aicore check thread to start checking at line 152
                with self._aicore_check_condition:
                    self._aicore_check_active = True
                    self._aicore_check_condition.notify_all()

                # Wait for aicore check to complete
                with self._aicore_check_condition:
                    if self._aicore_check_active:
                        self._aicore_check_condition.wait(timeout=3)
                    if self._aicore_check_active:
                        logger.warning("AICore usage check thread timeout")

                with self._shared_data_lock:
                    max_usage = self._max_aicore_usage

                logger.info(
                    "Aicore usage rate: %s%%, virtual request: %s",
                    max_usage,
                    "successful" if sim_inference_success else "failed",
                )

                if max_usage >= 80 and self.sim_sleep != 20:
                    logger.info("AICore usage is beyond 80%, Simulate Inference sleep longer time 20 seconds")
                    self.sim_sleep = 20
                elif max_usage < self.npu_usage_threshold and self.sim_sleep != 5:
                    logger.info(
                        "AICore usage is below %s%%, Simulate Inference sleep default time 5 seconds",
                        self.npu_usage_threshold,
                    )
                    self.sim_sleep = 5

                # Set abnormal status when AICore Usage below threshold and virtual inference failed
                if max_usage < self.npu_usage_threshold and not sim_inference_success:
                    logger.warning(
                        "AICore usage (%s%%) < threshold (%s%%) and virtual request failed",
                        max_usage,
                        self.npu_usage_threshold,
                    )
                    # Only increase failure count when count_failure_flag is true
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
                else:
                    # Set count_failure_flag to true when virtual inference succeeds or AICore usage reaches threshold
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
