# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import re
import threading
import asyncio
from typing import Any

import prometheus_client
import uvicorn
from fastapi import FastAPI, Response
from prometheus_client import CollectorRegistry, multiprocess, make_asgi_app
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.routing import Mount

from motor.common.resources.dispatch import infer_vllm_dispatch_profile_from_config
from motor.common.http.cert_util import CertUtil
from motor.common.logger import get_logger
from motor.common.utils.net import format_address
from motor.engine_server.core.config import IConfig
from motor.engine_server.core.endpoint import Endpoint
from motor.engine_server.core.health_collector import HealthCollector
from motor.engine_server.core.snapshot_monitor import SnapshotMonitor
from motor.engine_server.core.sim_inference import SimInference
from motor.engine_server.constants import constants
from motor.engine_server.constants.constants import (
    METRICS_INTERFACE,
    STATUS_INTERFACE,
    STATUS_KEY,
    INIT_STATUS,
    NORMAL_STATUS,
    ABNORMAL_STATUS,
)
from motor.common.utils.snapshot_utils import is_restored_from_host_side_snapshot


logger = get_logger(__name__)


class PrometheusResponse(Response):
    media_type = prometheus_client.CONTENT_TYPE_LATEST


def attach_metrics_router(app: FastAPI):
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)

    Instrumentator(
        excluded_handlers=[
            METRICS_INTERFACE,
            STATUS_INTERFACE,
        ],
        registry=registry,
    ).add().instrument(app).expose(app, response_class=PrometheusResponse)

    metrics_route = Mount(METRICS_INTERFACE, make_asgi_app(registry=registry))

    metrics_route.path_regex = re.compile("^/metrics(?P<path>.*)$")
    app.routes.append(metrics_route)
    logger.info("Created Endpoint metrics route: /metrics successfully")


class MgmtEndpoint(Endpoint):
    def __init__(self, config: IConfig):
        endpoint_config = config.get_endpoint_config()
        self.snapshot_enabled = endpoint_config.snapshot_metadata is not None
        self.host = endpoint_config.host
        self.mgmt_port = endpoint_config.mgmt_port
        self.mgmt_tls_config = endpoint_config.deploy_config.mgmt_tls_config
        self.app = FastAPI(title="EngineServer MgmtEndpoint")
        attach_metrics_router(self.app)

        args = config.get_args()
        infer_tls_config = endpoint_config.deploy_config.infer_tls_config
        health_check_config = endpoint_config.deploy_config.health_check_config or {}
        # Headless follower nodes have no API server, disable virtual inference
        if getattr(args, 'headless', False) and hasattr(health_check_config, 'enable_virtual_inference'):
            health_check_config.enable_virtual_inference = False
        self.sim_inference = SimInference(
            args,
            infer_tls_config,
            health_check_config,
            endpoint_config.role,
            dispatch_profile=infer_vllm_dispatch_profile_from_config(config),
        )

        self._stop_event = threading.Event()
        self._server: uvicorn.Server | None = None
        self._server_thread = threading.Thread(target=self._run_server, name="endpoint_server_thread", daemon=True)
        self.health_collector = HealthCollector(config)
        self.attach_status_router()
        self._virtual_inference_started = False
        self._lock = asyncio.Lock()
        self._server_core: Any | None = None

    def set_server_core(self, server_core: Any) -> None:
        self._server_core = server_core

    def run(self):
        if self._server_thread and not self._server_thread.is_alive():
            self._server_thread.start()
            logger.info("Endpoint server started: http://%s", format_address(self.host, self.mgmt_port))

    def run_virtual_inference(self):
        # start health check
        self.sim_inference.set_status(constants.NORMAL_STATUS)
        self.sim_inference.start_health_check()

    def shutdown(self):
        if self._server:
            self._server.should_exit = True
            logger.info("Endpoint: Uvicorn server exit triggered")
        self._stop_event.set()
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)
            log_msg = "exited" if not self._server_thread.is_alive() else "timeout"
            logger.info("Endpoint server thread %s", log_msg)

        self.sim_inference.stop_health_check()

        logger.info("Endpoint server stopped completely")

    def attach_status_router(self):
        @self.app.get(STATUS_INTERFACE)
        async def get_status(response: Response) -> dict[str, Any]:
            response.status_code = 200
            try:
                is_healthy = await self.health_collector.is_healthy()
                if is_healthy:
                    if self.snapshot_enabled:
                        if not is_restored_from_host_side_snapshot() and not SnapshotMonitor().is_suspend_done:
                            return {STATUS_KEY: INIT_STATUS}
                        elif is_restored_from_host_side_snapshot() and not SnapshotMonitor().is_resume_done:
                            return {STATUS_KEY: INIT_STATUS}
                        elif (
                            not is_restored_from_host_side_snapshot()
                            and SnapshotMonitor().is_suspend_done
                            and not SnapshotMonitor().is_unlock_done
                        ):
                            return {STATUS_KEY: NORMAL_STATUS}

                    async with self._lock:
                        if not self._virtual_inference_started:
                            self.run_virtual_inference()
                            self._virtual_inference_started = True
                    # check sim_inference status
                    if self.sim_inference and self.sim_inference.is_abnormal():
                        logger.warning("SimInference is in abnormal status, returning ABNORMAL_STATUS")
                        return {STATUS_KEY: ABNORMAL_STATUS}
                    return {STATUS_KEY: NORMAL_STATUS}
                return {STATUS_KEY: ABNORMAL_STATUS}
            except Exception:
                return {STATUS_KEY: INIT_STATUS}

    def _run_server(self):
        config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=self.mgmt_port,
            log_level="warning",
            workers=1,
        )

        config.load()
        if self.mgmt_tls_config and self.mgmt_tls_config.enable_tls:
            ssl_context = CertUtil.create_ssl_context(self.mgmt_tls_config)
            if ssl_context:
                config.ssl = ssl_context
            else:
                raise RuntimeError("Failed to create ssl context")
            logger.info("MgmtEndpoint server started: https://%s", format_address(self.host, self.mgmt_port))
        else:
            logger.info("MgmtEndpoint server started: http://%s", format_address(self.host, self.mgmt_port))

        self._server = uvicorn.Server(config)
        if not self._stop_event.is_set():
            self._server.run()
