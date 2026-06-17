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
Observability server: runs in a dedicated Obs process (spawned by CoordinatorDaemon via
ObsProcessManager).  Provides metrics, request counts, completion status, and other
observability endpoints.  Designed to host OpenTelemetry and other observability
tooling in the future.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from motor.common.logger import get_logger
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_server.base_server import BaseCoordinatorServer
from motor.coordinator.api_server.app_builder import AppBuilder
from motor.coordinator.metrics.metrics_collector import MetricsCollector
from motor.coordinator.scheduler.runtime import SchedulerConnectionManager
from motor.common.resources.instance import Instance

logger = get_logger(__name__)


class _SchedulerInstanceProvider:
    """Expose the live instance view to MetricsCollector in the Obs process.

    The Obs process has no /instances/refresh handler to feed a local
    InstanceManager, so it reads instances from the scheduler client, which
    subscribes to the scheduler's instance-change pub and keeps the cache fresh.
    """

    def __init__(self, connection: SchedulerConnectionManager):
        self._connection = connection

    async def get_all_instances(self) -> tuple[dict[int, Instance], dict[int, Instance]]:
        client = self._connection.get_client()
        if client is None:
            return {}, {}
        available = await client.get_available_instances(None)
        return available, {}


class ObservabilityServer(BaseCoordinatorServer):
    """
    Observability plane: runs in the Obs process only.
    Provides metrics and observability endpoints.
    """

    def __init__(
        self,
        config: CoordinatorConfig | None = None,
        daemon_pid: int | None = None,
    ):
        if config is None:
            config = CoordinatorConfig()
        super().__init__(config)
        self._daemon_pid = daemon_pid

        # Connect to the scheduler the same way Inference/Mgmt servers do, so the
        # client subscribes to the scheduler's instance-change pub and keeps a live view.
        self._scheduler_connection = SchedulerConnectionManager.from_config(config)
        self._instance_provider = _SchedulerInstanceProvider(self._scheduler_connection)
        self._app_builder = AppBuilder(config)
        self.observability_app = self._app_builder.create_observability_app(lifespan=self._lifespan)
        self._register_routes()

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        logger.info("Observability server is starting...")
        await self._scheduler_connection.connect()
        try:
            MetricsCollector().set_event_loop(asyncio.get_running_loop())
            MetricsCollector().set_scheduler_provider(lambda: self._instance_provider)
            MetricsCollector().start()
        except Exception as e:
            logger.warning("Ignored error setting up metrics collector: %s", e)
        try:
            yield
        except asyncio.CancelledError:
            logger.info("Observability server startup was cancelled")
        except Exception as e:
            logger.error("Observability server startup failed: %s", e)
            raise
        finally:
            logger.info("Observability server is shutting down...")
            try:
                MetricsCollector().stop()
            except Exception as e:
                logger.warning("Ignored error stopping metrics collector: %s", e)
            await self._scheduler_connection.disconnect()

    def _register_routes(self):
        @self.observability_app.get("/metrics")
        async def get_metrics(request: Request):
            metrics_type = request.query_params.get("type", "full")
            role = request.query_params.get("role", None)
            metrics_format = request.query_params.get("format", "prometheus")
            try:
                result = MetricsCollector().get_metrics(
                    metrics_type=metrics_type,
                    role=role,
                    metrics_format=metrics_format,
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if isinstance(result, str):
                return PlainTextResponse(content=result)
            return JSONResponse(content=result)

        @self.observability_app.get("/instance/metrics")
        async def get_instance_metrics():
            return PlainTextResponse(
                content=("# /instance/metrics is deprecated. Use GET /metrics?type=instance instead.\n"),
                status_code=410,
            )

        @self.observability_app.get("/health")
        async def health():
            return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

    async def run(self) -> None:
        config_kwargs = self.create_base_uvicorn_config(
            self.observability_app,
            self.coordinator_config.api_config.coordinator_api_host,
            self.coordinator_config.api_config.coordinator_obs_port,
        )
        self.apply_timeout_to_config(config_kwargs)
        uv_config = uvicorn.Config(**config_kwargs)
        uv_config.load()
        server = uvicorn.Server(uv_config)
        await server.serve()

    def _apply_config_changes(self, new_config: CoordinatorConfig) -> None:
        pass
