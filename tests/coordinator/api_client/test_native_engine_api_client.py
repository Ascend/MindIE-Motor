# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import MagicMock, patch

from motor.config.tls_config import TLSConfig
from motor.coordinator.api_client.native_engine_api_client import NativeEngineApiClient


@patch("motor.coordinator.api_client.native_engine_api_client.SafeHTTPSClient")
def test_query_metrics_uses_inference_tls(mock_client):
    tls_config = TLSConfig(enable_tls=True)
    response = MagicMock(text="# TYPE ready gauge\nready 1", status_code=200)
    mock_client.return_value.__enter__.return_value.do_get.return_value = response

    result = NativeEngineApiClient.query_metrics("10.0.0.8:8000", tls_config)

    assert result == response.text
    mock_client.assert_called_once_with(
        address="10.0.0.8:8000",
        tls_config=tls_config,
        timeout=2,
    )
    mock_client.return_value.__enter__.return_value.do_get.assert_called_once_with("/metrics")


@patch("motor.coordinator.api_client.native_engine_api_client.SafeHTTPSClient")
def test_query_metrics_failure_returns_empty_text(mock_client):
    mock_client.side_effect = RuntimeError("connection refused")

    assert NativeEngineApiClient.query_metrics("10.0.0.8:8000", None) == ""


@patch("motor.coordinator.api_client.native_engine_api_client.SafeHTTPSClient")
def test_query_metrics_non_success_status_returns_empty_text(mock_client):
    response = MagicMock(text="internal error", status_code=503)
    mock_client.return_value.__enter__.return_value.do_get.return_value = response

    assert NativeEngineApiClient.query_metrics("10.0.0.8:8000", None) == ""
