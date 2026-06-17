# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from motor.common.resources import InsEventMsg
from motor.common.http.http_client import SafeHTTPSClient
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.config.controller import ControllerConfig
from motor.config.coordinator import CoordinatorConfig

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)


class CoordinatorApiClient:
    controller_config = ControllerConfig.from_json()
    coordinator_config = CoordinatorConfig.from_json()

    @staticmethod
    def send_instance_refresh(event_msg: InsEventMsg) -> bool:
        is_succeed = True
        client = None

        client_ars = CoordinatorApiClient._generate_client_args()
        try:
            client = SafeHTTPSClient(timeout=0.5, **client_ars)
            response = client.post("/instances/refresh", data=event_msg.model_dump())
            response_text = response.get("text")

            if event_msg.instances and len(event_msg.instances) > 0:
                job_names = [instance.job_name for instance in event_msg.instances]
                job_names_str = ", ".join(job_names)
                logger.info(
                    "Event pushed type: %s, job names: [%s], response: %s",
                    event_msg.event,
                    job_names_str,
                    response_text,
                )
            else:
                logger.info("Event pushed type: %s, push all instances, response: %s", event_msg.event, response_text)
        except Exception as e:
            is_succeed = False
            address = client_ars.get("address", "unknown")
            logger.error("Exception occurred while pushing event, %s, %s", address, e)
        finally:
            if client is not None:
                client.close()

        return is_succeed

    @staticmethod
    def query_status(params: dict[str, str] | None = None) -> dict[str, str]:
        try:
            client_ars = CoordinatorApiClient._generate_client_args()
            client = SafeHTTPSClient(**client_ars, timeout=0.5)
            response = client.get("/readiness", params=params)
            _rl.record_success("controller.coordinator.query_status")
            _rl.emit_info_periodic(
                "controller.coordinator.query_status",
                "Controller->Coordinator query_status periodic summary: succeeded {count} times in last 60s",
            )
            return response
        except Exception as e:
            address = CoordinatorApiClient._generate_client_args().get("address", "unknown")
            logger.error(
                "Controller->Coordinator query_status failed. address=%s, error=%s. "
                "Possible causes: 1) coordinator down 2) network unreachable 3) tls mismatch. "
                "Check: ping %s, coordinator process status.",
                address,
                e,
                address,
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
                _rl.emit_info_periodic(
                    metrics_key,
                    "Controller->Coordinator get_metrics periodic summary: succeeded {count} times in last 60s",
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
