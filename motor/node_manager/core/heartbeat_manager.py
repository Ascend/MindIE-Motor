#!/usr/bin/env python3
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

import os
import threading
import time
import socket

from motor.common.resources.endpoint import Endpoint, EndpointStatus
from motor.common.resources.http_msg_spec import StartCmdMsg, HeartbeatMsg
from motor.common.logger import get_logger
from motor.common.utils.net import format_address
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.utils.snapshot_utils import is_restored_from_host_side_snapshot, RETRY_LOG_FREQUENCY
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.api_client.controller_api_client import ControllerApiClient
from motor.node_manager.api_client.engine_server_api_client import EngineServerApiClient
from motor.node_manager.core.engine_manager import EngineManager
from motor.node_manager.core.daemon import Daemon


logger = get_logger(__name__)


class HeartbeatManager(ThreadSafeSingleton):
    def __init__(self, config: NodeManagerConfig | None = None) -> None:
        if hasattr(self, "_initialized"):
            return

        self._endpoint_lock = threading.Lock()
        self.config_lock = threading.RLock()
        self.stop_event = threading.Event()

        if config is None:
            config = NodeManagerConfig.from_json()

        self._config = config
        self.heartbeat_interval_seconds = config.basic_config.heartbeat_interval_seconds

        self._job_name = ""
        self._role = "prefill"
        self._instance_id = -1
        self._endpoints: list[Endpoint] = []
        self._heartbeat_report_thread = threading.Thread(
            target=self._report_heartbeat_loop,
            daemon=True,
            name="heartbeat_report",
        )
        self._engine_server_status_thread = threading.Thread(
            target=self._refresh_endpoints_status_loop,
            daemon=True,
            name="endpoint_status_fetch",
        )
        self._thread_started = False
        self._engine_status_thread_start_time = None
        self._is_within_grace_period = True
        self._consecutive_abnormal_count = 0
        self._abnormal_count_lock = threading.Lock()
        self._should_suicide = False
        self._suicide_lock = threading.Lock()
        # for snapshot
        self._register_after_restore_retry_count = 0
        self._checkpoint_done_inspect_retry_count = 0
        self._is_registered_after_restore = False
        self._is_started_after_restore = False
        self._started_after_restore_lock = threading.Lock()
        self._endpoints_generation = 0

        self._initialized = True
        logger.info("HeartBeatManager module start.")

    def start(self):
        if self._thread_started is False:
            self._heartbeat_report_thread.start()
            self._engine_server_status_thread.start()
            self._engine_status_thread_start_time = time.time()
            self._thread_started = True
        else:
            logger.info("Heartbeat thread has been started...")

    def update_config(self, config: NodeManagerConfig) -> None:
        """Update configuration for the heartbeat manager"""
        with self.config_lock:
            # Update config fields
            self.heartbeat_interval_seconds = config.basic_config.heartbeat_interval_seconds
            logger.info("HeartbeatManager configuration updated")

    def update_endpoint(self, node_manager_info: StartCmdMsg) -> None:
        with self._endpoint_lock:
            self._job_name = node_manager_info.job_name
            self._role = node_manager_info.role
            self._instance_id = node_manager_info.instance_id
            self._endpoints.clear()
            for item in node_manager_info.endpoints:
                self._endpoints.append(item)
            self._endpoints_generation += 1
        # Reset abnormal count when endpoints are updated
        with self._abnormal_count_lock:
            self._consecutive_abnormal_count = 0
        # Reset suicide flag when endpoints are updated
        with self._suicide_lock:
            self._should_suicide = False

    def should_suicide(self) -> bool:
        """
        Check if suicide flag is set.
        Returns True if 5 consecutive abnormal heartbeats have been reported.
        """
        with self._suicide_lock:
            return self._should_suicide

    def stop(self) -> None:
        self.stop_event.set()
        if self._heartbeat_report_thread.is_alive():
            self._heartbeat_report_thread.join(timeout=2.0)
        if self._engine_server_status_thread.is_alive():
            self._engine_server_status_thread.join(timeout=2.0)
        logger.info("HeartBeatManager stopped.")

    def check_all_endpoints_normal(self) -> bool:
        """
        Check if all endpoints are in normal status.

        Returns:
            bool: True if all endpoints are normal, False if no endpoints or any endpoint is abnormal
        """
        with self._endpoint_lock:
            if not self._endpoints:
                logger.debug("[snapshot] No endpoints were pulled up yet")
                return False
            for endpoint in self._endpoints:
                if endpoint.status != EndpointStatus.NORMAL:
                    logger.warning(
                        "Endpoint %d at %s:%s is in status %s",
                        endpoint.id,
                        endpoint.ip,
                        endpoint.mgmt_port,
                        endpoint.status,
                    )
                    return False
        logger.debug("All endpoints are in normal status")
        return True

    def pause_all_endpoints(self) -> None:
        """Set all managed endpoints to PAUSED status for PreStop graceful shutdown.

        After this call:
        - check_all_endpoints_normal() returns False → readiness probe fails
        - Heartbeat reports PAUSED status to Controller
        - Controller triggers instance PAUSE flow
        """
        with self._endpoint_lock:
            for endpoint in self._endpoints:
                endpoint.status = EndpointStatus.PAUSED
        logger.info("All endpoints set to PAUSED for graceful shutdown")

    def get_engine_mgmt_addrs(self) -> list[str]:
        """Return engine management addresses for local metrics polling."""
        with self._endpoint_lock:
            return [format_address(ep.ip, ep.mgmt_port) for ep in self._endpoints]

    def resume_all_endpoints(self) -> None:
        """Resume all endpoints from PAUSED back to NORMAL status.

        Used when PreStop is cancelled. The next heartbeat will report
        NORMAL status, and Controller will trigger instance RESUME flow.
        """
        with self._endpoint_lock:
            for endpoint in self._endpoints:
                if endpoint.status == EndpointStatus.PAUSED:
                    endpoint.status = EndpointStatus.NORMAL
        logger.info("All endpoints resumed to NORMAL")

    def is_started_after_restore(self) -> bool:
        with self._started_after_restore_lock:
            return self._is_started_after_restore

    def set_started_after_restore(self, is_started: bool) -> None:
        with self._started_after_restore_lock:
            self._is_started_after_restore = is_started

    def _refresh_endpoints_status_loop(self) -> None:
        # Poll each engine server's mgmt port until it responds (max 60s)
        self._wait_for_engine_servers_ready(timeout=60)
        while not self.stop_event.is_set():
            self._get_engine_server_status()
            time.sleep(1)

    def _wait_for_engine_servers_ready(self, timeout: float = 60) -> None:
        """Poll each endpoint's mgmt port until it accepts connections or timeout."""
        with self._endpoint_lock:
            endpoints = list(self._endpoints)

        deadline = time.time() + timeout
        daemon = Daemon()
        for endpoint in endpoints:
            address = f"{endpoint.ip}:{endpoint.mgmt_port}"
            logger.info("Waiting for engine server at %s to become ready...", address)
            while not self.stop_event.is_set() and time.time() < deadline:
                try:
                    with socket.create_connection((endpoint.ip, int(endpoint.mgmt_port)), timeout=2):
                        logger.info("Engine server at %s is ready.", address)
                        break
                except (OSError, ConnectionRefusedError, TimeoutError):
                    # Check if engine process is still alive
                    if not any(os.path.isdir(f"/proc/{pid}") for pid in daemon.engine_pids):
                        logger.error("Engine process for %s is no longer running, aborting wait.", address)
                        break
                    time.sleep(1)

    def _get_engine_server_status(self) -> None:
        with self._endpoint_lock:
            endpoints_snapshot = list(self._endpoints)
            generation_at_start = self._endpoints_generation

        if not endpoints_snapshot:
            return

        # Check if within one minute after startup
        if self._is_within_grace_period and self._engine_status_thread_start_time is not None:
            elapsed_time = time.time() - self._engine_status_thread_start_time
            self._is_within_grace_period = elapsed_time < 120

        updated_endpoints = []
        client = None
        for item in endpoints_snapshot:
            original_status = item.status
            client = None
            detected_status = None
            engine_server_base_url = format_address(item.ip, item.mgmt_port)
            try:
                response = EngineServerApiClient.query_status(engine_server_base_url)
                if isinstance(response, dict) and "status" in response:
                    status_value = response.get("status")
                    try:
                        detected_status = EndpointStatus(status_value)
                    except ValueError:
                        logger.error(
                            "Invalid status value '%s' from Engine Server %d: %s",
                            status_value,
                            item.id,
                            engine_server_base_url,
                        )
                        detected_status = EndpointStatus.ABNORMAL
                else:
                    logger.error(
                        "Invalid response format from Engine Server%d: %s: %s",
                        item.id,
                        engine_server_base_url,
                        response,
                    )
                    detected_status = EndpointStatus.ABNORMAL
            except Exception as e:
                if not self._is_within_grace_period:
                    logger.error("Failed to get engine server status from %s: %s", engine_server_base_url, e)
                detected_status = EndpointStatus.ABNORMAL
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception as e:
                        logger.error("Failed to close client: %s", e)

            if is_restored_from_host_side_snapshot() and item.ip != self._config.api_config.pod_ip:
                # If restored from host side snapshot and not started after restore(pod_ip do not refresh yet), keep original status
                logger.info(
                    "[snapshot] Node manager is restored from host side snapshot and not started after restore, "
                    "keeping stale status: %s",
                    original_status,
                )
                item.status = original_status
            elif self._is_within_grace_period and detected_status == EndpointStatus.ABNORMAL:
                # If within grace period and abnormal status detected, do not update status
                logger.debug(
                    "Engine server %s status is abnormal within grace period, keeping original status: %s",
                    engine_server_base_url,
                    original_status,
                )
                item.status = original_status
            # Preserve manually-set PAUSED status (PreStop) — do not overwrite with engine-reported status
            elif original_status == EndpointStatus.PAUSED:
                item.status = original_status
            else:
                item.status = detected_status

            if item.status != original_status:
                logger.info(
                    "Engine Server rank %d, status change from %s to %s ",
                    item.id,
                    original_status,
                    item.status,
                )

            updated_endpoints.append(item)

        with self._endpoint_lock:
            if generation_at_start != self._endpoints_generation:
                return
            self._endpoints = updated_endpoints

    def _report_heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            has_abnormal = False
            is_normal = True
            try:
                with self._endpoint_lock:
                    # Check if any endpoint has abnormal status (only after grace period)
                    # Check actual endpoint status, not the reported status
                    has_abnormal = any(item.status == EndpointStatus.ABNORMAL for item in self._endpoints)
                    is_normal = all(item.status == EndpointStatus.NORMAL for item in self._endpoints)

                    endpoint_status_list = {item.id: item.status for item in self._endpoints}

                # If container snapshot enabled
                # During cold start, when suspend done, node manager should not report heartbeat to controller until engine checkpoint is done
                if (
                    is_normal
                    and not is_restored_from_host_side_snapshot()
                    and not EngineManager().is_engine_checkpoint_done()
                ):
                    if self._checkpoint_done_inspect_retry_count % RETRY_LOG_FREQUENCY == 0:
                        logger.info(
                            "[snapshot] Container snapshot enabled, current container checkpoint is not done, do not report heartbeat to controller..."
                        )
                    self._checkpoint_done_inspect_retry_count += 1
                else:
                    # Container snapshot checkpoint is barrier here, so that a new register can be first triggered after restore from snapshot
                    if is_restored_from_host_side_snapshot() and not self._is_registered_after_restore:
                        logger.warning("[snapshot] Node manager is restored from host side snapshot, registering...")
                        self._register_after_restore()
                        time.sleep(self.heartbeat_interval_seconds)
                        continue

                    # Build message and send request outside of lock
                    heartbeat_msg = HeartbeatMsg(
                        job_name=self._job_name,
                        ins_id=self._instance_id,
                        ip=self._config.api_config.pod_ip,
                        status=endpoint_status_list,
                    )

                    ControllerApiClient.report_heartbeat(heartbeat_msg)

            except Exception as e:
                # Exception triggered by host side snapshot restore, nodeManager re-send register message
                if is_restored_from_host_side_snapshot() and not self._is_registered_after_restore:
                    logger.warning("[snapshot] Node manager is restored from host side snapshot, registering...")
                    self._register_after_restore()
                elif "503" in str(e):
                    if not is_restored_from_host_side_snapshot() or self.is_started_after_restore():
                        logger.warning("Received 503, maybe controller has been restarted, reregistering...")
                        self._reregister()
                else:
                    with self.config_lock:
                        logger.error("Exception occurred while reporting endpoint status to controller: %s", e)

            # Update consecutive abnormal count after successful heartbeat report
            with self._abnormal_count_lock:
                if has_abnormal:
                    self._consecutive_abnormal_count += 1
                    logger.warning("Consecutive abnormal heartbeat count: %d/5", self._consecutive_abnormal_count)
                    # Set suicide flag if reached 5 consecutive abnormal heartbeats
                    if self._consecutive_abnormal_count >= 5:
                        logger.error(
                            "Reached 5 consecutive abnormal heartbeats, setting suicide flag for main to handle..."
                        )
                        with self._suicide_lock:
                            self._should_suicide = True
                else:
                    self._consecutive_abnormal_count = 0

            with self.config_lock:
                time.sleep(self.heartbeat_interval_seconds)

    def _register_after_restore(self) -> None:
        # refresh config: job_name from snapshot metadata and new pod ip
        try:
            EngineManager().register_prepare_after_restore()
        except Exception as e:
            if self._register_after_restore_retry_count % RETRY_LOG_FREQUENCY == 0:
                logger.error("[snapshot] Failed to register prepare after restore: %s", e)
            self._register_after_restore_retry_count += 1
            return

        # Register for post-snapshot brandnew job name
        # Do not consider retry
        # If current register failed, next register will be triggered by next heartbeat report exception
        ret = EngineManager().post_register_msg_after_restore()
        self._is_registered_after_restore = ret is True

    def _reregister(self) -> None:
        ret = EngineManager().post_reregister_msg()
        if ret is False:
            logger.error("reregister failed")
        else:
            logger.info("reregister success")
