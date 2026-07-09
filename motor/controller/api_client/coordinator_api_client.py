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

from motor.common.resources import InsEventMsg
from motor.common.http.http_client import ConnectionMode, SafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.config.controller import ControllerConfig
from motor.config.coordinator import CoordinatorConfig

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)

# --- Persistent client for instance-refresh pushes (Keep-Alive) ---
_REFRESH_CLIENT: SafeHTTPSClient | None = None
_REFRESH_CLIENT_LOCK = threading.Lock()  # protects client creation / reset
_REFRESH_REQUEST_LOCK = threading.Lock()  # serialises request+retry+reset on the shared client
_REFRESH_TIMEOUT = 3  # per-request TCP timeout (seconds)
_REFRESH_RETRY_DELAYS = (1, 2)  # back-off between retries (seconds)


def _get_refresh_client() -> SafeHTTPSClient:
    """Return a shared long-lived HTTP client for instance-refresh pushes."""
    global _REFRESH_CLIENT
    if _REFRESH_CLIENT is not None:
        return _REFRESH_CLIENT

    with _REFRESH_CLIENT_LOCK:
        if _REFRESH_CLIENT is not None:
            return _REFRESH_CLIENT
        client_args = CoordinatorApiClient._generate_client_args()
        _REFRESH_CLIENT = SafeHTTPSClient(
            mode=ConnectionMode.LONG,
            timeout=_REFRESH_TIMEOUT,
            **client_args,
        )
        logger.info(
            "Instance-refresh HTTP client created (Keep-Alive, timeout=%ds) → %s",
            _REFRESH_TIMEOUT,
            client_args.get("address", "unknown"),
        )
        return _REFRESH_CLIENT


def _reset_refresh_client() -> None:
    """Close and discard the shared refresh client."""
    global _REFRESH_CLIENT
    with _REFRESH_CLIENT_LOCK:
        if _REFRESH_CLIENT is not None:
            try:
                _REFRESH_CLIENT.close()
            except Exception:  # nosec B110 — best-effort close on a client we are discarding
                pass
            _REFRESH_CLIENT = None


class CoordinatorApiClient:
    controller_config = ControllerConfig.from_json()
    coordinator_config = CoordinatorConfig.from_json()

    @staticmethod
    def send_instance_refresh(event_msg: InsEventMsg) -> bool:
        """Push an instance event to the Coordinator with retry on failure.

        Uses a persistent Keep-Alive connection.  On TCP-level failure the stale
        client is discarded and up to two retries (1 s, 2 s back-off) are attempted.
        Returns True on success, False after all attempts are exhausted.

        The shared Keep-Alive client is guarded by a request lock so that
        concurrent callers cannot interleave requests on the same connection
        or reset the client while another thread is using it.
        """
        total_attempts = 1 + len(_REFRESH_RETRY_DELAYS)
        last_error: Exception | None = None
        client_args = CoordinatorApiClient._generate_client_args()

        for attempt in range(total_attempts):
            with _REFRESH_REQUEST_LOCK:
                try:
                    client = _get_refresh_client()
                    response = client.post("/instances/refresh", data=event_msg.model_dump())
                    response_text = response.get("text")

                    if event_msg.instances and len(event_msg.instances) > 0:
                        job_names = [instance.job_name for instance in event_msg.instances]
                        job_names_str = ", ".join(job_names)
                        logger.info(
                            "Event pushed type: %s, job names: [%s], response: %s (attempt %d/%d)",
                            event_msg.event,
                            job_names_str,
                            response_text,
                            attempt + 1,
                            total_attempts,
                        )
                    else:
                        logger.info(
                            "Event pushed type: %s, push all instances, response: %s (attempt %d/%d)",
                            event_msg.event,
                            response_text,
                            attempt + 1,
                            total_attempts,
                        )
                    return True

                except Exception as e:
                    last_error = e
                    _reset_refresh_client()
            # Lock released — sleep outside the critical section so other
            # threads can push events between retry attempts.
            if attempt < len(_REFRESH_RETRY_DELAYS):
                delay = _REFRESH_RETRY_DELAYS[attempt]
                logger.debug(
                    "Instance refresh attempt %d/%d failed (%s), retrying in %ds...",
                    attempt + 1,
                    total_attempts,
                    last_error,
                    delay,
                )
                time.sleep(delay)

        address = client_args.get("address", "unknown")
        _rl.error_window(
            "coordinator.send_instance_refresh",
            "Exception occurred while pushing event to %s: %s" % (address, last_error),
            window_sec=60,
        )
        return False

    @staticmethod
    def query_status(params: dict[str, str] | None = None) -> dict[str, str]:
        try:
            client_ars = CoordinatorApiClient._generate_client_args()
            client = SafeHTTPSClient(**client_ars, timeout=3)
            response = client.get("/readiness", params=params)
            _rl.record_success("controller.coordinator.query_status")
            _rl.emit_periodic(
                "controller.coordinator.query_status",
                "Controller->Coordinator query_status periodic summary: succeeded {count} times in last 60s",
                level="DEBUG",
            )
            return response
        except Exception as e:
            address = CoordinatorApiClient._generate_client_args().get("address", "unknown")
            # Rate-limit: the heartbeat detector runs frequently and repeated
            # connection failures flood the log.  Collapse into periodic summaries.
            _rl.error_window(
                "coordinator.query_status",
                "Controller->Coordinator query_status failed. address=%s, error=%s" % (address, e),
                window_sec=60,
            )
            raise e

    @staticmethod
    def get_metrics(metrics_type: str = "full", role: str | None = None) -> str | None:
        """
        Get metrics from Coordinator. Internal API, not exposed via Controller HTTP.
        Calls GET /metrics?type=<metrics_type>&role=<role>.
        Returns Prometheus text, or None on failure.
        """
        client = None
        try:
            client_ars = CoordinatorApiClient._generate_obs_client_args()
            address = client_ars.get("address", "unknown")
            client = SafeHTTPSClient(**client_ars, timeout=5.0)
            url = f"/metrics?type={metrics_type}"
            if role:
                url += f"&role={role}"
            response = client.do_get(url)
            if response and response.ok:
                metrics_key = f"controller.coordinator.get_metrics.{metrics_type}.{role or 'all'}"
                logger.debug(
                    "Controller->Coordinator get_metrics success. address=%s, "
                    "metrics_type=%s, role=%s, status_code=%s, size=%s",
                    address,
                    metrics_type,
                    role,
                    response.status_code,
                    len(response.text),
                )
                _rl.record_success(metrics_key)
                _rl.emit_periodic(
                    metrics_key,
                    "Controller->Coordinator get_metrics periodic summary: succeeded {count} times in last 60s",
                    level="DEBUG",
                )
                return response.text
            logger.warning(
                "Controller->Coordinator get_metrics non-2xx. address=%s, metrics_type=%s, role=%s, status_code=%s",
                address,
                metrics_type,
                role,
                getattr(response, "status_code", "unknown"),
            )
            return None
        except Exception as e:
            address = CoordinatorApiClient._generate_obs_client_args().get("address", "unknown")
            logger.error(
                "Controller->Coordinator get_metrics failed. address=%s, "
                "metrics_type=%s, role=%s, error=%s. "
                "Possible causes: 1) coordinator down 2) network issue. "
                "Check: ping %s.",
                address,
                metrics_type,
                role,
                e,
                address,
            )
            return None
        finally:
            if client is not None:
                client.close()

    @classmethod
    def _generate_client_args(cls) -> dict[str, str]:
        tls_config = cls.controller_config.mgmt_tls_config
        api_config = cls.coordinator_config.api_config
        address = f"{api_config.coordinator_api_dns}:{api_config.coordinator_api_mgmt_port}"
        return {"address": f"{address}", "tls_config": tls_config}

    @classmethod
    def _generate_obs_client_args(cls) -> dict[str, str]:
        tls_config = cls.controller_config.mgmt_tls_config
        api_config = cls.coordinator_config.api_config
        address = f"{api_config.coordinator_api_obs_dns}:{api_config.coordinator_obs_port}"
        return {"address": f"{address}", "tls_config": tls_config}
