# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from collections.abc import Callable
from typing import Any
from functools import wraps

import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse

from motor.common.resources import RegisterMsg, ReregisterMsg, HeartbeatMsg, TerminateInstanceMsg
from motor.common.standby.standby_manager import StandbyManager, StandbyRole
from motor.common.http.cert_util import CertUtil
from motor.common.logger import get_logger, ApiAccessFilter
from motor.common.http.http_response import format_success_response, raise_internal_error
from motor.common.utils.net import format_address
from motor.common.alarm.record import Record
from motor.common.alarm.precision_issue_alarm import PRECISION_ISSUE_ALARM_ID
from motor.config.controller import ControllerConfig
from motor.controller.observability.observability import Observability
from motor.controller.core.instance_assembler import InstanceAssembler
from motor.controller.core.instance_manager import InstanceManager
from motor.controller.fault_tolerance.fault_manager import FaultManager
from motor.controller.fault_tolerance.fault_types import FaultInfo
from motor.controller.core.recovery_service import terminate_instance_for_recovery
from motor.controller.observability.inventory.inventory_collector import InventoryCollector

logger = get_logger(__name__)


def observability_enabled_required(func: Callable):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        if not self.enable_observability_api:
            return raise_internal_error(message="Observability is not enabled.")
        return await func(self, *args, **kwargs)

    return wrapper


class ControllerAPI:
    def __init__(
        self,
        config: ControllerConfig | None = None,
        modules: dict[str, Any] | None = None,
        host: str = None,
        port: int = None,
    ):
        if config is None:
            config = ControllerConfig()

        self.observability = Observability()
        # Extract required config fields for TLS and standby mode
        self.enable_master_standby = config.standby_config.enable_master_standby
        self.mgmt_tls_config = config.mgmt_tls_config

        self.modules = modules
        self.config_lock = threading.RLock()
        self.host = host if host is not None else config.api_config.controller_api_host
        self.port = port if port is not None else config.api_config.controller_api_port
        self.server = None
        self.loop = None
        self.app = self._create_app()
        self.api_server_thread = None

        # Observability API configuration
        self.enable_observability_api = config.observability_config.observability_enable
        self.enable_fault_tolerance = config.fault_tolerance_config.enable_fault_tolerance
        self.observability_api_host = host if host is not None else config.api_config.controller_api_host
        self.observability_api_port = config.api_config.observability_api_port
        self.observability_tls_config = config.observability_tls_config
        self.observability_server = None
        self.observability_loop = None
        self.observability_app = self._create_observability_app()
        self.observability_api_server_thread = None

        # Independent of coordinator sampling; only gates terminate-on-precision-alarm.
        self.precision_auto_recovery_enabled = getattr(config, "precision_auto_recovery_enabled", False)

        logger.info("ControllerAPI initialized.")

    def start(self) -> None:
        # Create API server thread
        self.api_server_thread = threading.Thread(target=self._run_api_server, daemon=True, name="APIServer")
        self.api_server_thread.start()

        # Observability API startup logic merging
        self.observability_api_server_thread = threading.Thread(
            target=self._run_observability_api_server, daemon=True, name="ObservabilityAPIServer"
        )
        self.observability_api_server_thread.start()
        logger.info(
            "Observability API started successfully. Host: %s, Port: %d",
            self.observability_api_host,
            self.observability_api_port,
        )

        logger.info("ControllerAPI started.")

    def is_alive(self) -> bool:
        """Check if the API server thread is alive"""
        # Controller API server status
        controller_alive = self.api_server_thread is not None and self.api_server_thread.is_alive()

        # check observability API server status
        observability_alive = (
            self.observability_api_server_thread is not None and self.observability_api_server_thread.is_alive()
        )
        return controller_alive and observability_alive

    def stop(self) -> None:
        if self.server and self.loop:
            try:
                future = asyncio.run_coroutine_threadsafe(self.server.shutdown(), self.loop)
                future.result(timeout=3)
                logger.info("API server stopped gracefully")
            except Exception as e:
                logger.error("Error stopping server: %s", e)
                if self.loop and not self.loop.is_closed():
                    self.loop.call_soon_threadsafe(self.loop.stop)

        # stop observability API server
        if self.observability_server and self.observability_loop:
            try:
                future = asyncio.run_coroutine_threadsafe(self.observability_server.shutdown(), self.observability_loop)
                future.result(timeout=3)
                logger.info("Observability API server stopped gracefully")
            except Exception as e:
                logger.error("Error stopping Observability API server: %s", e)
                if self.observability_loop and not self.observability_loop.is_closed():
                    self.observability_loop.call_soon_threadsafe(self.observability_loop.stop)

    def update_config(self, config: ControllerConfig) -> None:
        """Update configuration for the controller API"""
        # Note: API server configuration cannot be updated while running
        # Only update the extracted config fields for future use
        with self.config_lock:
            # Observability API and fault tolerance configuration update
            self.enable_observability_api = config.observability_config.observability_enable
            self.enable_fault_tolerance = config.fault_tolerance_config.enable_fault_tolerance
            self.precision_auto_recovery_enabled = config.precision_auto_recovery_enabled

            logger.info("ControllerAPI configuration updated (runtime changes may require restart)")

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        logger.info("API server startup started")
        yield
        logger.info("API server shutdown completed")

    # Observability API lifecycle management
    @asynccontextmanager
    async def _observability_api_lifespan(self, app: FastAPI):
        logger.info("Observability API server startup started")
        yield
        logger.info("Observability API server shutdown completed")

    @observability_enabled_required
    async def _get_inventory(self) -> dict[str, Any]:
        try:
            return format_success_response(InventoryCollector().collect_inventory())

        except Exception as e:
            logger.error("Failed to get inventory: %s", e)
            raise_internal_error(f"Internal server error: {str(e)}")

    @observability_enabled_required
    async def _get_metrics(self, request: Request):
        """[DEPRECATED] Metrics proxy: forward to Coordinator.
        Use Coordinator's GET /metrics?type={type}&role={role} directly.
        """
        logger.warning(
            "[DEPRECATED] /observability/metrics is deprecated. Use Coordinator's GET /metrics?type=%s instead.",
            request.query_params.get("type", "full"),
        )
        try:
            metrics_type = request.query_params.get("type", "full").strip()
            role = request.query_params.get("role", None)
            if role is not None:
                role = role.strip()
            metrics_data = self.observability.get_metrics(metrics_type=metrics_type, role=role)
            return PlainTextResponse(content=metrics_data)
        except Exception as e:
            logger.error("Failed to get metrics: %s", e)
            raise_internal_error("Internal server error: %s" % str(e))

    @observability_enabled_required
    async def _get_alarms(self, request: Request) -> dict[str, Any]:
        try:
            source_id = request.query_params.get("source_id", None)
            alarms = self.observability.get_alarms(source_id=source_id)

            return format_success_response(
                {
                    "total": len(alarms),
                    "alarms": alarms,
                }
            )
        except Exception as e:
            logger.error("Failed to get alarms: %s", e)
            raise_internal_error(f"Internal server error: {str(e)}")

    def _create_app(self) -> FastAPI:
        app = FastAPI(lifespan=self._lifespan)

        # Apply filter to suppress access logs for specified APIs unless level >= configured level
        api_filters = {
            "/controller/heartbeat": logging.ERROR,
            "/controller/register": logging.INFO,
            "/controller/reregister": logging.INFO,
            "/controller/terminate_instance": logging.INFO,
            "/controller/report_software_fault": logging.INFO,
            "/observability/add_alarm": logging.INFO,
            "/startup": logging.ERROR,
            "/readiness": logging.ERROR,
            "/liveness": logging.ERROR,
        }
        logging.getLogger("uvicorn.access").addFilter(ApiAccessFilter(api_filters))

        # Register routes
        post_methods = ["POST"]
        get_methods = ["GET"]
        app.add_api_route("/controller/heartbeat", self._heartbeat, methods=post_methods)
        app.add_api_route("/controller/register", self._register, methods=post_methods)
        app.add_api_route("/controller/reregister", self._reregister, methods=post_methods)
        app.add_api_route("/controller/terminate_instance", self._terminate_instance, methods=post_methods)

        app.add_api_route("/startup", self._startup, methods=get_methods)
        app.add_api_route("/readiness", self._readiness, methods=get_methods)
        app.add_api_route("/liveness", self._liveness, methods=get_methods)

        app.add_api_route("/observability/add_alarm", self._add_alarm, methods=post_methods)

        app.add_api_route("/controller/report_software_fault", self._report_software_fault, methods=post_methods)

        return app

    async def _heartbeat(self, request: Request):
        body = await request.json()
        try:
            hb_msg = HeartbeatMsg(**body)
        except Exception as e:
            logger.error("Failed to parse HeartbeatMsg: %s, body: %s", e, body)
            return {"error": "Invalid HeartbeatMsg format"}
        ret = InstanceManager().handle_heartbeat(hb_msg)
        return {"result": ret}

    async def _register(self, request: Request) -> dict:
        body = await request.json()
        try:
            register_msg = RegisterMsg(**body)
        except Exception as e:
            logger.error("Failed to parse RegisterMsg: %s, body: %s", e, body)
            return {"error": "Invalid RegisterMsg format"}
        ret = InstanceAssembler().register(register_msg)
        if ret == -1:
            return {"error": "Instance already registered"}
        else:
            return {"result": ret}

    async def _reregister(self, request: Request) -> dict:
        body = await request.json()
        try:
            reregister_msg = ReregisterMsg(**body)
        except Exception as e:
            logger.error("Failed to parse ReregisterMsg: %s, body: %s", e, body)
            return {"error": "Invalid ReregisterMsg format"}
        ret = InstanceAssembler().reregister(reregister_msg)
        if ret == -1:
            return {"error": "Instance already registered"}
        else:
            return {"result": ret}

    def _run_api_server(self) -> None:
        try:
            server_config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="info")
            if self.mgmt_tls_config.enable_tls:
                server_config.load()
                context = CertUtil.create_ssl_context(self.mgmt_tls_config)
                if not context:
                    raise RuntimeError("Failed to create SSL context")

                server_config.ssl = context
                logger.info("Starting Controller API server on https://%s", format_address(self.host, self.port))
            else:
                logger.info("Starting Controller API server on http://%s", format_address(self.host, self.port))

            self.server = uvicorn.Server(server_config)
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.server.serve())
        except Exception as e:
            logger.error("API server error: %s", e)
        finally:
            if self.loop and not self.loop.is_closed():
                self.loop.close()

    async def _terminate_instance(self, request: Request) -> dict:
        body = await request.json()
        try:
            terminate_instance_msg = TerminateInstanceMsg(**body)
        except Exception as e:
            logger.error("Failed to parse TerminateInstanceMsg: %s, body: %s", e, body)
            return {"error": "Invalid TerminateInstanceMsg format"}
        logger.warning("Terminate instance, reason: %s", terminate_instance_msg.reason)
        if not terminate_instance_for_recovery(terminate_instance_msg.instance_id, terminate_instance_msg.reason):
            return {"error": "Instance not found or terminate failed"}
        return {"result": "Terminate instance succeed!"}

    async def _readiness(self) -> dict:
        """
        Readiness probe - returns result base on deploy mode and role:

        STANDALONE: returns 200 if overall healthy.
                    Otherwise, returns 503.

        MASTER_STANDBY: returns 200 only when role is master and overall healthy.
                        Otherwise, returns 503.

        """
        status = self._get_controller_status()
        msg = "message"
        reason = "reason"
        if status.get("overall_healthy") is False:
            raise HTTPException(status_code=503, detail={msg: "Controller is not ready", reason: "Overall not healthy"})

        if status.get("deploy_mode") == "master_standby":
            if status.get("role") != StandbyRole.MASTER.value:
                raise HTTPException(status_code=503, detail={msg: "Controller is not ready", reason: "Not master"})
        return {msg: "Controller is ready"}

    def _get_controller_status(self) -> dict:
        """
        Get controller status including:
        - deploy mode: "master_standby" or "standalone"
        - role(Optional): "master" or "standby"
        - overall health of all modules
        """
        status = {}

        # Set deploy mode and role
        with self.config_lock:
            enable_master_standby = self.enable_master_standby
        if enable_master_standby:
            status["deploy_mode"] = "master_standby"
            # Get singleton instance (assumes it has been initialized)
            if StandbyManager.is_initialized():
                status["role"] = "master" if StandbyManager().is_master() else "standby"
            else:
                status["role"] = "standby"
        else:
            status["deploy_mode"] = "standalone"

        # Check module health
        # In master_standby mode, standby node doesn't run modules, so don't check health
        if enable_master_standby and StandbyManager.is_initialized() and not StandbyManager().is_master():
            # Standby node: modules are not running, but this is expected
            status["overall_healthy"] = True
        else:
            unhealthy_modules = []
            for name, module in self.modules.items():
                if not hasattr(module, "is_alive"):
                    continue
                alive = module.is_alive()
                if not alive:
                    unhealthy_modules.append(name)

            if unhealthy_modules:
                status["overall_healthy"] = False
                logger.error("Unhealthy modules: %s", unhealthy_modules)
            else:
                status["overall_healthy"] = True

        return status

    async def _startup(self) -> dict:
        return {"message": "Controller startup"}

    async def _liveness(self) -> dict:
        """Liveness probe - returns 200 as long as the process is running"""
        status = self._get_controller_status()

        # For liveness, we just check if the process is responsive
        # Even standby controllers should be considered alive
        if status.get("overall_healthy") is False:
            raise HTTPException(
                status_code=503, detail={"message": "Controller is not alive", "reason": "Overall not healthy"}
            )
        else:
            return {"message": "Controller is alive"}

    async def _add_alarm(self, request: Request) -> dict:
        body = await request.json()
        try:
            record = Record(**body)
            # Precision auto-recovery does not require observability (OM) to be enabled.
            await self._maybe_precision_auto_recover(record)
            if not self.enable_observability_api:
                return format_success_response(message="OM is not enabled.")
            self.observability.add_alarm(record)
            return format_success_response()
        except Exception as e:
            logger.error("Failed to add alarms: %s", e)
            raise_internal_error(f"Internal server error: {str(e)}")

    async def _report_software_fault(self, request: Request) -> dict:
        """Receive software fault reports from NodeManagers at node granularity.

        Request body:
        {
            "exception_type": "RuntimeError",
            "exception_message": "engine crashed",
            "engine_id": 1,
            "engine_status": 1,
            "pod_ip": "192.168.1.1",
            "additional_info": {}
        }
        """
        if not self.enable_fault_tolerance:
            return format_success_response(message="Fault tolerance is not enabled")

        body = await request.json()
        try:
            exception_message = body.get("exception_message", "")
            engine_id = body.get("engine_id")
            engine_status = body.get("engine_status")
            pod_ip = body.get("pod_ip", "")
            additional_info = body.get("additional_info")

            if engine_id is None or engine_status is None:
                return raise_internal_error("Missing required fields: engine_id, engine_status")
            if not pod_ip:
                return raise_internal_error("Missing required field: pod_ip")

            exc = RuntimeError(exception_message or "")
            fault_info = FaultInfo.from_exception(
                exception=exc,
                engine_id=int(engine_id),
                engine_status=int(engine_status),
                additional_info=additional_info,
            )

            FaultManager().report_software_fault(fault_info, pod_ip=pod_ip)
            return format_success_response(message="Software fault reported successfully")
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to report software fault: %s, body: %s", e, body)
            return raise_internal_error(f"Internal server error: {str(e)}")

    async def _maybe_precision_auto_recover(self, record: Record) -> None:
        """Terminate PD instance group when precision alarm reports and auto-recovery is enabled."""
        if record.alarm_id != PRECISION_ISSUE_ALARM_ID:
            return
        with self.config_lock:
            allow = self.precision_auto_recovery_enabled
        if not allow:
            return

        # Terminate D instance
        d_id = None
        try:
            d_id = int(record.instance_id) if record.instance_id else None
        except (TypeError, ValueError):
            logger.error("Precision auto-recover: invalid instance_id %r", record.instance_id)
        if d_id is not None:
            logger.warning(
                "Precision auto-recover: terminating D instance_id=%s",
                d_id,
            )
            if not terminate_instance_for_recovery(d_id, "precision_alarm"):
                logger.error("Precision auto-recover: failed for D instance_id=%s", d_id)

        # Terminate P instance
        p_id = None
        try:
            p_id = int(record.p_instance_id) if record.p_instance_id else None
        except (TypeError, ValueError):
            logger.error("Precision auto-recover: invalid p_instance_id %r", record.p_instance_id)
        if p_id is not None:
            logger.warning(
                "Precision auto-recover: terminating P instance_id=%s",
                p_id,
            )
            if not terminate_instance_for_recovery(p_id, "precision_alarm"):
                logger.error("Precision auto-recover: failed for P instance_id=%s", p_id)

    def _create_observability_app(self) -> FastAPI:
        app = FastAPI(lifespan=self._observability_api_lifespan)

        # Apply filter to suppress access logs for specified APIs unless level >= configured level
        api_filters = {
            "/observability/inventory": logging.ERROR,
            "/observability/metrics": logging.ERROR,
            "/observability/alarms": logging.ERROR,
        }
        logging.getLogger("uvicorn.access").addFilter(ApiAccessFilter(api_filters))

        # Register middleware check role
        app.middleware("http")(self._master_standby_middleware)

        # Register observability routes
        get_methods = ["GET"]
        app.add_api_route("/observability/inventory", self._get_inventory, methods=get_methods)
        app.add_api_route("/observability/metrics", self._get_metrics, methods=get_methods)
        app.add_api_route("/observability/alarms", self._get_alarms, methods=get_methods)

        return app

    async def _master_standby_middleware(self, request: Request, call_next):
        # if enable master/standby and is standby role then raise exception
        if self.enable_master_standby and StandbyManager.is_initialized() and not StandbyManager().is_master():
            # raise exception at the middleware layer is an incorrect way. It is better to construct the response.
            raise_internal_error("This controller is not master")
        # master continue
        response = await call_next(request)
        return response

    def _run_observability_api_server(self) -> None:
        try:
            server_config = uvicorn.Config(
                self.observability_app,
                host=self.observability_api_host,
                port=self.observability_api_port,
                log_level="info",
            )
            if self.observability_tls_config.enable_tls:
                server_config.load()
                context = CertUtil.create_ssl_context(self.observability_tls_config)
                if not context:
                    raise RuntimeError("Failed to create SSL context")

                server_config.ssl = context
                logger.info(
                    "Starting observability API server on https://%s",
                    format_address(self.observability_api_host, self.observability_api_port),
                )
            else:
                logger.info(
                    "Starting observability API server on http://%s",
                    format_address(self.observability_api_host, self.observability_api_port),
                )

            self.observability_server = uvicorn.Server(server_config)
            self.observability_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.observability_loop)
            self.observability_loop.run_until_complete(self.observability_server.serve())

        except Exception as e:
            logger.error("Observability API server error: %s", e)
        finally:
            if self.observability_loop and not self.observability_loop.is_closed():
                self.observability_loop.close()
