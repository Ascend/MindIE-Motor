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
Provides readiness, liveness, metrics, instances/refresh, instances list.
Does not create or start inference Workers; those are started by CoordinatorDaemon via InferenceProcessManager.
"""

import asyncio
import json
import os
import secrets
import signal
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status

from motor.common.resources.http_msg_spec import EventType, ExternalInsEventMsg, InsEventMsg
from motor.common.http.cert_util import CertUtil
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.common.http.security_utils import sanitize_error_message, log_audit_event
from motor.config.coordinator import CoordinatorConfig, DEFAULT_SCHEDULER_PROCESS_CONFIG, MGMT_API_KEY_HEADER
from motor.coordinator.models.response import RequestResponse
from motor.coordinator.api_server.base_server import BaseCoordinatorServer
from motor.coordinator.api_server.app_builder import AppBuilder
from motor.coordinator.scheduler.runtime.scheduler_server import AsyncSchedulerServer
from motor.coordinator.api_client.conductor_api_client import ConductorApiClient
from motor.coordinator.api_client.native_engine_api_client import NativeEngineApiClient
from motor.coordinator.domain.instance_manager import InstanceIdConflictError, InstanceManager, TYPE_MGMT
from motor.coordinator.domain.instance_status import build_instance_status_response
from motor.coordinator.domain.probe import (
    DaemonLivenessProvider,
    LivenessProbe,
    LivenessResult,
    ReadinessProbe,
    ReadinessResult,
    RoleShmDaemonLivenessProvider,
)
from motor.coordinator.render.vllm_render_client import VLLMRenderClient

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)
_READINESS_REMAINS_READY_KEY = "coordinator.readiness.remains_ready"
_RENDER_HEALTH_RETRY_SECONDS = 5.0
_EXTERNAL_MODEL_PROBE_WORKERS = 8
_RENDER_HEALTH_FAIL_THRESHOLD = 3


def _in_kubernetes() -> bool:
    return bool(os.getenv("KUBERNETES_SERVICE_HOST") or os.getenv("POD_NAMESPACE"))


# Readiness 503: result -> HTTP detail.
_READINESS_503: dict[ReadinessResult, str] = {
    ReadinessResult.DAEMON_EXITED: "Coordinator daemon has exited; not ready",
    ReadinessResult.HEARTBEAT_STALE: "Coordinator daemon heartbeat stale; not ready",
    ReadinessResult.NOT_MASTER: "Coordinator is not master",
}

# Request body limits for /instances/refresh
_MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024  # 10MB
_REQUEST_BODY_PREVIEW_LENGTH = 200

# Top-level fields that only appear in coordinator-standalone External Deployer events.
_STANDALONE_TOP_LEVEL_MARKERS = frozenset({"model_name", "dispatch_capabilities", "engine_type"})


def _is_standalone_instance_refresh(body: Any) -> bool:
    """Return True when the payload follows the coordinator-standalone External protocol.

    Controller deployments send full InsEventMsg instances (job_name plus dict endpoints).
    Standalone External Deployer events may omit all top-level optional fields and only
    provide id/role/endpoints[], where endpoints is a list of {"id": 0, "address": "host:port"}.
    """
    if not isinstance(body, dict):
        return False
    if any(marker in body for marker in _STANDALONE_TOP_LEVEL_MARKERS):
        return True

    instances = body.get("instances")
    if not isinstance(instances, list) or not instances:
        return False

    saw_standalone_shape = False
    saw_controller_shape = False
    for instance in instances:
        if not isinstance(instance, dict):
            continue
        if "job_name" in instance:
            saw_controller_shape = True
            continue
        endpoints = instance.get("endpoints")
        if isinstance(endpoints, list):
            saw_standalone_shape = True
        elif isinstance(endpoints, dict):
            saw_controller_shape = True

    if saw_standalone_shape and saw_controller_shape:
        raise ValueError("mixed controller and coordinator-standalone instance payloads in one request")
    return saw_standalone_shape


def _build_ok_response(message: str) -> dict[str, str]:
    return {"status": "ok", "message": message}


def _build_readiness_response(message: str, ready: bool) -> dict[str, Any]:
    return {"status": "ok", "message": message, "ready": ready}


INSTANCE_REFRESH = "instance_refresh"
INSTANCE_REFRESH_URL = "/instances/refresh"
PRECISION_ALARM_CLEARED = "precision_alarm_cleared"
PRECISION_ALARM_CLEARED_URL = "/precision/alarm_cleared"


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
        self._mgmt_api_key_config = self.coordinator_config.mgmt_api_key_config
        self._mgmt_api_key = self._load_mgmt_api_key()
        self._daemon_pid = daemon_pid
        self._daemon_liveness = daemon_liveness or RoleShmDaemonLivenessProvider(
            daemon_pid=daemon_pid,
        )
        self._liveness_probe = LivenessProbe(self._daemon_liveness)
        # Create dependencies before app so lifespan and routes see them (lifespan runs on uvicorn start)
        self._instance_manager = (
            instance_manager if instance_manager is not None else InstanceManager(self.coordinator_config, TYPE_MGMT)
        )
        self._control_plane = AsyncSchedulerServer(
            self.coordinator_config,
            frontend_address=DEFAULT_SCHEDULER_PROCESS_CONFIG.frontend_address,
            instance_manager=self._instance_manager,
        )
        self._readiness_probe = ReadinessProbe(
            self._daemon_liveness,
            self._instance_manager,
            enable_master_standby=self.coordinator_config.standby_config.enable_master_standby,
            allow_decode_only=(self.coordinator_config.scheduler_config.enable_pd_separation_fallback_to_hybrid),
        )
        self._app_builder = AppBuilder(self.coordinator_config)
        self.management_app = self._app_builder.create_management_app(lifespan=self._lifespan)
        self._readiness_was_ready: bool | None = None
        self._readiness_last_503_result: ReadinessResult | None = None
        self._refresh_lock = asyncio.Lock()
        self._re_register_task: asyncio.Task | None = None
        self._re_register_executor: ThreadPoolExecutor | None = None
        self._render_health_task: asyncio.Task | None = None
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
        control = getattr(self, "_control_plane", None)
        if control is not None:
            control.instance_manager = value

    @property
    def lifespan(self):
        """Public accessor for lifespan context manager (G.CLS.11: avoid protected access)."""
        return self._lifespan

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        logger.info("Management server is starting...")
        await self._start_control_plane()
        self._start_re_register_task()
        try:
            yield
        except asyncio.CancelledError:
            logger.info("Management server startup was cancelled")
        except Exception as e:
            logger.error("Management server startup failed: %s", e)
            raise
        finally:
            await self._stop_render_health_observer()
            await self._stop_re_register_task()
            logger.info("Management server is shutting down...")
            await self._stop_control_plane()

    async def _start_control_plane(self) -> None:
        """Bind ROUTER/PUB and create schema-4 SHM, then serve control-plane RPCs in-process."""
        await self._control_plane.start_control_plane()
        self._control_plane._loop_task = asyncio.create_task(self._control_plane.run_request_loop())

    async def _stop_control_plane(self) -> None:
        """Tear down control-plane sockets and SHM."""
        await self._control_plane.stop()

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
        kv_config = self.coordinator_config.scheduler_config.kv_conductor_config
        interval = kv_config.re_register_interval_sec
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

    def _start_render_health_observer(self) -> None:
        """Start Render health observation after Coordinator becomes ready."""
        if not self.coordinator_config.render_config.enable or self._render_health_task is not None:
            return
        self._render_health_task = asyncio.create_task(
            self._observe_render_health(),
            name="render-health-observer",
        )

    async def _stop_render_health_observer(self) -> None:
        """Cancel the Render health observer during Mgmt shutdown."""
        task = self._render_health_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._render_health_task = None

    def _stop_coordinator_for_render(self) -> None:
        """Take the Coordinator daemon down so Docker does not keep serving."""
        logger.error("vLLM Render sidecar is down; stopping Coordinator (docker fail-closed)")
        daemon_pid = self._daemon_pid
        if daemon_pid:
            try:
                os.kill(daemon_pid, signal.SIGTERM)
                return
            except OSError as exc:
                logger.error("Failed to signal Coordinator daemon pid=%s: %s", daemon_pid, exc)
        logger.error("Coordinator daemon pid is unavailable; cannot fail-close with Render")

    async def _observe_render_health(self) -> None:
        """Wait until Render is ready; Docker then heartbeats and fail-closes."""
        client = VLLMRenderClient(self.coordinator_config.render_config)
        unavailable_logged = False
        ready = False
        consecutive_failures = 0
        try:
            while True:
                if await client.health():
                    if not ready:
                        logger.info("vLLM Render sidecar is ready")
                        ready = True
                        if _in_kubernetes():
                            return
                    consecutive_failures = 0
                elif not ready:
                    if not unavailable_logged:
                        logger.warning("vLLM Render sidecar is unavailable; Coordinator will use tokenizer fallback")
                        unavailable_logged = True
                else:
                    consecutive_failures += 1
                    logger.warning(
                        "vLLM Render sidecar heartbeat failed (%s/%s)",
                        consecutive_failures,
                        _RENDER_HEALTH_FAIL_THRESHOLD,
                    )
                    if consecutive_failures >= _RENDER_HEALTH_FAIL_THRESHOLD:
                        self._stop_coordinator_for_render()
                        return
                await asyncio.sleep(_RENDER_HEALTH_RETRY_SECONDS)
        finally:
            await client.aclose()

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
        new_mgmt_api_key_config = new_config.mgmt_api_key_config
        new_mgmt_api_key = new_mgmt_api_key_config.load_api_key() if new_mgmt_api_key_config.enable_api_key else ""
        self._mgmt_ssl_config = new_config.mgmt_tls_config
        self._mgmt_api_key_config = new_mgmt_api_key_config
        self._mgmt_api_key = new_mgmt_api_key
        self._readiness_probe.allow_decode_only = new_config.scheduler_config.enable_pd_separation_fallback_to_hybrid

    def _load_mgmt_api_key(self) -> str:
        if not self._mgmt_api_key_config.enable_api_key:
            return ""
        return self._mgmt_api_key_config.load_api_key()

    def _verify_mgmt_api_key(self, request: Request) -> None:
        if not self._mgmt_api_key_config.enable_api_key:
            return
        api_key = request.headers.get(MGMT_API_KEY_HEADER)
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Missing {MGMT_API_KEY_HEADER} header",
            )
        if not secrets.compare_digest(api_key.encode("utf-8"), self._mgmt_api_key.encode("utf-8")):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid management API key")

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
                    self._start_render_health_observer()
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

        @self.management_app.get("/instances")
        async def list_instances(request: Request):
            self._verify_mgmt_api_key(request)
            return await build_instance_status_response(
                self._instance_manager,
                self._control_plane.circuit_breaker_manager,
            )

        @self.management_app.post("/instances/refresh", response_model=RequestResponse)
        @self.timeout_handler()
        async def refresh_instances(request: Request) -> RequestResponse:
            try:
                self._verify_mgmt_api_key(request)
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

        @self.management_app.post("/precision/alarm_cleared", response_model=RequestResponse)
        @self.timeout_handler()
        async def precision_alarm_cleared(request: Request) -> RequestResponse:
            try:
                self._verify_mgmt_api_key(request)
                result = await self._handle_precision_alarm_cleared(request)
                log_audit_event(
                    request=request,
                    event_type=PRECISION_ALARM_CLEARED,
                    resource_name=PRECISION_ALARM_CLEARED_URL,
                    event_result="success",
                )
                return result
            except Exception as e:
                log_audit_event(
                    request=request,
                    event_type=PRECISION_ALARM_CLEARED,
                    resource_name=PRECISION_ALARM_CLEARED_URL,
                    event_result=f"failed: {sanitize_error_message(str(e))[:100]}",
                )
                raise

        @self.management_app.get("/")
        async def root():
            return {
                "service": "Motor Coordinator Management Server",
                "version": "1.0.0",
                "description": "Management plane: liveness, startup, readiness, metrics, instance list/refresh",
                "endpoints": {
                    "GET /liveness": "liveness check",
                    "GET /startup": "startup probe",
                    "GET /readiness": "readiness check",
                    "GET /instances": "list registered instances with controller status and circuit-breaker overlay",
                    "POST /instances/refresh": "refresh instances",
                    "POST /precision/alarm_cleared": "clear precision alarm scheduler state",
                },
            }

    async def _handle_precision_alarm_cleared(self, request: Request) -> RequestResponse:
        try:
            raw_body = await request.body()
            if not raw_body:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Request body cannot be empty",
                )
            body = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid JSON format: {str(e)}",
            ) from e
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Request body must be a JSON object",
            )

        d_raw = body.get("d_instance_id")
        if d_raw is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing required field: d_instance_id",
            )
        try:
            d_id = int(d_raw)
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid d_instance_id: {e}",
            ) from e
        p_id: int | None = None
        p_raw = body.get("p_instance_id")
        if p_raw is not None:
            try:
                p_val = int(p_raw)
                p_id = p_val if p_val > 0 else None
            except (TypeError, ValueError) as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid p_instance_id: {e}",
                ) from e

        scheduler = getattr(self._control_plane, "scheduler", None)
        if scheduler is None:
            logger.warning(
                "Precision alarm state clear failed: control plane unavailable pd_group=(%s,%s)",
                p_id,
                d_id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Control plane unavailable",
            )
        dismissed = await scheduler.dismiss_precision_alarm_state(
            p_instance_id=p_id,
            d_instance_id=d_id,
        )
        if not dismissed:
            logger.warning(
                "Precision alarm state clear failed: control plane rejected pd_group=(%s,%s)",
                p_id,
                d_id,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Failed to clear precision alarm scheduler state",
            )

        return RequestResponse(
            request_id="precision_alarm_cleared",
            status="success",
            message="Precision alarm state cleared",
            data={"dismissed": True},
        )

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

        event_msg = await self._parse_instance_event(body)

        async with self._refresh_lock:
            try:
                await self._control_plane.apply_refresh(event_msg.event, event_msg.instances)
            except InstanceIdConflictError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc),
                ) from exc

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

    async def _parse_instance_event(self, body: Any) -> InsEventMsg:
        """Select InsEventMsg (Controller) vs ExternalInsEventMsg (coordinator-standalone).

        Standalone model-name discovery/verification uses blocking HTTP (requests, timeout=2s per endpoint).
        That I/O is offloaded so /liveness, /readiness, and /instances stay responsive.
        """
        try:
            is_standalone_request = _is_standalone_instance_refresh(body)
        except ValueError as mixed_protocol_error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(mixed_protocol_error),
            ) from mixed_protocol_error

        if not is_standalone_request:
            try:
                return InsEventMsg.model_validate(body)
            except Exception as controller_error:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid request format: {str(controller_error)}",
                ) from controller_error

        try:
            # Segment 1: schema-only. No engine I/O, so this stays on the event loop.
            external_msg = ExternalInsEventMsg.model_validate(body)
            aigw_model = self.coordinator_config.get_aigw_models() or {}
            # Segment 2: blocking /v1/models (requests, 2s/endpoint) runs in a worker thread.
            resolved_model_name, rejected_indexes = await asyncio.to_thread(
                self._resolve_external_model_name,
                external_msg,
            )
            if rejected_indexes:
                external_msg = external_msg.model_copy(
                    update={
                        "instances": [
                            instance
                            for index, instance in enumerate(external_msg.instances)
                            if index not in rejected_indexes
                        ]
                    }
                )
            return external_msg.to_internal(str(aigw_model.get("id", "")), resolved_model_name)
        except Exception as standalone_error:
            body_keys = list(body.keys()) if isinstance(body, dict) else "not a dict"
            logger.error(
                "Failed to parse coordinator-standalone instance refresh request: error=%s body_keys=%s",
                standalone_error,
                body_keys,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid request format: {str(standalone_error)}",
            ) from standalone_error

    def _resolve_external_model_name(self, event_msg: ExternalInsEventMsg) -> tuple[str, set[int]]:
        """Resolve/verify standalone model_name against every reachable instance DP.

        SET/ADD probe all endpoints concurrently with a bounded worker pool. An omitted
        model_name requires every reachable DP to advertise the same single model. A
        provided model_name must appear on every reachable DP. If any DP is unreachable,
        the whole instance is rejected while other fully reachable instances continue.
        DEL keeps serial first-reachable discovery and skips probing when model_name is
        already present.
        """
        expected = (event_msg.model_name or "").strip()
        verify_all = event_msg.event in (EventType.SET, EventType.ADD)
        if expected and not verify_all:
            return "", set()

        probe_targets = [
            (instance_index, endpoint.address.strip())
            for instance_index, instance in enumerate(event_msg.instances)
            for endpoint in instance.endpoints
        ]

        def query_model_ids(target: tuple[int, str]) -> tuple[int, str, list[str], str]:
            instance_index, address = target
            try:
                model_ids = NativeEngineApiClient.query_model_ids(
                    address,
                    self.coordinator_config.infer_tls_config,
                )
                return instance_index, address, model_ids, ""
            except (OSError, RuntimeError, ValueError) as exc:
                return instance_index, address, [], f"{address} ({exc})"

        if verify_all and probe_targets:
            max_workers = min(_EXTERNAL_MODEL_PROBE_WORKERS, len(probe_targets))
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="model-probe") as executor:
                probe_results = list(executor.map(query_model_ids, probe_targets))
        else:
            probe_results = list(map(query_model_ids, probe_targets))

        rejected_indexes: set[int] = set()
        if verify_all:
            rejection_reasons: dict[int, list[str]] = {}
            for instance_index, _address, _model_ids, probe_error in probe_results:
                if not probe_error:
                    continue
                rejected_indexes.add(instance_index)
                rejection_reasons.setdefault(instance_index, []).append(probe_error)
            for instance_index, reasons in rejection_reasons.items():
                instance = event_msg.instances[instance_index]
                logger.warning(
                    "Rejecting external instance because at least one DP is unreachable: "
                    "instance_id=%s role=%s errors=%s",
                    instance.id,
                    instance.role.value,
                    reasons,
                )
            if rejected_indexes and len(rejected_indexes) == len(event_msg.instances):
                raise ValueError("no fully reachable instances remain after probing every DP")

        last_error = ""
        resolved = ""
        for instance_index, address, model_ids, probe_error in probe_results:
            if probe_error:
                last_error = probe_error
                continue
            if instance_index in rejected_indexes:
                continue

            if expected:
                if not any(model_id.casefold() == expected.casefold() for model_id in model_ids):
                    raise ValueError(f"{address}/v1/models serves {model_ids}, expected {expected!r}")
                continue

            if not model_ids:
                raise ValueError(f"{address}/v1/models returned no models; provide model_name explicitly")
            if len(model_ids) != 1:
                raise ValueError(
                    f"{address}/v1/models serves multiple models {model_ids}; provide model_name explicitly"
                )
            found = model_ids[0]
            if not resolved:
                resolved = found
                if not verify_all:
                    return resolved, set()
                continue
            if found.casefold() != resolved.casefold():
                raise ValueError(
                    f"{address}/v1/models serves {found!r}, "
                    f"which does not match previously probed {resolved!r}; "
                    "all instances and DPs must share the same model_name"
                )

        if expected:
            return "", rejected_indexes
        if resolved:
            return resolved, rejected_indexes
        hint = f"; last error: {last_error}" if last_error else ""
        raise ValueError(f"no reachable native engine /v1/models{hint}; provide model_name explicitly")
