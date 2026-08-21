# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import threading
import time

from motor.common.resources.endpoint import Endpoint, EndpointStatus
from motor.common.resources.http_msg_spec import StartCmdMsg, HeartbeatMsg
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.common.utils.net import format_address
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.utils.snapshot_utils import is_restored_from_host_side_snapshot, RETRY_LOG_FREQUENCY
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.api_client.controller_api_client import ControllerApiClient
from motor.node_manager.core.register_manager import RegisterManager
from motor.node_manager.core.services.native_engine.models import RuntimeState


logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)


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
        # The status thread is created in start() — it needs the Daemon's
        # engine-ready event injected at that point.
        self._engine_status_thread: threading.Thread | None = None
        self._thread_started = False
        self._engine_status_thread_start_time = None
        self._is_within_grace_period = True
        # for snapshot
        self._register_after_restore_retry_count = 0
        self._checkpoint_done_inspect_retry_count = 0
        self._is_registered_after_restore = False
        self._is_started_after_restore = False
        self._started_after_restore_lock = threading.Lock()
        self._endpoints_generation = 0

        self._initialized = True
        logger.info("HeartBeatManager module start.")

    def start(self, engine_ready_event: threading.Event | None = None) -> None:
        """Start the heartbeat and engine-status threads.

        ``engine_ready_event`` (the Daemon's engine-ready handoff) gates the
        status polling: the status thread waits for it before probing the
        engines, so the HeartbeatManager has no engine-readiness logic of its
        own. None (snapshot-restore path, no engines pulled) starts probing
        immediately.
        """
        if self._thread_started is False:
            self._engine_status_thread = threading.Thread(
                target=self._refresh_endpoints_status_loop,
                args=(engine_ready_event,),
                daemon=True,
                name="endpoint_status_fetch",
            )
            self._heartbeat_report_thread.start()
            self._engine_status_thread.start()
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
        self._is_within_grace_period = True
        if self._thread_started:
            self._engine_status_thread_start_time = time.time()

    def has_abnormal_endpoints(self) -> bool:
        """True when any managed endpoint is ABNORMAL.

        State fact for the Daemon's suicide arbitration — the heartbeat
        module only maintains endpoint state; the Daemon decides whether to
        kill the pod.
        """
        with self._endpoint_lock:
            return any(item.status == EndpointStatus.ABNORMAL for item in self._endpoints)

    def normal_endpoint_ids(self) -> list[int]:
        """Ids of the endpoints currently NORMAL.

        State fact for the Daemon's suicide arbitration: an endpoint that was
        never NORMAL is still cold-starting (model loading) — its ABNORMAL
        observations must not be reported as engine death.
        """
        with self._endpoint_lock:
            return [item.id for item in self._endpoints if item.status == EndpointStatus.NORMAL]

    def abnormal_endpoint_ids(self) -> list[int]:
        """Ids of the endpoints currently ABNORMAL.

        ABNORMAL covers both native-engine process death and business-level
        failure (engine alive but its internal executor died — vLLM EngineCore
        crash) — either way the engine is unusable and must be relaunched.
        """
        with self._endpoint_lock:
            return [item.id for item in self._endpoints if item.status == EndpointStatus.ABNORMAL]

    def endpoints_generation(self) -> int:
        """Incremented on every endpoint update (Daemon resets its suicide
        count when this changes).
        """
        with self._endpoint_lock:
            return self._endpoints_generation

    def is_within_grace_period(self) -> bool:
        """True during the 120s cold-start window after endpoints were set."""
        with self._endpoint_lock:
            return self._is_within_grace_period

    def stop(self) -> None:
        self.stop_event.set()
        if self._heartbeat_report_thread.is_alive():
            self._heartbeat_report_thread.join(timeout=2.0)
        if self._engine_status_thread is not None and self._engine_status_thread.is_alive():
            self._engine_status_thread.join(timeout=2.0)
        logger.info("HeartBeatManager stopped.")

    def check_all_endpoints_recovering(self) -> bool:
        """True when no managed endpoint is ABNORMAL.

        Relaxed readiness for the engine-relaunch flow: an engine that was
        just relaunched reports INITIAL while loading its model — the relaunch
        succeeded as soon as every endpoint is past ABNORMAL (engine process
        alive); model readiness is tracked separately by NORMAL.
        """
        with self._endpoint_lock:
            if not self._endpoints:
                return False
            for endpoint in self._endpoints:
                if endpoint.status == EndpointStatus.ABNORMAL:
                    return False
        return True

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

    def get_engine_metrics_targets(self) -> list[str]:
        """Return native metrics URLs for routable local endpoints."""
        # Lazy import: the Daemon imports this module at top level; this
        # runtime lookup keeps the module graph acyclic.
        from motor.node_manager.core.daemon import Daemon  # pylint: disable=cyclic-import

        with self._endpoint_lock:
            endpoints = [endpoint for endpoint in self._endpoints if not endpoint.headless]
        daemon = Daemon()
        return [target for endpoint in endpoints if (target := daemon.get_engine_metrics_target(endpoint)) is not None]

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

    def _refresh_endpoints_status_loop(self, engine_ready_event: threading.Event | None = None) -> None:
        # Wait for the Daemon's engine-ready handoff (mgmt ports up) before
        # probing — engine readiness is the Daemon's/engine service's concern,
        # not this module's. The event is always set eventually (also when the
        # engines died), so this never blocks indefinitely.
        if engine_ready_event is not None:
            while not self.stop_event.is_set():
                if engine_ready_event.wait(timeout=1.0):
                    break
            if self.stop_event.is_set():
                return
        while not self.stop_event.is_set():
            self._refresh_native_engine_status()
            self.stop_event.wait(1)

    def _refresh_native_engine_status(self) -> None:
        with self._endpoint_lock:
            endpoints_snapshot = list(self._endpoints)
            generation_at_start = self._endpoints_generation
            instance_id_at_start = self._instance_id

        if not endpoints_snapshot:
            return

        # Lazy import: the Daemon imports this module at top level; this
        # runtime lookup keeps the module graph acyclic.
        from motor.node_manager.core.daemon import Daemon  # pylint: disable=cyclic-import

        updated_endpoints = []
        daemon = Daemon()
        for item in endpoints_snapshot:
            original_status = item.status
            try:
                runtime_state = daemon.get_engine_runtime_state(item, instance_id_at_start)
            except Exception as e:
                logger.error(
                    "Failed to probe native engine at %s: %s",
                    format_address(item.ip, item.business_port),
                    e,
                )
                runtime_state = RuntimeState.UNHEALTHY

            detected_status = {
                RuntimeState.RUNNING: EndpointStatus.WAIT2START,
                RuntimeState.READY: EndpointStatus.NORMAL,
                RuntimeState.UNHEALTHY: EndpointStatus.ABNORMAL,
                RuntimeState.STOPPED: EndpointStatus.ABNORMAL,
            }.get(runtime_state)

            if is_restored_from_host_side_snapshot() and item.ip != self._config.api_config.pod_ip:
                # If restored from host side snapshot and not started after restore(pod_ip do not refresh yet), keep original status
                logger.info(
                    "[snapshot] Node manager is restored from host side snapshot and not started after restore, "
                    "keeping stale status: %s, old endpoint ip=%s, new endpoint ip=%s",
                    original_status,
                    item.ip,
                    self._config.api_config.pod_ip,
                )
                item.status = original_status
            elif runtime_state in (RuntimeState.STARTING, RuntimeState.STOPPING):
                # Loading is not a failure. Keep INITIAL (or the last reported
                # status) until the native readiness probe succeeds.
                logger.debug(
                    "Native engine %s is %s, keeping status %s",
                    format_address(item.ip, item.business_port),
                    runtime_state.value,
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
                    "Native engine rank %d, status change from %s to %s ",
                    item.id,
                    original_status,
                    item.status,
                )

            updated_endpoints.append(item)

        with self._endpoint_lock:
            if generation_at_start != self._endpoints_generation:
                return
            self._endpoints = updated_endpoints
            # Once an endpoint has proved READY, subsequent ABNORMAL states
            # are runtime failures rather than cold-start noise.  Ending the
            # global grace period lets the Daemon report those failures; its
            # per-endpoint NORMAL history still protects endpoints that have
            # not completed startup yet.
            if any(item.status == EndpointStatus.NORMAL for item in updated_endpoints):
                self._is_within_grace_period = False

    def _report_heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            is_normal = True
            try:
                with self._endpoint_lock:
                    is_normal = all(item.status == EndpointStatus.NORMAL for item in self._endpoints)

                    endpoint_status_list = {item.id: item.status for item in self._endpoints}

                # If container snapshot enabled
                # During cold start, when suspend done, node manager should not report heartbeat to controller until engine checkpoint is done
                if (
                    is_normal
                    and not is_restored_from_host_side_snapshot()
                    and not RegisterManager().is_engine_checkpoint_done()
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
                        _rl.error_window(
                            "heartbeat.report_error",
                            "Exception occurred while reporting endpoint status to controller: %s" % e,
                            window_sec=60,
                        )

            # Suicide arbitration moved to the Daemon's process monitor:
            # this module only reports the endpoint-state facts.
            with self.config_lock:
                time.sleep(self.heartbeat_interval_seconds)

    def _register_after_restore(self) -> None:
        # refresh config: job_name from snapshot metadata and new pod ip
        try:
            RegisterManager().register_prepare_after_restore()
        except Exception as e:
            if self._register_after_restore_retry_count % RETRY_LOG_FREQUENCY == 0:
                logger.error("[snapshot] Failed to register prepare after restore: %s", e)
            self._register_after_restore_retry_count += 1
            return

        # Register for post-snapshot brandnew job name
        # Do not consider retry
        # If current register failed, next register will be triggered by next heartbeat report exception
        ret = RegisterManager().post_register_msg()
        self._is_registered_after_restore = ret is True

    def _reregister(self) -> None:
        ret = RegisterManager().post_reregister_msg()
        if ret is False:
            logger.error("reregister failed")
        else:
            logger.info("reregister success")
