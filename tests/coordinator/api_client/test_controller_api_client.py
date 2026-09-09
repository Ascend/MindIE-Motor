# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

import json
from unittest.mock import MagicMock, patch

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_client.controller_api_client import ControllerApiClient


def setup_function():
    ControllerApiClient.controller_config = None
    ControllerApiClient.coordinator_config = None


def teardown_function():
    ControllerApiClient.controller_config = None
    ControllerApiClient.coordinator_config = None


def test_report_alarms_skips_controller_loading_in_standalone_mode(tmp_path):
    config_path = tmp_path / "standalone.json"
    config_path.write_text(
        json.dumps(
            {
                "motor_coordinator_config": {
                    "aigw": {"id": "test-model", "p_max_seqlen": 4096, "d_max_seqlen": 4096},
                }
            }
        ),
        encoding="utf-8",
    )
    config = CoordinatorConfig.from_json(str(config_path))
    ControllerApiClient.coordinator_config = config

    with patch(
        "motor.coordinator.api_client.controller_api_client.ControllerConfig.from_json",
        side_effect=AssertionError("Controller config must not be loaded"),
    ):
        outcome = ControllerApiClient.report_alarms({"alarm_id": "standalone"})

    assert outcome["ok"] is True
    assert ControllerApiClient.controller_config is None


def test_report_alarms_keeps_controller_mode_compatible(tmp_path):
    config_path = tmp_path / "full.json"
    config_path.write_text(
        json.dumps(
            {
                "motor_controller_config": {},
                "motor_coordinator_config": {},
            }
        ),
        encoding="utf-8",
    )
    coordinator_config = CoordinatorConfig.from_json(str(config_path))
    ControllerApiClient.coordinator_config = coordinator_config

    controller_config = MagicMock()
    controller_config.api_config.controller_api_dns = "controller.example"
    controller_config.api_config.controller_api_port = 1024
    ControllerApiClient.controller_config = controller_config

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"data": {}}
    client = MagicMock()
    client.do_post.return_value = response
    context = MagicMock()
    context.__enter__.return_value = client

    with patch(
        "motor.coordinator.api_client.controller_api_client.SafeHTTPSClient",
        return_value=context,
    ) as client_class:
        outcome = ControllerApiClient.report_alarms({"alarm_id": "controller"})

    assert outcome["ok"] is True
    client_class.assert_called_once()
    client.do_post.assert_called_once_with("/observability/add_alarm", {"alarm_id": "controller"})
