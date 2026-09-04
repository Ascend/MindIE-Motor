# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for workload shared memory role handling."""

import pytest

from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import Instance, InsStatus, PDRole
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.runtime.workload_shm.layout import ROLE_ENCODE
from motor.coordinator.scheduler.runtime.workload_shm.reader import _shm_role_to_pdrole
from motor.coordinator.scheduler.runtime.workload_shm.writer import (
    _collect_entries_and_slot_map,
    _pdrole_to_shm_role,
)


def _make_encode_instance() -> Instance:
    inst = Instance(
        job_name="encode-1",
        model_name="test_model",
        id=1,
        role=PDRole.ROLE_E,
        status=InsStatus.ACTIVE,
    )
    inst.add_endpoints(
        "pod-1",
        {
            0: Endpoint(
                id=10,
                ip="10.0.0.1",
                business_port="8000",
                status=EndpointStatus.NORMAL,
                workload=Workload(),
            )
        },
    )
    return inst


@pytest.mark.asyncio
async def test_workload_shm_snapshot_includes_encode_role():
    instance_manager = InstanceManager()
    await instance_manager.refresh_instances(EventType.ADD, [_make_encode_instance()])
    # ADD resets endpoint.workload; collect reads IM, so re-apply the tokens under test.
    await instance_manager.update_instance_workload(1, 10, Workload(active_tokens=7))

    entries, slot_map = _collect_entries_and_slot_map(instance_manager, max_entries=10)

    assert slot_map == {(1, 10): 0}
    assert entries == [(1, 10, ROLE_ENCODE, 7)]


def test_workload_shm_round_trips_encode_role():
    shm_role = _pdrole_to_shm_role(PDRole.ROLE_E)

    assert shm_role == ROLE_ENCODE
    assert _shm_role_to_pdrole(shm_role) == PDRole.ROLE_E
