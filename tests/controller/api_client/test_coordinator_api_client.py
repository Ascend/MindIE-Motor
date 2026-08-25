# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import tempfile
from unittest.mock import MagicMock, patch

import requests

from motor.controller.api_client.coordinator_api_client import CoordinatorApiClient
from motor.config.coordinator import CoordinatorConfig, MGMT_API_KEY_HEADER


def _mock_safe_https_client(mock_cls: MagicMock, response: MagicMock) -> MagicMock:
    client = mock_cls.return_value.__enter__.return_value
    client.do_post.return_value = response
    return client


def test_notify_precision_alarm_cleared_returns_true_when_dismissed() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "status": "success",
        "data": {"dismissed": True},
    }

    with (
        patch.object(CoordinatorApiClient, "_generate_client_args", return_value={"address": "127.0.0.1:1234"}),
        patch("motor.controller.api_client.coordinator_api_client.SafeHTTPSClient") as mock_client_cls,
    ):
        client = _mock_safe_https_client(mock_client_cls, response)
        assert CoordinatorApiClient.notify_precision_alarm_cleared(1, 2) is True

    client.do_post.assert_called_once_with(
        "/precision/alarm_cleared",
        data={"p_instance_id": 1, "d_instance_id": 2},
    )


def test_notify_precision_alarm_cleared_returns_false_when_not_dismissed() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "status": "success",
        "data": {"dismissed": False},
    }

    with (
        patch.object(CoordinatorApiClient, "_generate_client_args", return_value={"address": "127.0.0.1:1234"}),
        patch("motor.controller.api_client.coordinator_api_client.SafeHTTPSClient") as mock_client_cls,
    ):
        _mock_safe_https_client(mock_client_cls, response)
        assert CoordinatorApiClient.notify_precision_alarm_cleared(1, 2) is False


def test_notify_precision_alarm_cleared_returns_false_on_http_error() -> None:
    http_error = requests.HTTPError("404 Client Error")
    http_error.response = MagicMock(status_code=404)

    with (
        patch.object(CoordinatorApiClient, "_generate_client_args", return_value={"address": "127.0.0.1:1234"}),
        patch("motor.controller.api_client.coordinator_api_client.SafeHTTPSClient") as mock_client_cls,
    ):
        client = mock_client_cls.return_value.__enter__.return_value
        client.do_post.side_effect = http_error
        assert CoordinatorApiClient.notify_precision_alarm_cleared(1, 2) is False


def test_generate_client_args_loads_management_api_key_from_file() -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as key_file:
        key_file.write("controller-management-key\n")
        api_key_file = key_file.name
    try:
        config = CoordinatorConfig()
        config.mgmt_api_key_config.enable_api_key = True
        config.mgmt_api_key_config.api_key_file = api_key_file

        with patch.object(CoordinatorApiClient, "coordinator_config", config):
            client_args = CoordinatorApiClient._generate_client_args()
            probe_client_args = CoordinatorApiClient._generate_client_args(include_mgmt_api_key=False)

        assert client_args["headers"] == {MGMT_API_KEY_HEADER: "controller-management-key"}
        assert "headers" not in probe_client_args
    finally:
        os.remove(api_key_file)
