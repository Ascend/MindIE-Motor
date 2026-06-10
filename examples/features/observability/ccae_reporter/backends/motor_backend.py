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
MotorBackend: used by the Controller's ccae_reporter.
- Alarms / inventory / readiness → Controller's own API (obs_client + probe_client).
- Metrics → Coordinator's API (coord_client), since all metrics processing is now on the Coordinator side.

The Coordinator's ccae_reporter uses BaseBackend directly.
"""

import os
import base64

from ccae_reporter.common.logging import Log
from ccae_reporter.config import ConfigUtil
from motor.common.http.http_client import SafeHTTPSClient
from motor.common.utils.env import Env
from .base_backend import BaseBackend


class MotorBackend(BaseBackend):
    def __init__(self, identity: str):
        super().__init__(identity)
        self.logger = Log(__name__).getlog()
        pod_ip = os.getenv("POD_IP")

        # Controller observability APIs (alarms, inventory)
        obs_port = ConfigUtil.get_config('motor_controller_config.api_config.observability_api_port')
        self.obs_client = SafeHTTPSClient(address="%s:%d" % (pod_ip, obs_port))

        # Controller probe API (readiness)
        controller_probe_port = ConfigUtil.get_config('motor_controller_config.api_config.controller_api_port')
        self.probe_client = SafeHTTPSClient(address="%s:%d" % (pod_ip, controller_probe_port))

        # Coordinator observability API (metrics now served by Coordinator's obs server)
        coord_obs_dns = Env.coordinator_obs_service or pod_ip or '127.0.0.1'
        coord_obs_port = ConfigUtil.get_config('motor_coordinator_config.api_config.coordinator_obs_port')
        self.coord_client = SafeHTTPSClient(address="%s:%d" % (coord_obs_dns, coord_obs_port))

    def fetch_alarm_info(self) -> list:
        if not self.is_alive():
            self.logger.warning("CCAE is not alive, skip fetching alarms info")
            return []
        url = "/observability/alarms"
        try:
            response = self.obs_client.do_get("%s?source_id=%s" % (url, os.getenv('NORTH_PLATFORM', 'ccae_reporter')))
            if response.status_code != 200:
                self.logger.error("Failed to fetch alarms info from %s", url)
                return []
            alarm_info = response.json()
            data = alarm_info.get("data")
            return data.get("alarms", [])
        except Exception as e:
            self.logger.error("Failed to fetch alarms info from %s: %s", url, e)
            return []

    def fetch_inventory_info(self, model_id: str) -> dict:
        inventory_url = "/observability/inventory"
        try:
            response = self.obs_client.do_get(inventory_url)
            if response.status_code != 200:
                self.logger.error("Failed to fetch inventory info from %s", inventory_url)
                return {}
            inventory_info = response.json()
            data = inventory_info.get("data")
            if data:
                metric_info = self._fetch_metrics_info()
                if metric_info:
                    data["metrics"] = {"metric": base64.b64encode(metric_info.encode()).decode(), "metricPeriod": 1}
                else:
                    data["metrics"] = {"metric": "", "metricPeriod": 1}
                data["modelID"] = model_id
            return {
                "componentType": 0 if self.identity == "Controller" else 1,
                "modelServiceInfo": [data],
            }
        except Exception as e:
            self.logger.error("Failed to fetch inventory info from %s: %s", inventory_url, e)
            return {}

    def is_alive(self) -> bool:
        url = "/readiness"
        try:
            response = self.probe_client.do_get(url)
            if response.status_code != 200:
                return False
            return True
        except Exception as e:
            self.logger.error("Failed to check liveness from %s: %s", url, e)
            return False

    def terminate_instance(self, instance_id: int, reason: str) -> bool:
        if not self.is_alive():
            self.logger.warning("Controller not ready (readiness), skip terminate_instance")
            return False
        try:
            response = self.probe_client.do_post(
                "/controller/terminate_instance",
                {"instance_id": instance_id, "reason": reason},
            )
            if response.status_code != 200:
                self.logger.error(
                    "terminate_instance HTTP %s: %s",
                    response.status_code,
                    getattr(response, "text", ""),
                )
                return False
            body = response.json()
            if isinstance(body, dict) and body.get("error"):
                self.logger.error("terminate_instance error: %s", body.get("error"))
                return False
            return True
        except Exception as e:
            self.logger.error("terminate_instance failed: %s", e)
            return False

    def _fetch_metrics_info(self) -> str:
        """Fetch metrics from Coordinator (all metrics are now served by Coordinator)."""
        metrics_url = "/metrics"
        try:
            response = self.coord_client.do_get(metrics_url)
            if response.status_code != 200:
                self.logger.error("Failed to fetch metrics info from %s", metrics_url)
                return ""
            return response.text
        except Exception as e:
            self.logger.error("Failed to fetch metrics info from %s: %s", metrics_url, e)
            return ""
