# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Management plane: runs in the dedicated Mgmt process only (spawned by CoordinatorDaemon via MgmtProcessManager).
Provides readiness, liveness, metrics, instances/refresh.
Does not create or start inference Workers; those are started by CoordinatorDaemon via InferenceProcessManager.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status

from motor.common.resources.http_msg_spec import InsEventMsg
from motor.common.http.cert_util import CertUtil
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.common.http.security_utils import sanitize_error_message, log_audit_event
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.models.response import RequestResponse
from motor.coordinator.api_server.base_server import BaseCoordinatorServer
from motor.coordinator.scheduler.runtime import SchedulerConnectionManager
from motor.coordinator.api_server.app_builder import AppBuilder
from motor.coordinator.api_client.conductor_api_client import ConductorApiClient
from motor.coordinator.domain.instance_manager import InstanceManager, TYPE_MGMT
from motor.coordinator.domain.probe import (
    DaemonLivenessProvider,
    LivenessProbe,
    LivenessResult,
    ReadinessProbe,
    ReadinessResult,
    RoleShmDaemonLivenessProvider,
)

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)
_READINESS_REMAINS_READY_KEY = "coordinator.readiness.remains_ready"

# Readiness 503: result -> HTTP detail.
_READINESS_503: dict[ReadinessResult, str] = {
    ReadinessResult.DAEMON_EXITED: "Coordinator daemon has exited; not ready",
    ReadinessResult.HEARTBEAT_STALE: "Coordinator daemon heartbeat stale; not ready",
    ReadinessResult.NOT_MASTER: "Coordinator is not master",
}

# Request body limits for /instances/refresh
_MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024  # 10MB
_REQUEST_BODY_PREVIEW_LENGTH = 200


def _build_ok_response(message: str) -> dict[str, str]:
    return {"status": "ok", "message": message}


def _build_readiness_response(message: str, ready: bool) -> dict[str, Any]:
    return {"status": "ok", "message": message, "ready": ready}


INSTANCE_REFRESH = "instance_refresh"
INSTANCE_REFRESH_URL = "/instances/refresh"


class ManagementServer(BaseCoordinatorServer):
    """
    Management plane: runs in the Mgmt process only (spawned by MgmtProcessManager); does not start inference Workers.
    """

    def __init__(
        self,
        config: CoordinatorConfig | None = None,
        instance_manager: InstanceManager | None = None,
        daemon_pid: int | None = None,
        daemon_liveness: DaemonLivenessProvider | None = None,
    ):
        super().__init__(config)
        self._mgmt_ssl_config = self.coordinator_config.mgmt_tls_config
        self._daemon_liveness = daemon_liveness or RoleShmDaemonLivenessProvider(
            daemon_pid=daemon_pid,
        )
        self._liveness_probe = LivenessProbe(self._daemon_liveness)
        # Create dependencies before app so lifespan and routes see them (lifespan runs on uvicorn start)
        self._scheduler_connection = SchedulerConnectionManager.from_config(self.coordinator_config)
        self._instance_manager = (
            instance_manager if instance_manager is not None else InstanceManager(self.coordinator_config, TYPE_MGMT)
        )
        self._readiness_probe = ReadinessProbe(
            self._daemon_liveness,
            self._instance_manager,
            enable_master_standby=self.coordinator_config.standby_config.enable_master_standby,
        )
        self._app_builder = AppBuilder(self.coordinator_config)
        self.management_app = self._app_builder.create_management_app(lifespan=self._lifespan)
        self._readiness_was_ready: bool | None = None
        self._readiness_last_503_result: ReadinessResult | None = None
        self._re_register_task: asyncio.Task | None = None
        self._re_register_executor: ThreadPoolExecutor | None = None
        self._register_routes()

    @property
    def instance_manager(self) -> InstanceManager:
        """Public accessor for Mgmt process InstanceManager (G.CLS.11: avoid protected access)."""
        return self._instance_manager

    @instance_manager.setter
    def instance_manager(self, value: InstanceManager) -> None:
        """Allow tests to inject a custom instance manager."""
        self._instance_manager = value
        self._readiness_probe.instance_manager = value

    @property
    def lifespan(self):
        """Public accessor for lifespan context manager (G.CLS.11: avoid protected access)."""
        return self._lifespan

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        logger.info("Management server is starting...")
        await self._scheduler_connection.connect()
        self._start_re_register_task()
        try:
            yield
        except asyncio.CancelledError:
            logger.info("Management server startup was cancelled")
        except Exception as e:
            logger.error("Management server startup failed: %s", e)
            raise
        finally:
            await self._stop_re_register_task()
            logger.info("Management server is shutting down...")
            await self._scheduler_connection.disconnect()

    async def run(self) -> None:
        """Run uvicorn on management port only; does not create or start inference Workers."""
        mgmt_config_kwargs = self.create_base_uvicorn_config(
            self.management_app,
            self.coordinator_config.api_config.coordinator_api_host,
            self.coordinator_config.api_config.coordinator_api_mgmt_port,
        )
        self.apply_timeout_to_config(mgmt_config_kwargs)
        mgmt_config = uvicorn.Config(**mgmt_config_kwargs)
        mgmt_config.load()
        if self._mgmt_ssl_config and self._mgmt_ssl_config.enable_tls:
            mgmt_ssl_context = CertUtil.create_ssl_context(tls_config=self._mgmt_ssl_config)
            if mgmt_ssl_context:
                mgmt_config.ssl = mgmt_ssl_context
        mgmt_server = uvicorn.Server(mgmt_config)
        await mgmt_server.serve()

    def _start_re_register_task(self) -> None:
        """Start the periodic KV instance re-register background task."""
        kv_reg = self.coordinator_config.scheduler_config.kv_conductor_config
        interval = kv_reg.re_register_interval_sec
        if interval <= 0:
            # Fallback to legacy prefill_kv_event_config for backward compatibility
            interval = self.coordinator_config.prefill_kv_event_config.re_register_interval_sec
        if interval <= 0:
            logger.info("KV instance re-register timer is disabled (re_register_interval_sec=%s)", interval)
            return
        logger.info("Starting KV instance re-register timer (interval=%ss)", interval)
        self._re_register_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="re-register")
        self._re_register_task = asyncio.create_task(self._re_register_loop(interval))

    async def _stop_re_register_task(self) -> None:
        """Cancel the periodic KV instance re-register background task."""
        task = self._re_register_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._re_register_task = None
        if self._re_register_executor is not None:
            self._re_register_executor.shutdown(wait=False)
            self._re_register_executor = None

    async def _re_register_loop(self, interval: int) -> None:
        """Periodically re-register KV instances to Conductor."""
        while True:
            try:
                await asyncio.sleep(interval)
                await self._do_re_register()
            except asyncio.CancelledError:
                logger.info("KV instance re-register loop cancelled")
                break
            except Exception as e:
                logger.error("Unexpected error in KV instance re-register loop: %s", e)

    async def _do_re_register(self) -> None:
        """Re-register all KVA-eligible instances to Conductor if missing."""
        instances_view = self._instance_manager.get_available_instances(role=None)
        instances = list(instances_view.values())
        if not instances:
            logger.debug("No instances available for KV re-register, skip")
            return
        executor = self._re_register_executor
        if executor is None:
            logger.warning("KV re-register executor not available, skip")
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(executor, ConductorApiClient.re_register_kv_instances, instances)

    def _apply_config_changes(self, new_config: CoordinatorConfig) -> None:
        """Apply Mgmt-specific config changes."""
        self._mgmt_ssl_config = new_config.mgmt_tls_config

    def _log_configuration(self) -> None:
        super()._log_configuration()
        logger.info(
            "Mgmt SSL configuration: enable_tls=%s",
            self.coordinator_config.mgmt_tls_config.enable_tls,
        )
        if self.coordinator_config.mgmt_tls_config.enable_tls:
            logger.info(
                "Mgmt SSL: cert_file=%s, key_file=%s, ca_file=%s",
                self.coordinator_config.mgmt_tls_config.cert_file,
                self.coordinator_config.mgmt_tls_config.key_file,
                self.coordinator_config.mgmt_tls_config.ca_file,
            )

    def _register_routes(self) -> None:
        @self.management_app.get("/startup")
        async def startup_probe():
            logger.debug("Received startup probe request")
            return _build_ok_response("Coordinator is starting up")

        @self.management_app.get("/liveness")
        async def liveness_check():
            result = self._liveness_probe.check()
            if result == LivenessResult.OK:
                logger.debug("Received liveness check request, Coordinator is alive")
                return _build_ok_response("Coordinator is alive")
            if result == LivenessResult.DAEMON_EXITED:
                logger.warning(
                    "[Liveness] Daemon has exited (Mgmt orphaned), failing liveness for pod restart",
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Coordinator daemon has exited; liveness failed for pod restart",
                )
            logger.warning(
                "[Liveness] Daemon heartbeat stale, failing liveness for pod restart",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Coordinator daemon heartbeat stale; liveness failed for pod restart",
            )

        @self.management_app.get("/readiness")
        async def readiness_check(request: Request):
            # Note: If this returns ready=False (e.g. no required instances), K8s removes the pod from
            # the Service. Then the controller's POST /instances/refresh cannot reach this pod (deadlock).
            logger.debug("[Readiness] Probe received")
            try:
                out = await self._readiness_probe.check()
            except HTTPException:
                raise
            except Exception as e:
                logger.exception("[Readiness] Probe failed: %s", e)
                raise e from e

            instances_status = out.instance_readiness.value if out.instance_readiness else None
            if out.result in _READINESS_503:
                if out.result != self._readiness_last_503_result:
                    self._readiness_last_503_result = out.result
                    logger.warning(
                        "[Readiness] Returning 503, result=%s. "
                        "Check: daemon alive, master/standby role, role_heartbeat_interval_sec",
                        out.result.value,
                    )
                self._readiness_was_ready = False
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=_READINESS_503[out.result],
                )

            self._readiness_last_503_result = None
            msg = "Coordinator is master" if out.result == ReadinessResult.OK_MASTER else "Coordinator is ok"
            # Log INFO/WARNING only on ready transitions; steady probes stay DEBUG.
            prev_ready = self._readiness_was_ready
            if out.is_ready:
                if not prev_ready:
                    logger.info(
                        "[Readiness] Coordinator is ready. result=%s instances_status=%s",
                        out.result.value,
                        instances_status,
                    )
                else:
                    logger.debug(
                        "[Readiness] Coordinator remains ready. result=%s instances_status=%s",
                        out.result.value,
                        instances_status,
                    )
                    _rl.record_success(_READINESS_REMAINS_READY_KEY)
                    _rl.emit_periodic(
                        _READINESS_REMAINS_READY_KEY,
                        "[Readiness] Coordinator remains ready periodic summary: "
                        "probe succeeded {count} times in last 60s, result=%s instances_status=%s"
                        % (out.result.value, instances_status),
                    )
            else:
                if prev_ready:
                    logger.warning(
                        "[Readiness] Coordinator is no longer ready. result=%s instances_status=%s",
                        out.result.value,
                        instances_status,
                    )
                else:
                    logger.debug(
                        "[Readiness] Coordinator is not ready yet. result=%s instances_status=%s",
                        out.result.value,
                        instances_status,
                    )
            self._readiness_was_ready = out.is_ready
            return _build_readiness_response(msg, out.is_ready)

        @self.management_app.post("/instances/refresh", response_model=RequestResponse)
        @self.timeout_handler()
        async def refresh_instances(request: Request) -> RequestResponse:
            try:
                result = await self._handle_refresh_instances(request)
                log_audit_event(
                    request=request,
                    event_type=INSTANCE_REFRESH,
                    resource_name=INSTANCE_REFRESH_URL,
                    event_result="success",
                )
                return result
            except Exception as e:
                log_audit_event(
                    request=request,
                    event_type=INSTANCE_REFRESH,
                    resource_name=INSTANCE_REFRESH_URL,
                    event_result=f"failed: {sanitize_error_message(str(e))[:100]}",
                )
                raise

        @self.management_app.get("/")
        async def root():
            return {
                "service": "Motor Coordinator Management Server",
                "version": "1.0.0",
                "description": "Management plane: liveness, startup, readiness, metrics, instance refresh",
                "endpoints": {
                    "GET /liveness": "liveness check",
                    "GET /startup": "startup probe",
                    "GET /readiness": "readiness check",
                    "POST /instances/refresh": "refresh instances",
                },
            }

    async def _handle_refresh_instances(self, request: Request) -> RequestResponse:
        try:
            raw_body = await request.body()
            if not raw_body:
                logger.error("Request body is empty")
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Request body cannot be empty",
                )
            if len(raw_body) > _MAX_REQUEST_BODY_SIZE:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"Request body size exceeds maximum allowed size of {_MAX_REQUEST_BODY_SIZE // (1024 * 1024)}MB"
                    ),
                )
            body = json.loads(raw_body.decode("utf-8"))
            if not body:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Request body cannot be empty",
                )
        except HTTPException:
            raise
        except json.JSONDecodeError as e:
            logger.error("Failed to parse request body as JSON: %s", e)
            preview = raw_body.decode("utf-8", errors="ignore")[:_REQUEST_BODY_PREVIEW_LENGTH] if raw_body else "empty"
            logger.error("Request body (first %s chars): %s", _REQUEST_BODY_PREVIEW_LENGTH, preview)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid JSON format: {str(e)}",
            ) from e
        except Exception as e:
            logger.error("Failed to parse request body: %s, type: %s", e, type(e))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to parse request body: {str(e)}",
            ) from e

        try:
            event_msg = InsEventMsg(**body)
        except Exception as e:
            body_keys = list(body.keys()) if isinstance(body, dict) else "not a dict"
            logger.error("Failed to parse InsEventMsg: %s, body keys: %s", e, body_keys)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid request format: {str(e)}",
            ) from e

        await self._scheduler_connection.ensure_connected()
        client = self._scheduler_connection.get_client()
        if client is not None:
            await client.refresh_instances(event_msg.event, event_msg.instances)
        await self._instance_manager.refresh_instances(event_msg.event, event_msg.instances)

        return RequestResponse(
            request_id="refresh_request",
            status="success",
            message="Instance refresh completed",
            data={
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event_type": event_msg.event.value,
                "instance_count": len(event_msg.instances),
            },
        )
