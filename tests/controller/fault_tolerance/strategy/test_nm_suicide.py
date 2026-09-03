# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""NmSuicide stops every NodeManager of the isolated instance."""

from unittest.mock import Mock, patch

from motor.common.resources.instance import NodeManagerInfo
from motor.controller.api_client.node_manager_api_client import NodeManagerStopStatus
from motor.controller.fault_tolerance.strategy.nm_suicide import NmSuicideStrategy


def _instance(instance_id=1, nms=None):
    inst = Mock()
    inst.id = instance_id
    inst.job_name = "mindie-vllm-0-p0"
    inst.role = "prefill"
    inst.get_node_managers.return_value = nms or [
        NodeManagerInfo(pod_ip="10.0.0.1", port="1026"),
        NodeManagerInfo(pod_ip="10.0.0.2", port="1026"),
    ]
    return inst


def test_execute_stops_all_node_managers_of_the_instance():
    """A multi-pod instance must receive stop on every NodeManager."""
    inst = _instance()
    with (
        patch("motor.controller.fault_tolerance.strategy.nm_suicide.InstanceManager") as mock_im_cls,
        patch("motor.controller.fault_tolerance.strategy.nm_suicide.NodeManagerApiClient") as mock_client,
    ):
        mock_im_cls.return_value.get_instance.return_value = inst
        mock_im_cls.return_value.get_instance_by_job_name.return_value = inst
        mock_client.stop.return_value = True
        mock_client.stop_status.return_value = NodeManagerStopStatus.OK
        NmSuicideStrategy().execute(1)

    assert mock_client.stop_status.call_count == 2


def test_execute_skips_stop_when_a_newer_instance_id_exists():
    """Replacement Pods must not receive stop meant for a superseded id."""
    stale = _instance(instance_id=1)
    current = _instance(instance_id=3)
    with (
        patch("motor.controller.fault_tolerance.strategy.nm_suicide.InstanceManager") as mock_im_cls,
        patch("motor.controller.fault_tolerance.strategy.nm_suicide.NodeManagerApiClient") as mock_client,
    ):
        mock_im_cls.return_value.get_instance.return_value = stale
        mock_im_cls.return_value.get_instance_by_job_name.return_value = current
        NmSuicideStrategy().execute(1)

    mock_client.stop_status.assert_not_called()


def test_execute_treats_unreachable_stop_as_finished():
    """Timeout / refused stop still finishes the strategy so it does not fail-loop."""
    inst = _instance(nms=[NodeManagerInfo(pod_ip="10.0.0.1", port="1026")])
    strategy = NmSuicideStrategy()
    with (
        patch("motor.controller.fault_tolerance.strategy.nm_suicide.InstanceManager") as mock_im_cls,
        patch("motor.controller.fault_tolerance.strategy.nm_suicide.NodeManagerApiClient") as mock_client,
    ):
        mock_im_cls.return_value.get_instance.return_value = inst
        mock_im_cls.return_value.get_instance_by_job_name.return_value = inst
        mock_client.stop_status.return_value = NodeManagerStopStatus.UNREACHABLE
        strategy.execute(1)

    assert strategy.is_finished()
    assert not strategy.is_failed()


def test_execute_marks_failed_when_stop_is_rejected():
    """HTTP 5xx / other stop errors must fail so the center can retry or escalate."""
    inst = _instance(nms=[NodeManagerInfo(pod_ip="10.0.0.1", port="1026")])
    strategy = NmSuicideStrategy()
    with (
        patch("motor.controller.fault_tolerance.strategy.nm_suicide.InstanceManager") as mock_im_cls,
        patch("motor.controller.fault_tolerance.strategy.nm_suicide.NodeManagerApiClient") as mock_client,
    ):
        mock_im_cls.return_value.get_instance.return_value = inst
        mock_im_cls.return_value.get_instance_by_job_name.return_value = inst
        mock_client.stop_status.return_value = NodeManagerStopStatus.FAILED
        strategy.execute(1)

    assert strategy.is_finished()
    assert strategy.is_failed()
