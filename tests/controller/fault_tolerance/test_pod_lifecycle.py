# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for DP scale-down Pod lifecycle cleanup."""

from types import SimpleNamespace
from unittest.mock import patch

from motor.common.resources.instance import NodeManagerInfo
from motor.controller.fault_tolerance.pod_lifecycle import PodLifecycle


def _instance(*pod_ips: str):
    node_managers = [NodeManagerInfo(pod_ip=pod_ip, port="8001") for pod_ip in pod_ips]
    return SimpleNamespace(get_node_managers=lambda: node_managers)


@patch("motor.controller.fault_tolerance.pod_lifecycle.NodeManagerApiClient.stop")
def test_stop_recyclable_pods_stops_selected_node_managers_once(mock_stop):
    mock_stop.return_value = True
    instance = _instance("192.0.2.1", "192.0.2.2")

    PodLifecycle.stop_recyclable_pods(
        instance,
        ["192.0.2.1", "192.0.2.2"],
    )

    assert mock_stop.call_count == 2


@patch("motor.controller.fault_tolerance.pod_lifecycle.NodeManagerApiClient.stop")
def test_stop_recyclable_pods_does_not_retry_failed_or_missing_pods(mock_stop):
    mock_stop.return_value = False

    PodLifecycle.stop_recyclable_pods(
        _instance("192.0.2.1"),
        ["192.0.2.1", "192.0.2.9"],
    )

    mock_stop.assert_called_once()
