# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace
from unittest.mock import patch

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import PDRole
from motor.node_manager.core.services.native_engine.service import NativeEngineService
from motor.node_manager.core.services.native_engine.models import CommandSpec, LaunchSpec, ProbeSpec


def _native_engine_service() -> NativeEngineService:
    with (
        patch("motor.node_manager.core.services.native_engine.service.get_backend") as get_backend,
        patch("motor.node_manager.core.services.native_engine.service.ProcessSupervisor") as supervisor_class,
    ):
        service = NativeEngineService(
            engine_type="sglang",
            config_path="/tmp/config.json",
            device_num=1,
            parallel_config=SimpleNamespace(local_world_size=1),
            enable_multi_endpoints=False,
        )
    service.backend = get_backend.return_value
    service.supervisor = supervisor_class.return_value
    return service


def test_successful_pull_clears_recovery_latch():
    service = _native_engine_service()
    service._recovery_requested = True
    service.backend.prepare.return_value = LaunchSpec(
        command=CommandSpec(argv=("python", "-m", "sglang.launch_server"), env={}),
        probe=ProbeSpec(path="/health", timeout_seconds=1, startup_timeout_seconds=10),
    )
    service.supervisor.start.return_value = True
    endpoint = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="8001")

    service.pull(PDRole.ROLE_D, [endpoint], instance_id=1, master_dp_ip="127.0.0.1")

    assert service._recovery_requested is False
    service.supervisor.start.assert_called_once()


@patch("motor.node_manager.core.services.native_engine.service.os.kill")
def test_health_check_requests_recovery_only_once(mock_kill):
    service = _native_engine_service()
    service.restart_on_failure = True
    service.supervisor.dead_pids.side_effect = [[101], [102]]

    service.health_check()
    service.health_check()

    mock_kill.assert_called_once()
