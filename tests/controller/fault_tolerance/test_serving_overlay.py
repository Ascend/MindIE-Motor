# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for the immutable Coordinator serving overlay."""

from unittest.mock import patch

import pytest

from motor.common.resources import EventType, Instance, ReadOnlyInstance
from motor.common.resources.endpoint import Endpoint
from motor.controller.fault_tolerance.dp_scale_down import (
    FtPhase,
    FtRuntime,
    get_ft_runtime_store,
)
from motor.controller.fault_tolerance.serving_overlay import ServingOverlay


@pytest.fixture(autouse=True)
def clear_runtime_store():
    get_ft_runtime_store().clear()
    yield
    get_ft_runtime_store().clear()


def _instance() -> Instance:
    instance = Instance(job_name="decode-0", model_name="model", id=7, role="decode")
    instance.add_endpoints(
        "192.0.2.1",
        {0: Endpoint(id=0, ip="192.0.2.1", business_port="8000")},
    )
    instance.add_endpoints(
        "192.0.2.2",
        {1: Endpoint(id=1, ip="192.0.2.2", business_port="8000")},
    )
    return instance


def test_transient_phase_withdraws_instance_from_serving_view():
    instance = _instance()
    get_ft_runtime_store().put(FtRuntime(instance_id=7, phase=FtPhase.SCALING_DOWN))

    assert ServingOverlay.project(ReadOnlyInstance(instance)) is None


def test_scaled_down_view_filters_dead_rank_without_mutating_authoritative_topology():
    instance = _instance()
    get_ft_runtime_store().put(FtRuntime(instance_id=7, phase=FtPhase.SCALED_DOWN_RUNNING, dead_committed=[1]))

    projected = ServingOverlay.project(instance)

    assert [endpoint.id for endpoint in projected.get_all_endpoints(True)] == [0]
    assert sorted(endpoint.id for endpoint in instance.get_all_endpoints(True)) == [
        0,
        1,
    ]
    assert list(projected.endpoints) == ["192.0.2.1"]


@patch("motor.controller.fault_tolerance.serving_overlay.CoordinatorApiClient.send_instance_refresh")
def test_withdraw_uses_full_del_and_publish_uses_filtered_add(mock_refresh):
    mock_refresh.return_value = True
    instance = _instance()

    assert ServingOverlay.withdraw(instance) is True
    get_ft_runtime_store().put(FtRuntime(instance_id=7, phase=FtPhase.SCALED_DOWN_RUNNING, dead_committed=[1]))
    assert ServingOverlay.publish(instance) is True

    withdraw_msg = mock_refresh.call_args_list[0].args[0]
    publish_msg = mock_refresh.call_args_list[1].args[0]
    assert withdraw_msg.event == EventType.DEL
    assert sorted(endpoint.id for endpoint in withdraw_msg.instances[0].get_all_endpoints(True)) == [0, 1]
    assert publish_msg.event == EventType.ADD
    assert [endpoint.id for endpoint in publish_msg.instances[0].get_all_endpoints(True)] == [0]


@patch("motor.controller.fault_tolerance.serving_overlay.CoordinatorApiClient.send_instance_refresh")
def test_consecutive_scale_down_withdraws_previous_published_view(mock_refresh):
    mock_refresh.return_value = True
    instance = _instance()
    get_ft_runtime_store().put(FtRuntime(instance_id=7, phase=FtPhase.SCALING_DOWN, dead_committed=[1]))

    assert ServingOverlay.withdraw(instance) is True

    withdraw_msg = mock_refresh.call_args.args[0]
    assert [endpoint.id for endpoint in withdraw_msg.instances[0].get_all_endpoints(True)] == [0]
    assert sorted(endpoint.id for endpoint in instance.get_all_endpoints(True)) == [
        0,
        1,
    ]
