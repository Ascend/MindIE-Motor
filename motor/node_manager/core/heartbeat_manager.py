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
from motor.common.utils.singleton import ThreadSafeSingleton
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
            bool: True if all endpoints are normal, False if any endpoint is abnormal
        """
        with self._endpoint_lock:
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
            engine_server_base_url = f"{item.ip}:{item.mgmt_port}"
            try:
                response = EngineServerApiClient.query_status(engine_server_base_url)
                if isinstance(response, dict) and "status" in response:
                    status_value = response.get("status")
                    try:
                        detected_status = EndpointStatus(status_value)
                        if detected_status != original_status:
                            logger.info(
                                "Engine Server rank %d, status change from %s to %s ",
                                item.id,
                                original_status,
                                detected_status,
                            )
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

            # If within grace period and abnormal status detected, do not update status
            if self._is_within_grace_period and detected_status == EndpointStatus.ABNORMAL:
                logger.debug(
                    "Engine server %s status is abnormal within grace period, keeping original status: %s",
                    engine_server_base_url,
                    original_status,
                )
                item.status = original_status
            else:
                item.status = detected_status

            updated_endpoints.append(item)

        with self._endpoint_lock:
            self._endpoints = updated_endpoints

    def _report_heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                with self._endpoint_lock:
                    # Check if any endpoint has abnormal status (only after grace period)
                    # Check actual endpoint status, not the reported status
                    has_abnormal = any(item.status == EndpointStatus.ABNORMAL for item in self._endpoints)

                    endpoint_status_list = {item.id: item.status for item in self._endpoints}

                # Build message and send request outside of lock
                heartbeat_msg = HeartbeatMsg(
                    job_name=self._job_name,
                    ins_id=self._instance_id,
                    ip=self._config.api_config.pod_ip,
                    status=endpoint_status_list,
                )

                ControllerApiClient.report_heartbeat(heartbeat_msg)

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

            except Exception as e:
                if "503" in str(e):
                    logger.warning("Received 503, maybe controller has been restarted, reregistering...")
                    self._reregister()
                else:
                    with self.config_lock:
                        logger.error("Exception occurred while reporting endpoint status to controller: %s", e)

            with self.config_lock:
                time.sleep(self.heartbeat_interval_seconds)

    def _reregister(self) -> None:
        ret = EngineManager().post_reregister_msg()
        if ret is False:
            logger.error("reregister failed")
        else:
            logger.info("reregister success")
