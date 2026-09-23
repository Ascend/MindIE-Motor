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
import json
import os
import signal
import socket
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import Response
import uvicorn

from motor.common.http.cert_util import CertUtil
from motor.common.utils.net import detect_family, format_address
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.core.heartbeat_manager import HeartbeatManager
from motor.common.logger import ApiAccessFilter, get_logger
from motor.common.resources.http_msg_spec import StartCmdMsg
from motor.node_manager.core.register_manager import RegisterManager
from motor.node_manager.core.daemon import Daemon, EngineRestartInProgressError, EngineRestartParamError
from motor.node_manager.core.api_ready_event import clear_api_ready, mark_api_ready, wait_until_api_ready
from motor.common.resources.instance import PDRole
from motor.common.utils.snapshot_utils import is_restored_from_host_side_snapshot

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Lifespan context manager for FastAPI app"""
    # Startup: signal that the server is ready
    mark_api_ready()
    logger.info("NodeManagerAPI server is ready")
    yield
    # Shutdown: clear the ready event
    clear_api_ready()


app = FastAPI(lifespan=lifespan)

MAX_CONCURRENT_THREADS = 10
thread_semaphore = asyncio.Semaphore(MAX_CONCURRENT_THREADS)


def _require_ft_proxy(action: str) -> tuple[Any, NodeManagerConfig]:
    """Return the local FT manager when the proxy feature is enabled."""
    try:
        daemon = Daemon()
        config = daemon.config
        if not config.fault_tolerance_config.enable_dp_scale_down_proxy:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="DP scale-down proxy is disabled")
        return daemon.engine_ft_manager, config
    except HTTPException:
        raise
    except Exception as err:
        logger.error("Failed to prepare engine FT %s request: %s", action, err)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Engine FT %s failed" % action,
        ) from err


async def _parse_ft_payload(request: Request, action: str) -> dict:
    """Parse one FT request and reject malformed client input with HTTP 400."""
    try:
        payload = await request.json()
    except Exception as err:
        logger.error("Failed to parse engine FT %s request: %s", action, err)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body for engine FT %s" % action,
        ) from err
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object")
    return payload


def _require_int_list(payload: dict, field_name: str, *, allow_empty: bool = False) -> list[int]:
    value = payload.get(field_name)
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        detail = "'%s' must be %sa list of integer DP ranks" % (
            field_name,
            "an empty or " if allow_empty else "a non-empty ",
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    return value


async def _ft_request_context(request: Request, action: str) -> tuple[dict, Any, NodeManagerConfig]:
    """Parse and gate one NodeManager FT proxy request."""
    payload = await _parse_ft_payload(request, action)
    manager, config = _require_ft_proxy(action)
    return payload, manager, config


async def _run_ft_operation(action: str, operation: Callable[..., Any], *args: Any) -> Any:
    """Run one blocking FT operation and preserve the public 502 contract."""
    try:
        return await asyncio.to_thread(operation, *args)
    except Exception as err:
        logger.error("Failed to %s engine FT request: %s", action, err)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Engine FT %s failed" % action,
        ) from err


@app.post("/node-manager/fault-tolerance/status")
async def fault_tolerance_status(request: Request):
    """Proxy local engine FT status; engine HTTP details stay in NodeManager."""
    payload, manager, config = await _ft_request_context(request, "status query")
    return await _run_ft_operation(
        "status query",
        manager.query,
        _require_int_list(payload, "endpoint_ids"),
        config.fault_tolerance_config.poll_timeout_sec,
    )


@app.post("/node-manager/fault-tolerance/apply")
async def fault_tolerance_apply(request: Request):
    """Apply FT instruction after NodeManager adds its configured DP store port."""
    payload, manager, config = await _ft_request_context(request, "apply")
    endpoint_ids = _require_int_list(payload, "endpoint_ids")
    instruction = payload.get("instruction")
    if instruction not in {"retry", "scale_down"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported FT instruction")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'params' must be a JSON object")
    await _run_ft_operation(
        "apply",
        manager.apply,
        endpoint_ids,
        instruction,
        params,
        payload.get("request_id", ""),
        config.fault_tolerance_config.poll_timeout_sec,
        config.basic_config.parallel_config.dp_master_port,
    )
    return {"status": "accepted"}


@app.post("/node-manager/fault-tolerance/guard")
async def fault_tolerance_guard(request: Request):
    """Freeze local suicide before Controller starts the distributed transaction."""
    manager, _ = _require_ft_proxy("guard")
    payload = await _parse_ft_payload(request, "guard")
    await _run_ft_operation(
        "guard",
        manager.guard,
        payload.get("request_id"),
        payload.get("lease_sec"),
    )
    return {"status": "guarded"}


@app.post("/node-manager/fault-tolerance/finalize")
async def fault_tolerance_finalize(request: Request):
    """Commit local retired ranks, or abort the local scale-down guard."""
    payload, manager, _ = await _ft_request_context(request, "finalize")
    commit = payload.get("commit") is True
    retired_endpoint_ids = _require_int_list(payload, "retired_endpoint_ids", allow_empty=True)
    dp_master_rank = payload.get("dp_master_rank")
    if commit and (not isinstance(dp_master_rank, int) or isinstance(dp_master_rank, bool) or dp_master_rank < 0):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'dp_master_rank' must be a non-negative integer when committing",
        )
    await _run_ft_operation(
        "finalize",
        manager.finalize,
        payload.get("request_id"),
        retired_endpoint_ids,
        commit,
        dp_master_rank,
    )
    return {"status": "committed" if commit else "aborted"}


@app.post("/node-manager/start")
async def start_instance(request: Request):
    """post instance and role info"""
    try:
        payload = await request.json()
        start_msg = StartCmdMsg(**payload)
        register_manager = RegisterManager()

        async with thread_semaphore:
            try:
                parsed_ok = await asyncio.to_thread(register_manager.parse_start_cmd, start_msg)
            except Exception as inner_err:
                logger.error("Failed to parse start command: %s", inner_err)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid start command payload"
                ) from inner_err

        if not parsed_ok:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Start command validation failed"
            )

        # If restore from snapshot
        # Use start_msg.master_dp_ip to update snapshot metadata for engine resume
        # Update endpoint and set started after restore flag
        if is_restored_from_host_side_snapshot():
            await asyncio.to_thread(RegisterManager().engine_resume_prepare, start_msg)
            # Registration order may assign a different endpoint.id block than at
            # cold start; rekey supervisor runtimes before HB probes with new ids.
            await asyncio.to_thread(Daemon().rebind_engine_endpoints_after_restore, start_msg.endpoints)
            HeartbeatManager().update_endpoint(start_msg)
            HeartbeatManager().set_started_after_restore(True)
            return {}

        # If snapshot mode is not disabled, prepare snapshot runtime directories and metadata file for engine suspend
        await asyncio.to_thread(RegisterManager().engine_suspend_prepare)

        daemon = Daemon()
        try:
            await asyncio.to_thread(
                daemon.pull_engine,
                PDRole(start_msg.role),
                start_msg.endpoints,
                start_msg.instance_id,
                start_msg.master_dp_ip,
                register_manager.d2d_peer_ips,
                start_msg.node_rank,
            )
        except Exception as pull_err:
            logger.error("Failed to pull engine: %s", pull_err)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to start native engine"
            ) from pull_err

        # Start KV store service (if backend is configured)
        try:
            await asyncio.to_thread(daemon.pull_kv_store)
        except Exception as ls_err:
            logger.error("Failed to start KV store service, cleaning up engines: %s", ls_err)
            await asyncio.to_thread(daemon.stop)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to start KV store service"
            ) from ls_err

        HeartbeatManager().update_endpoint(start_msg)
        # Gate the engine-status probing on the Daemon's engine-ready handoff
        # (mgmt ports up) — the HeartbeatManager has no readiness logic.
        HeartbeatManager().start(engine_ready_event=daemon.engine_ready_event)
        return {}

    except HTTPException as http_err:
        raise http_err
    except Exception as err:
        # Catch other unexpected exceptions to avoid returning unfriendly internal errors
        logger.error("Unexpected error: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An internal server error occurred"
        ) from err


def _self_terminate() -> None:
    """SIGTERM this process: the Application treats it as a graceful shutdown
    and exits (-1) — k8s then restarts the pod.
    """
    os.kill(os.getpid(), signal.SIGTERM)


@app.post("/node-manager/stop")
async def stop_instance(request: Request):
    """Stop all engine processes, then terminate this NodeManager.

    The Controller dispatches ``/node-manager/stop`` as the "suicide"
    instruction: engine stop followed by process exit (-1), which k8s turns
    into a pod restart. Used for instance teardown and for partial-loss
    coordination (the surviving NodeManagers of a cross-machine instance exit
    so the whole instance restarts together).
    """
    try:
        await asyncio.to_thread(Daemon().stop)
        # Delayed so the 200 response is sent before the process exits.
        threading.Timer(0.5, _self_terminate).start()
        content = {"message": "All engine processes stopped successfully."}
        return Response(status_code=status.HTTP_200_OK, content=json.dumps(content))
    except Exception as err:
        logger.error("Failed to stop engines via daemon: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to stop engine processes"
        ) from err


@app.post("/node-manager/engine-restart")
async def engine_restart(request: Request):
    """Controller-driven engine relaunch without container restart.

    Body: ``{"action": "restart"|"abort", "instance_id": int?}``

    - ``restart``: kill and re-pull all engine subprocesses in place
      (``Daemon.restart_engine`` — resolves the launch params, freezes
      suicide, suspends/resumes the EngineFtManager; KV store untouched).
      Returns 200 once the processes were spawned (model loading continues
      asynchronously — completion is polled by the Controller via
      ``/node-manager/status``).
    - ``abort``: unfreeze the suicide counter — the heartbeat mechanism
      resumes counting ABNORMAL reports and the pod restarts via k8s
      (fallback path when engine relaunch failed).

    Forcing this NodeManager to exit (partial-loss coordination) is not an
    engine-restart concern — the Controller uses ``/node-manager/stop``.
    """
    try:
        payload = await request.json()
    except Exception as err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body") from err
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object")

    action = payload.get("action")
    if action not in ("restart", "abort"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'action' must be 'restart' or 'abort'")

    daemon = Daemon()
    if action == "abort":
        daemon.unfreeze_suicide()
        logger.info("Engine restart aborted: suicide arbitration unfrozen (container restart fallback)")
        return {"message": "abort accepted"}

    # Only reject during an actual snapshot restore: is_started_after_restore
    # is False in the normal (non-snapshot) deployment, so it alone must
    # not gate the relaunch.
    if is_restored_from_host_side_snapshot() and not HeartbeatManager().is_started_after_restore():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Snapshot restore in progress, engine restart not supported",
        )

    instance_id = payload.get("instance_id")
    try:
        await asyncio.to_thread(daemon.restart_engine, instance_id)
    except EngineRestartInProgressError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Engine restart already in progress")
    except EngineRestartParamError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No engine start recorded, nothing to restart"
        )
    except Exception as err:
        logger.error("Failed to restart engines: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Engine restart failed: {err}"
        ) from err

    logger.info("Engines restarted in place for instance %s", instance_id)
    return {"message": "engine restart accepted"}


@app.post("/node-manager/pause")
async def pause_instance(request: Request):
    """
    PreStop hook: set all endpoints to PAUSED status.
    Pod readiness probe returns false; liveness probe remains true.
    Controller will receive PAUSED status via heartbeat and trigger
    the pause flow to Coordinator.
    """
    try:
        await asyncio.to_thread(HeartbeatManager().pause_all_endpoints)
        hm = HeartbeatManager()
        engine_metrics_targets = hm.get_engine_metrics_targets()
        content = {
            "status": "ok",
            "message": "Endpoints set to PAUSED",
            "engine_metrics_targets": engine_metrics_targets,
        }
        return Response(status_code=status.HTTP_200_OK, content=json.dumps(content))
    except Exception as err:
        logger.error("Failed to set endpoints to PAUSED: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to set endpoints to PAUSED"
        ) from err


@app.post("/node-manager/resume")
async def resume_instance(request: Request):
    """
    Resume instance from PAUSED back to NORMAL status.
    Used when PreStop is cancelled (e.g. rollout rollback).
    """
    try:
        await asyncio.to_thread(HeartbeatManager().resume_all_endpoints)
        content = {"status": "ok", "message": "Endpoints resumed to NORMAL"}
        return Response(status_code=status.HTTP_200_OK, content=json.dumps(content))
    except Exception as err:
        logger.error("Failed to resume endpoints: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to resume endpoints"
        ) from err


async def _check_node_manager_ready() -> bool:
    is_normal = await asyncio.to_thread(HeartbeatManager().check_all_endpoints_normal)
    if is_restored_from_host_side_snapshot():
        is_normal = is_normal and HeartbeatManager().is_started_after_restore()
    return is_normal


@app.get("/node-manager/status")
async def get_instance_status(relaxed: bool = False):
    """
    Check if all endpoints managed by this node manager are in normal status.

    ``relaxed=true`` (used by the engine-relaunch flow) returns True when no
    endpoint is ABNORMAL — a freshly relaunched engine reports INITIAL while
    loading its model, which counts as recovering, not failed.
    """
    try:
        if relaxed:
            is_normal = await asyncio.to_thread(HeartbeatManager().check_all_endpoints_recovering)
        else:
            is_normal = await _check_node_manager_ready()
        return {"status": is_normal}
    except Exception as err:
        logger.error("Failed to check endpoints status: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to check endpoints status"
        ) from err


@app.get("/readiness")
async def readiness():
    """
    Readiness probe - returns 200 when all endpoints are healthy.
    Otherwise, returns 503.
    """
    try:
        is_ready = await _check_node_manager_ready()
    except Exception as err:
        logger.error("Failed to check node manager readiness: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to check node manager readiness"
        ) from err

    msg = "message"
    reason = "reason"
    if not is_ready:
        if is_restored_from_host_side_snapshot() and not HeartbeatManager().is_started_after_restore():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={msg: "Node manager is not ready", reason: "Not started after container snapshot restore"},
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={msg: "Node manager is not ready", reason: "Endpoints not healthy"},
        )
    return {msg: "Node manager is ready"}


class NodeManagerAPI:
    def __init__(self, config: NodeManagerConfig = None):
        self._config = config
        # Get host and port from config
        if self._config and self._config.api_config.pod_ip:
            self.host = self._config.api_config.pod_ip
        else:
            # IPv6 single-stack: when POD_IP env is an IPv6 literal, default to
            # the v6 wildcard (::) instead of the v4 wildcard (0.0.0.0).
            self.host = "::" if detect_family(os.getenv("POD_IP", "")) == socket.AF_INET6 else "0.0.0.0"

        if self._config:
            self.port = self._config.api_config.node_manager_port
        else:
            self.port = 8080  # Default port
        self.server = None
        self.serve_task = None
        self._thread = None

        # Reset the ready event before starting
        clear_api_ready()

        self._thread = threading.Thread(target=self._serve_in_thread, daemon=True, name="nm_api_server")
        self._thread.start()

    @staticmethod
    def wait_until_ready(timeout: float = None) -> bool:
        """
        Wait until the NodeManagerAPI server is ready.

        Args:
            timeout: Maximum time to wait in seconds. None means wait indefinitely.

        Returns:
            True if the server is ready, False if timeout occurred.
        """
        return wait_until_api_ready(timeout=timeout)

    def stop(self):
        # Synchronous on purpose: Application.stop_all_modules calls
        # module.stop() without awaiting — an async stop would produce a
        # coroutine-never-awaited warning on shutdown. The teardown itself
        # is sync (stop_sync), so the async wrapper was redundant.
        self.stop_sync()

    def stop_sync(self):
        if self.server:
            self.server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                logger.warning("API server thread did not stop within timeout")

    @staticmethod
    def _suppress_probe_access_logs() -> None:
        """Suppress noisy uvicorn access logs from K8s readiness/liveness probes."""
        probe_filter = ApiAccessFilter(
            {
                "/readiness": logging.ERROR,
            }
        )
        logging.getLogger("uvicorn.access").addFilter(probe_filter)

    def _serve_in_thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._suppress_probe_access_logs()
        config = uvicorn.Config(app, host=self.host, port=self.port, loop="asyncio")
        config.load()
        if self._config.mgmt_tls_config.enable_tls:
            context = CertUtil.create_ssl_context(self._config.mgmt_tls_config)
            if not context:
                raise RuntimeError("Failed to create SSL context")
            config.ssl = context

            logger.info("Node Manager server started: https://%s", format_address(self.host, self.port))
        else:
            logger.info("Node Manager server started: http://%s", format_address(self.host, self.port))

        self.server = uvicorn.Server(config)
        try:
            loop.run_until_complete(self.server.serve())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as e:
                logger.error("Failed to shutdown server: %s", e)
            loop.close()
