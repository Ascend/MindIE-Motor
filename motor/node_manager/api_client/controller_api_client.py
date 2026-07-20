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

from motor.common.resources import RegisterMsg, ReregisterMsg, HeartbeatMsg
from motor.common.http.http_client import ConnectionMode, SafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.config.controller import ControllerConfig
from motor.config.node_manager import NodeManagerConfig

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)

# --- Persistent heartbeat client (long-lived Keep-Alive connection) ---
# A dedicated, reusable HTTP client with Connection: Keep-Alive avoids the
# overhead and jitter of a fresh TCP handshake on every heartbeat cycle (every
# 3 s).  In active-standby Controller deployments the K8s Service may briefly
# have zero ready endpoints during failover; retries with back-off absorb those
# transients instead of letting a single TCP-level failure ripple into a
# heartbeat timeout.
_HEARTBEAT_CLIENT: SafeHTTPSClient | None = None
_HEARTBEAT_CLIENT_LOCK = threading.Lock()  # protects client creation / reset
_HEARTBEAT_REQUEST_LOCK = threading.Lock()  # serialises request+retry+reset on the shared client
_HEARTBEAT_TIMEOUT = 5  # per-request TCP timeout (seconds)
_HEARTBEAT_RETRY_DELAYS = (1, 2)  # back-off between retries (seconds)


def _get_heartbeat_client() -> SafeHTTPSClient:
    """Return the shared long-lived heartbeat HTTP client, creating it on first use."""
    global _HEARTBEAT_CLIENT
    if _HEARTBEAT_CLIENT is not None:
        return _HEARTBEAT_CLIENT

    with _HEARTBEAT_CLIENT_LOCK:
        if _HEARTBEAT_CLIENT is not None:
            return _HEARTBEAT_CLIENT
        client_args = ControllerApiClient._generate_client_args()
        _HEARTBEAT_CLIENT = SafeHTTPSClient(
            mode=ConnectionMode.LONG,
            timeout=_HEARTBEAT_TIMEOUT,
            **client_args,
        )
        logger.info(
            "Heartbeat HTTP client created (Keep-Alive, timeout=%ds) → %s",
            _HEARTBEAT_TIMEOUT,
            client_args.get("address", "unknown"),
        )
        return _HEARTBEAT_CLIENT


def _reset_heartbeat_client() -> None:
    """Close and discard the shared heartbeat client so the next call creates a fresh one."""
    global _HEARTBEAT_CLIENT
    with _HEARTBEAT_CLIENT_LOCK:
        if _HEARTBEAT_CLIENT is not None:
            try:
                _HEARTBEAT_CLIENT.close()
            except Exception:  # nosec B110 — best-effort close on a client we are discarding
                pass
            _HEARTBEAT_CLIENT = None


class ControllerApiClient:
    controller_config = ControllerConfig.from_json()
    nodemanager_config = NodeManagerConfig.from_json()

    @staticmethod
    def register(register_msg: RegisterMsg) -> bool:
        client_args = {}
        try:
            client_args = ControllerApiClient._generate_client_args()
            with SafeHTTPSClient(timeout=15, **client_args) as client:
                response = client.post("/controller/register", register_msg.model_dump())
        except Exception as e:
            logger.error(
                "Exception occurred while register to controller at %s: %s", client_args.get("address", "unknown"), e
            )
            return False

        if not isinstance(response, dict):
            logger.error("Invalid register response from controller: %s", response)
            return False
        if error := response.get("error"):
            logger.warning("Register rejected by controller: %s", error)
            return False

        logger.info("Register success!")
        return True

    @staticmethod
    def re_register(re_register_msg: ReregisterMsg):
        client_args = {}
        try:
            client_args = ControllerApiClient._generate_client_args()
            with SafeHTTPSClient(timeout=15, **client_args) as client:
                _ = client.post("/controller/reregister", re_register_msg.model_dump())
                logger.info("Register success!")
                return True
        except Exception as e:
            logger.error(
                "Exception occurred while reregister to controller at %s: %s", client_args.get("address", "unknown"), e
            )
            return False

    @staticmethod
    def report_heartbeat(heartbeat_msg: HeartbeatMsg):
        """Send heartbeat to Controller over a persistent Keep-Alive connection.

        On TCP-level failure the stale client is discarded and up to two retries
        with back-off (1 s, 2 s) are attempted before propagating the exception.

        The shared Keep-Alive client is guarded by a request lock so that
        concurrent callers cannot interleave requests on the same connection
        or reset the client while another thread is using it.
        """
        last_error: Exception | None = None
        total_attempts = 1 + len(_HEARTBEAT_RETRY_DELAYS)

        for attempt in range(total_attempts):
            with _HEARTBEAT_REQUEST_LOCK:
                try:
                    client = _get_heartbeat_client()
                    response = client.post("/controller/heartbeat", heartbeat_msg.model_dump())
                except Exception as exc:
                    last_error = exc
                    # Discard the connection — it may be bound to a stale backend.
                    _reset_heartbeat_client()
                else:
                    # ---- success path ----
                    _rl.record_success("node_manager.controller.report_heartbeat")
                    _rl.emit_periodic(
                        "node_manager.controller.report_heartbeat",
                        "NodeManager->Controller report_heartbeat periodic summary: succeeded {count} times in last 60s",
                        level="DEBUG",
                    )
                    logger.debug(
                        "Heartbeat success (attempt %d/%d), response: %s",
                        attempt + 1,
                        total_attempts,
                        response,
                    )
                    return  # success — exit early

            # Lock released — sleep outside the critical section so other
            # threads can send heartbeats between retry attempts.
            if attempt < len(_HEARTBEAT_RETRY_DELAYS):
                delay = _HEARTBEAT_RETRY_DELAYS[attempt]
                logger.debug(
                    "Heartbeat attempt %d/%d failed (%s), retrying in %ds...",
                    attempt + 1,
                    total_attempts,
                    last_error,
                    delay,
                )
                time.sleep(delay)

        # All attempts exhausted — propagate the last error to the heartbeat loop.
        raise last_error  # type: ignore[misc]

    @staticmethod
    def report_software_fault(fault_data: dict):
        """Report a software fault to the Controller.

        Args:
            fault_data: dict with keys: exception_type, exception_message,
                        engine_id, engine_status, pod_ip, additional_info
        """
        client_args = {}
        try:
            client_args = ControllerApiClient._generate_client_args()
            with SafeHTTPSClient(timeout=15, **client_args) as client:
                response = client.post("/controller/report_software_fault", fault_data)
                logger.debug("Software fault reported successfully, response: %s", response)
                return True
        except Exception as e:
            logger.error(
                "Exception occurred while reporting software fault to controller at %s: %s",
                client_args.get("address", "unknown"),
                e,
            )
            return False

    @classmethod
    def _generate_client_args(cls) -> dict[str, str]:
        api_config = cls.controller_config.api_config
        tls_config = cls.nodemanager_config.mgmt_tls_config
        address = f"{api_config.controller_api_dns}:{api_config.controller_api_port}"
        client_ars = {"address": f"{address}", "tls_config": tls_config}
        return client_ars
