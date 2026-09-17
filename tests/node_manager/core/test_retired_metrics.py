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
from unittest.mock import MagicMock, patch

with patch("motor.config.node_manager.NodeManagerConfig.from_json", return_value=MagicMock()):
    from motor.common.resources.endpoint import Endpoint
    from motor.node_manager.core.heartbeat_manager import HeartbeatManager


def test_engine_metrics_targets_exclude_retired_endpoints():
    manager = object.__new__(HeartbeatManager)
    manager._endpoint_lock = threading.Lock()
    manager._endpoints = [
        Endpoint(id=1, ip="10.0.0.1", business_port="8001"),
        Endpoint(id=2, ip="10.0.0.2", business_port="8002"),
    ]
    manager._retired_endpoint_ids = {2}

    with patch("motor.node_manager.core.daemon.Daemon") as daemon_cls:
        daemon_cls.return_value.get_engine_metrics_target.side_effect = lambda endpoint: endpoint.ip
        targets = manager.get_engine_metrics_targets()

    assert targets == ["10.0.0.1"]
