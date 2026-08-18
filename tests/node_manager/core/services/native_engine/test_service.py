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

from motor.node_manager.core.services.native_engine.service import NativeEngineService


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


def test_health_check_returns_dead_pids():
    """health_check only surfaces dead PIDs; recovery routing is the Daemon's."""
    service = _native_engine_service()
    service.supervisor.dead_pids.return_value = [(101, 0)]

    assert service.health_check() == [(101, 0)]


def test_health_check_empty_when_no_deaths():
    service = _native_engine_service()
    service.supervisor.dead_pids.return_value = []

    assert service.health_check() == []
