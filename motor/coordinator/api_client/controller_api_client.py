# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


from motor.common.http.http_client import SafeHTTPSClient
from motor.common.logger import get_logger
from motor.config.config_utils import has_motor_controller_config
from motor.config.controller import ControllerConfig
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.probe import is_master_from_role_shm

logger = get_logger(__name__)


class ControllerApiClient:
    controller_config: ControllerConfig | None = None
    coordinator_config: CoordinatorConfig | None = None

    @classmethod
    def _get_coordinator_config(cls) -> CoordinatorConfig:
        if cls.coordinator_config is None:
            cls.coordinator_config = CoordinatorConfig.from_json()
        return cls.coordinator_config

    @classmethod
    def _get_controller_config(cls) -> ControllerConfig:
        if cls.controller_config is None:
            cls.controller_config = ControllerConfig.from_json()
        return cls.controller_config

    @classmethod
    def report_alarms(cls, params: dict) -> dict:
        """POST alarm payload; return ``{ok, precision_alarm_cleared_by_auto_recovery}``."""
        client_args = {}
        default_outcome = {
            "ok": False,
            "precision_alarm_cleared": False,
            "precision_alarm_cleared_by_auto_recovery": False,
        }
        try:
            coordinator_config = cls._get_coordinator_config()
            if not has_motor_controller_config(coordinator_config.config_path):
                logger.debug("No motor_controller_config in user config; skip alarm reporting.")
                return {
                    "ok": True,
                    "precision_alarm_cleared": False,
                    "precision_alarm_cleared_by_auto_recovery": False,
                }

            if coordinator_config.standby_config.enable_master_standby:
                if not is_master_from_role_shm():
                    logger.debug("The standby coordinator does not need to report alarms.")
                    return {
                        "ok": True,
                        "precision_alarm_cleared": False,
                        "precision_alarm_cleared_by_auto_recovery": False,
                    }

            client_args = ControllerApiClient._generate_client_args()
            alarm_id = params.get("alarm_id", "")
            alarm_name = params.get("alarm_name", "")
            instance_id = params.get("instance_id", "")
            p_instance_id = params.get("p_instance_id", "")
            category = params.get("category", "")
            logger.info(
                "Reporting alarm to controller: alarm_id=%s alarm_name=%s category=%s "
                "instance_id=%s p_instance_id=%s controller=%s",
                alarm_id,
                alarm_name,
                category,
                instance_id,
                p_instance_id,
                client_args.get("address", "unknown"),
            )
            with SafeHTTPSClient(timeout=5, **client_args) as client:
                response = client.do_post("/observability/add_alarm", params)
                logger.info(
                    "Report alarms success! alarm_id=%s instance_id=%s p_instance_id=%s status=%s",
                    alarm_id,
                    instance_id,
                    p_instance_id,
                    response.status_code,
                )
                if response.status_code != 200:
                    return default_outcome
                try:
                    body = response.json()
                except Exception:
                    return {
                        "ok": True,
                        "precision_alarm_cleared": False,
                        "precision_alarm_cleared_by_auto_recovery": False,
                    }
                data = body.get("data") if isinstance(body, dict) else None
                if not isinstance(data, dict):
                    data = {}
                cleared = bool(
                    data.get("precision_alarm_cleared") or data.get("precision_alarm_cleared_by_auto_recovery")
                )
                return {
                    "ok": True,
                    "precision_alarm_cleared": cleared,
                    "precision_alarm_cleared_by_auto_recovery": bool(
                        data.get("precision_alarm_cleared_by_auto_recovery")
                    ),
                }
        except Exception as e:
            logger.error(
                "Exception occurred while reporting alarms at %s: %s", client_args.get("address", "unknown"), e
            )
            return default_outcome

    @classmethod
    def _generate_client_args(cls) -> dict[str, str]:
        api_config = cls._get_controller_config().api_config
        tls_config = cls._get_coordinator_config().mgmt_tls_config
        address = f"{api_config.controller_api_dns}:{api_config.controller_api_port}"
        client_ars = {"address": f"{address}", "tls_config": tls_config}
        return client_ars
