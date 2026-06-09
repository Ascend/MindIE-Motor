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
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import Response
import uvicorn

from motor.common.http.cert_util import CertUtil

from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.core.heartbeat_manager import HeartbeatManager
from motor.common.logger import get_logger
from motor.common.resources.http_msg_spec import StartCmdMsg
from motor.node_manager.core.engine_manager import EngineManager
from motor.node_manager.core.daemon import Daemon
from motor.common.resources.instance import PDRole

logger = get_logger(__name__)

# Global event to signal when NodeManagerAPI server is ready
_api_ready_event = threading.Event()


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Lifespan context manager for FastAPI app"""
    # Startup: signal that the server is ready
    _api_ready_event.set()
    logger.info("NodeManagerAPI server is ready")
    yield
    # Shutdown: clear the ready event
    _api_ready_event.clear()


app = FastAPI(lifespan=lifespan)

MAX_CONCURRENT_THREADS = 10
thread_semaphore = asyncio.Semaphore(MAX_CONCURRENT_THREADS)


@app.post("/node-manager/start")
async def start_instance(request: Request):
    """post instance and role info"""
    try:
        payload = await request.json()
        start_msg = StartCmdMsg(**payload)
        engine_manager = EngineManager()

        async with thread_semaphore:
            try:
                parsed_ok = await asyncio.to_thread(engine_manager.parse_start_cmd, start_msg)
            except Exception as inner_err:
                logger.error("Failed to parse start command: %s", inner_err)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid start command payload"
                ) from inner_err

        if not parsed_ok:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Start command validation failed"
            )

        try:
            await asyncio.to_thread(
                Daemon().pull_engine,
                PDRole(start_msg.role),
                start_msg.endpoints,
                start_msg.instance_id,
                start_msg.master_dp_ip,
                engine_manager.d2d_peer_ips,
                start_msg.node_rank,
            )
        except Exception as pull_err:
            logger.error("Failed to pull engine: %s", pull_err)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to start engine server"
            ) from pull_err

        HeartbeatManager().update_endpoint(start_msg)
        HeartbeatManager().start()
        engine_manager.start()
        return {}

    except HTTPException as http_err:
        raise http_err
    except Exception as err:
        # Catch other unexpected exceptions to avoid returning unfriendly internal errors
        logger.error("Unexpected error: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An internal server error occurred"
        ) from err


@app.post("/node-manager/stop")
async def stop_instance(request: Request):
    """
    Stop all engine processes by invoking Daemon.exit_daemon().
    """
    try:
        await asyncio.to_thread(Daemon().stop)
        content = {"message": "All engine processes stopped successfully."}
        return Response(status_code=status.HTTP_200_OK, content=json.dumps(content))
    except Exception as err:
        logger.error("Failed to stop engines via daemon: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to stop engine processes"
        ) from err


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
        engine_mgmt_addrs = hm.get_engine_mgmt_addrs()
        content = {
            "status": "ok",
            "message": "Endpoints set to PAUSED",
            "engine_mgmt_addrs": engine_mgmt_addrs,
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


@app.get("/node-manager/status")
async def get_instance_status():
    """
    Check if all endpoints managed by this node manager are in normal status.
    Returns True if all endpoints are normal, False if any endpoint is abnormal.
    """
    try:
        is_normal = await asyncio.to_thread(HeartbeatManager().check_all_endpoints_normal)
        return {"status": is_normal}
    except Exception as err:
        logger.error("Failed to check endpoints status: %s", err)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to check endpoints status"
        ) from err


class NodeManagerAPI:
    def __init__(self, config: NodeManagerConfig = None):
        self._config = config
        # Get host and port from config
        if self._config and self._config.api_config.pod_ip:
            self.host = self._config.api_config.pod_ip
        else:
            self.host = "0.0.0.0"  # Default host

        if self._config:
            self.port = self._config.api_config.node_manager_port
        else:
            self.port = 8080  # Default port
        self.server = None
        self.serve_task = None
        self._thread = None

        # Reset the ready event before starting
        _api_ready_event.clear()

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
        return _api_ready_event.wait(timeout=timeout)

    async def stop(self):
        self.stop_sync()

    def stop_sync(self):
        if self.server:
            self.server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                logger.warning("API server thread did not stop within timeout")

    def _serve_in_thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        config = uvicorn.Config(app, host=self.host, port=self.port, loop="asyncio")
        config.load()
        if self._config.mgmt_tls_config.enable_tls:
            context = CertUtil.create_ssl_context(self._config.mgmt_tls_config)
            if not context:
                raise RuntimeError("Failed to create SSL context")
            config.ssl = context

            logger.info("Node Manager server started: https://%s:%s", self.host, self.port)
        else:
            logger.info("Node Manager server stated: http://%s:%s", self.host, self.port)

        self.server = uvicorn.Server(config)
        try:
            loop.run_until_complete(self.server.serve())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as e:
                logger.error("Failed to shutdown server: %s", e)
            loop.close()
