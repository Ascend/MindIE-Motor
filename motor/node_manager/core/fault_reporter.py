# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""FaultReporter – subscribes to EngineServer ZMQ PUB sockets and forwards
software fault status updates to the Controller over HTTP.
"""

import threading

import zmq
import msgspec.msgpack

from motor.common.logger import get_logger
from motor.common.resources.endpoint import Endpoint
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.api_client.controller_api_client import ControllerApiClient

logger = get_logger(__name__)

# ZMQ PUB topic used by vllm ClientSentinel to broadcast engine status
_FAULT_STATE_PUB_TOPIC = "vllm_fault"

# Map engine status names (from EngineStatusType enum in vllm) to int values
_ENGINE_STATUS_NAME_TO_INT = {
    "healthy": 0,
    "dead": 1,
    "unhealthy": 2,
}


class FaultReporter:
    """Subscribes to per-engine ZMQ PUB sockets and reports non-healthy
    engines to the Controller.

    One ZMQ SUB socket is created for each EngineServer endpoint – the
    EngineServer publishes on ``base_port + engine_id``.  Status updates
    are msgpack-encoded and received asynchronously in a background thread.
    """

    def __init__(self, config: NodeManagerConfig):
        self._config = config
        self._config_lock = threading.RLock()
        self._enabled = config.fault_tolerance_config.enable_fault_tolerance
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._endpoints: list[Endpoint] = []

    def start(self, endpoints: list[Endpoint] | None = None) -> None:
        if not self._enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            logger.debug("FaultReporter thread already running")
            return
        if endpoints is not None:
            self._endpoints = endpoints
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._main_loop,
            daemon=True,
            name="fault_reporter",
        )
        self._thread.start()
        logger.info("FaultReporter started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning("FaultReporter thread did not stop within timeout")
        self._thread = None
        logger.info("FaultReporter stopped.")

    def update_config(self, config: NodeManagerConfig, endpoints: list[Endpoint]) -> None:
        with self._config_lock:
            old_enable = self._enabled
            old_endpoint_ids = {ep.id for ep in self._endpoints}
            old_pod_ip = self._config.api_config.pod_ip if self._endpoints else None
            old_zmq_port = self._config.fault_tolerance_config.zmq_pub_port
            self._config = config
            self._endpoints = endpoints
            self._enabled = config.fault_tolerance_config.enable_fault_tolerance

        new_endpoint_ids = {ep.id for ep in endpoints}
        endpoints_changed = old_endpoint_ids != new_endpoint_ids
        pod_ip_changed = old_pod_ip is not None and old_pod_ip != config.api_config.pod_ip
        zmq_port_changed = old_zmq_port != config.fault_tolerance_config.zmq_pub_port
        needs_restart = endpoints_changed or pod_ip_changed or zmq_port_changed

        if self._enabled != old_enable:
            if self._enabled:
                self.start()
                logger.info("FaultReporter enabled, started thread")
            else:
                self.stop()
                logger.info("FaultReporter disabled, stopped thread")
        elif self._enabled and needs_restart:
            # ZMQ connection parameters changed while enabled — restart to rebuild sockets
            changes = []
            if endpoints_changed:
                changes.append(f"endpoints ({len(old_endpoint_ids)} -> {len(new_endpoint_ids)} engines)")
            if pod_ip_changed:
                changes.append(f"pod_ip ({old_pod_ip} -> {config.api_config.pod_ip})")
            if zmq_port_changed:
                changes.append(f"zmq_pub_port ({old_zmq_port} -> {config.fault_tolerance_config.zmq_pub_port})")
            logger.info(
                "FaultReporter config changed (%s), restarting to rebuild ZMQ subscriptions",
                ", ".join(changes),
            )
            self.stop()
            self.start()

    def _setup_zmq_sub_sockets(self) -> tuple[list, zmq.Poller | None, zmq.Context | None]:
        """Create one ZMQ SUB socket per engine endpoint."""
        sub_sockets: list = []
        zmq_ctx = None
        try:
            with self._config_lock:
                pod_ip = self._config.api_config.pod_ip
                base_port = self._config.fault_tolerance_config.zmq_pub_port

            if base_port > 0 and len(self._endpoints) > 0:
                zmq_ctx = zmq.Context()
                for ep in self._endpoints:
                    port = base_port + ep.id
                    sub = zmq_ctx.socket(zmq.SUB)
                    sub.setsockopt(zmq.RECONNECT_IVL, 5000)
                    sub.connect(f"tcp://{pod_ip}:{port}")
                    sub.setsockopt_string(zmq.SUBSCRIBE, _FAULT_STATE_PUB_TOPIC)
                    sub_sockets.append(sub)
                    logger.info(
                        "ZMQ SUB connected to tcp://%s:%d for engine %d",
                        pod_ip,
                        port,
                        ep.id,
                    )
            else:
                logger.info(
                    "ZMQ fault pub port=%d, endpoints=%d, ZMQ subscription disabled",
                    base_port,
                    len(self._endpoints),
                )
        except Exception as e:
            logger.warning("Failed to set up ZMQ SUB sockets: %s, ZMQ subscription disabled", e)

        poller = None
        if sub_sockets:
            poller = zmq.Poller()
            for sub in sub_sockets:
                poller.register(sub, zmq.POLLIN)
        return sub_sockets, poller, zmq_ctx

    @staticmethod
    def _teardown_zmq_sub_sockets(
        sub_sockets: list,
        zmq_ctx: zmq.Context | None,
    ) -> None:
        for sub in sub_sockets:
            sub.close()
        if zmq_ctx:
            zmq_ctx.term()

    _ZMQ_RECONNECT_DELAY = 5.0  # seconds to wait before retrying ZMQ setup after error

    def _main_loop(self) -> None:
        """Subscribe to ZMQ PUB sockets and forward faults to Controller."""
        logger.info("FaultReporter loop started.")
        known_statuses: dict[int, str] = {}

        sub_sockets, poller, zmq_ctx = self._setup_zmq_sub_sockets()

        while not self._stop_event.is_set():
            if poller:
                try:
                    socks = dict(poller.poll(timeout=500))
                    for sub in sub_sockets:
                        if sub in socks:
                            topic, raw = sub.recv_multipart()
                            self._process_zmq_engine_status(raw, known_statuses)
                except zmq.ZMQError:
                    logger.warning(
                        "ZMQ error in poll loop, tearing down and reconnecting in %.1fs ...",
                        self._ZMQ_RECONNECT_DELAY,
                    )
                    self._teardown_zmq_sub_sockets(sub_sockets, zmq_ctx)
                    # Wait before retry, respecting stop_event
                    if self._stop_event.wait(timeout=self._ZMQ_RECONNECT_DELAY):
                        break
                    logger.info("Reconnecting ZMQ SUB sockets ...")
                    sub_sockets, poller, zmq_ctx = self._setup_zmq_sub_sockets()
                except Exception as e:
                    logger.error("Error processing ZMQ engine status: %s", e)
            else:
                # No sockets — avoid busy-wait; wait for stop or config change
                if self._stop_event.wait(timeout=1.0):
                    break
                # Retry setup in case endpoints/port are now available
                sub_sockets, poller, zmq_ctx = self._setup_zmq_sub_sockets()

        self._teardown_zmq_sub_sockets(sub_sockets, zmq_ctx)
        logger.info("FaultReporter loop stopped.")

    def _process_zmq_engine_status(
        self,
        raw: bytes,
        known_statuses: dict[int, str],
    ) -> None:
        """Decode a ZMQ PUB message and report new non-healthy engines."""
        msg = msgspec.msgpack.decode(raw)
        engines = msg.get("engines", [])

        for engine in engines:
            engine_id = engine["id"]
            status = engine["status"]

            if status == "healthy":
                known_statuses[engine_id] = status
                continue

            if known_statuses.get(engine_id) == status:
                continue  # already reported

            engine_status = _ENGINE_STATUS_NAME_TO_INT.get(status)
            if engine_status is None:
                logger.warning("Unknown engine status '%s' for engine %d", status, engine_id)
                continue

            exception_type = "EngineDeadError" if engine_status == 1 else "EngineUnhealthyError"
            exception_message = "Engine process died" if engine_status == 1 else "Engine unhealthy"

            fault_data = {
                "exception_type": exception_type,
                "exception_message": exception_message,
                "engine_id": engine_id,
                "engine_status": engine_status,
            }
            # Only mark as reported after successful delivery to Controller
            if self._send_fault_to_controller(fault_data):
                known_statuses[engine_id] = status

    def _send_fault_to_controller(self, fault_data: dict) -> bool:
        """Inject pod_ip and forward a single fault to Controller.

        Returns True if the fault was successfully reported, False otherwise.
        """
        with self._config_lock:
            fault_data["pod_ip"] = self._config.api_config.pod_ip

        logger.debug(
            "Forwarding software fault to Controller: engine_id=%s, type=%s",
            fault_data.get("engine_id"),
            fault_data.get("exception_type"),
        )
        return ControllerApiClient.report_software_fault(fault_data)
