# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import ipaddress
import os
import threading
import time
from typing import Any

import requests

from motor.common.resources import InsEventMsg
from motor.common.http.http_client import ConnectionMode, SafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.common.utils.net import format_address
from motor.config.controller import ControllerConfig
from motor.config.coordinator import CoordinatorConfig, MGMT_API_KEY_HEADER

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)

# --- Persistent client for instance-refresh pushes (Keep-Alive) ---
_REFRESH_CLIENT: SafeHTTPSClient | None = None
_REFRESH_CLIENT_LOCK = threading.Lock()  # protects client creation / reset
_REFRESH_REQUEST_LOCK = threading.Lock()  # serialises request+retry+reset on the shared client
_REFRESH_TIMEOUT = 3  # per-request TCP timeout (seconds)
# After failover the Coordinator Service still points at the isolated old master
# (or at no Ready endpoint). SET must go to the new master's pod IP until kubelet
# updates endpoints. None means the configured Service DNS.
_REFRESH_HOST_OVERRIDE: str | None = None
_COORDINATOR_POD_LABEL = "app=mindie-motor-coordinator"
_COORDINATOR_POD_PREFIX = "mindie-motor-coordinator"
_POD_IP_CACHE_TTL_SEC = 5.0
_POD_IP_CACHE_LOCK = threading.Lock()
_POD_IP_CACHE_AT = 0.0
_POD_IP_CACHE: tuple[str, ...] = ()
_K8S_CLIENT = None
_PUSH_TIMEOUT_SEC = 2.0


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


def _parse_refresh_host(host: str | None) -> str | None:
    """Accept a pod IP (v4/v6). Service DNS and empty values are ignored."""
    if not host:
        return None
    stripped = host.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]
    try:
        ipaddress.ip_address(stripped)
    except ValueError:
        logger.warning("Ignoring non-IP Coordinator refresh host %r", host)
        return None
    return stripped


def _k8s_client():
    global _K8S_CLIENT
    if _K8S_CLIENT is None:
        from motor.controller.fault_tolerance.k8s.k8s_client import K8sClient

        _K8S_CLIENT = K8sClient()
    return _K8S_CLIENT


def _list_coordinator_pod_ips() -> list[str]:
    """Discover Coordinator pod IPs so SET/ADD can reach standby before it is Ready."""
    global _POD_IP_CACHE_AT, _POD_IP_CACHE
    now = time.monotonic()
    with _POD_IP_CACHE_LOCK:
        if _POD_IP_CACHE and now - _POD_IP_CACHE_AT < _POD_IP_CACHE_TTL_SEC:
            return list(_POD_IP_CACHE)
    namespace = os.getenv("POD_NAMESPACE", "").strip()
    if not namespace:
        return []
    try:
        k8s = _k8s_client()
        ips = k8s.list_pod_ips(namespace, label_selector=_COORDINATOR_POD_LABEL)
        if not ips:
            ips = k8s.list_pod_ips(namespace, name_prefix=_COORDINATOR_POD_PREFIX)
    except Exception as e:
        logger.warning("Failed to list Coordinator pod IPs: %s", e)
        return []
    unique = tuple(dict.fromkeys(ips))
    with _POD_IP_CACHE_LOCK:
        _POD_IP_CACHE_AT = now
        _POD_IP_CACHE = unique
    return list(unique)


def _instance_refresh_hosts() -> list[str | None]:
    """Targets for instance refresh: new-master override first, then every pod IP."""
    ordered: list[str] = []
    override = _REFRESH_HOST_OVERRIDE
    if override:
        ordered.append(override)
    for ip in _list_coordinator_pod_ips():
        if ip not in ordered:
            ordered.append(ip)
    if ordered:
        return ordered
    return [None]


class CoordinatorApiClient:
    controller_config = ControllerConfig.from_json()
    coordinator_config = CoordinatorConfig.from_json()

    @staticmethod
    def reset_refresh_client() -> None:
        """Drop the shared instance-refresh Keep-Alive client.

        Coordinator Service endpoints change after master/standby failover.
        Reusing a TCP connection pinned to an isolated pod delays SET until that
        connection times out.
        """
        _reset_refresh_client()

    @staticmethod
    def set_refresh_host(host: str | None) -> None:
        """Send later instance-refresh POSTs to this Coordinator pod IP.

        Pass None to go back to coordinator_api_dns. A non-IP string is ignored
        and does not clear an already-set pod IP.
        """
        global _REFRESH_HOST_OVERRIDE
        if host is None:
            parsed: str | None = None
        else:
            parsed = _parse_refresh_host(host)
            if parsed is None:
                return
        changed = False
        with _REFRESH_CLIENT_LOCK:
            if _REFRESH_HOST_OVERRIDE != parsed:
                _REFRESH_HOST_OVERRIDE = parsed
                changed = True
        if changed:
            _reset_refresh_client()
            if parsed:
                logger.warning("Instance refresh target switched to new Coordinator master %s", parsed)

    @staticmethod
    def get_refresh_host() -> str | None:
        return _REFRESH_HOST_OVERRIDE

    @staticmethod
    def clear_refresh_host(expected: str) -> bool:
        """Clear the pod-IP pin only if it still matches ``expected``."""
        global _REFRESH_HOST_OVERRIDE
        with _REFRESH_CLIENT_LOCK:
            if not expected or _REFRESH_HOST_OVERRIDE != expected:
                return False
            _REFRESH_HOST_OVERRIDE = None
        _reset_refresh_client()
        logger.warning("Coordinator pod %s unreachable; instance refresh falls back to Service DNS", expected)
        return True

    @staticmethod
    def send_instance_refresh(event_msg: InsEventMsg) -> bool:
        """Push an instance event to every reachable Coordinator.

        Standby must receive SET/ADD before it is Ready, otherwise it cannot
        join the inference Service and the 30s check hits an empty window.
        Isolated old-master IPs time out quickly and are skipped.
        Returns True if at least one target accepted the event.
        """
        hosts = _instance_refresh_hosts()
        last_error: Exception | None = None
        last_address = "unknown"
        any_ok = False

        for host in hosts:
            with _REFRESH_REQUEST_LOCK:
                try:
                    args = CoordinatorApiClient._generate_client_args(host=host)
                    last_address = args.get("address", "unknown")
                    with SafeHTTPSClient(
                        mode=ConnectionMode.SHORT,
                        timeout=_PUSH_TIMEOUT_SEC,
                        **args,
                    ) as client:
                        response = client.post("/instances/refresh", data=event_msg.model_dump())
                    response_text = response.get("text") if isinstance(response, dict) else response
                    if event_msg.instances and len(event_msg.instances) > 0:
                        job_names = [instance.job_name for instance in event_msg.instances]
                        job_names_str = ", ".join(job_names)
                        logger.info(
                            "Event pushed type: %s, job names: [%s], response: %s address=%s",
                            event_msg.event,
                            job_names_str,
                            response_text,
                            last_address,
                        )
                    else:
                        logger.info(
                            "Event pushed type: %s, push all instances, response: %s address=%s",
                            event_msg.event,
                            response_text,
                            last_address,
                        )
                    any_ok = True
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "Instance refresh to %s failed: %s",
                        last_address if last_address != "unknown" else host or "service-dns",
                        e,
                    )
                    _reset_refresh_client()

        if any_ok:
            return True

        if None not in hosts:
            logger.warning("All Coordinator pod-IP refreshes failed; retrying via Service DNS")
            with _REFRESH_REQUEST_LOCK:
                try:
                    args = CoordinatorApiClient._generate_client_args(host=None)
                    last_address = args.get("address", "unknown")
                    with SafeHTTPSClient(
                        mode=ConnectionMode.SHORT,
                        timeout=_PUSH_TIMEOUT_SEC,
                        **args,
                    ) as client:
                        client.post("/instances/refresh", data=event_msg.model_dump())
                    return True
                except Exception as e:
                    last_error = e
                    logger.warning("Instance refresh via Service DNS failed: %s", e)
                    _reset_refresh_client()

        _rl.error_window(
            "coordinator.send_instance_refresh",
            "Exception occurred while pushing event to %s: %s" % (last_address, last_error),
            window_sec=60,
        )
        return False

    @staticmethod
    def notify_precision_alarm_cleared(
        p_instance_id: int | None,
        d_instance_id: int,
    ) -> bool:
        """Ask Coordinator to drop precision alarm state for a PD group."""
        client_args = CoordinatorApiClient._generate_client_args()
        payload = {
            "p_instance_id": p_instance_id,
            "d_instance_id": d_instance_id,
        }
        try:
            with SafeHTTPSClient(timeout=3, **client_args) as client:
                response = client.do_post("/precision/alarm_cleared", data=payload)
                if response.status_code != 200:
                    logger.warning(
                        "Coordinator precision alarm_cleared HTTP %s pd_group=(%s,%s)",
                        response.status_code,
                        p_instance_id,
                        d_instance_id,
                    )
                    return False
                body = response.json()
                data = body.get("data") if isinstance(body, dict) else None
                dismissed = isinstance(data, dict) and bool(data.get("dismissed"))
                if not dismissed:
                    logger.warning(
                        "Coordinator precision alarm_cleared not dismissed pd_group=(%s,%s) body=%s",
                        p_instance_id,
                        d_instance_id,
                        body,
                    )
                    return False
                logger.info(
                    "Coordinator precision alarm_cleared ok pd_group=(%s,%s)",
                    p_instance_id,
                    d_instance_id,
                )
                return True
        except requests.HTTPError as e:
            status_code = getattr(e.response, "status_code", "unknown")
            logger.warning(
                "Coordinator precision alarm_cleared HTTP %s pd_group=(%s,%s)",
                status_code,
                p_instance_id,
                d_instance_id,
            )
            return False
        except Exception as e:
            logger.warning(
                "Coordinator precision alarm_cleared failed pd_group=(%s,%s): %s",
                p_instance_id,
                d_instance_id,
                e,
            )
            return False

    @staticmethod
    def query_status(params: dict[str, str] | None = None) -> dict[str, str]:
        client_ars = CoordinatorApiClient._generate_client_args(include_mgmt_api_key=False)
        address = client_ars.get("address", "unknown")
        try:
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
    def _generate_client_args(
        cls,
        include_mgmt_api_key: bool = True,
        host: str | None = None,
    ) -> dict[str, Any]:
        tls_config = cls.controller_config.mgmt_tls_config
        api_config = cls.coordinator_config.api_config
        target = host or _REFRESH_HOST_OVERRIDE
        if target:
            address = format_address(target, api_config.coordinator_api_mgmt_port)
        else:
            address = f"{api_config.coordinator_api_dns}:{api_config.coordinator_api_mgmt_port}"
        client_args: dict[str, Any] = {"address": address, "tls_config": tls_config}
        mgmt_api_key_config = cls.coordinator_config.mgmt_api_key_config
        if include_mgmt_api_key and mgmt_api_key_config.enable_api_key:
            client_args["headers"] = {MGMT_API_KEY_HEADER: mgmt_api_key_config.load_api_key()}
        return client_args

    @classmethod
    def _generate_obs_client_args(cls) -> dict[str, str]:
        tls_config = cls.controller_config.mgmt_tls_config
        api_config = cls.coordinator_config.api_config
        address = f"{api_config.coordinator_api_obs_dns}:{api_config.coordinator_obs_port}"
        return {"address": f"{address}", "tls_config": tls_config}
