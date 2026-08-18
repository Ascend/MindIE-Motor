# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for the Controller -> NodeManager client (engine restart dispatch)."""

from unittest.mock import MagicMock, patch

import pytest
from motor.common.resources.instance import NodeManagerInfo
from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient

# pylint: disable=redefined-outer-name


@pytest.fixture
def node_mgr():
    return NodeManagerInfo(pod_ip="10.0.0.1", port="8080")


@pytest.fixture
def mock_http_client():
    with patch("motor.controller.api_client.node_manager_api_client.SafeHTTPSClient") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        yield mock_client


def test_restart_engine_posts_restart(node_mgr, mock_http_client):
    assert NodeManagerApiClient.restart_engine(node_mgr, action="restart", instance_id=7) is True
    posted = mock_http_client.post.call_args
    assert posted.args[0] == "/node-manager/engine-restart"
    assert posted.kwargs["data"] == {"action": "restart", "instance_id": 7}


def test_restart_engine_abort_payload(node_mgr, mock_http_client):
    assert NodeManagerApiClient.restart_engine(node_mgr, action="abort") is True
    posted = mock_http_client.post.call_args
    assert posted.kwargs["data"] == {"action": "abort"}


def test_restart_engine_failure_returns_false(node_mgr, mock_http_client):
    mock_http_client.post.side_effect = RuntimeError("unreachable")
    assert NodeManagerApiClient.restart_engine(node_mgr, action="restart", instance_id=7) is False


def test_restart_engine_client_construction_failure_returns_false(node_mgr):
    """A SafeHTTPSClient construction failure must not raise UnboundLocalError in close()."""
    with patch(
        "motor.controller.api_client.node_manager_api_client.SafeHTTPSClient",
        side_effect=RuntimeError("tls config invalid"),
    ):
        assert NodeManagerApiClient.restart_engine(node_mgr, action="restart") is False
