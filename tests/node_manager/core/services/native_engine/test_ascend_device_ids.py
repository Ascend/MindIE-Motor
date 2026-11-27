# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import patch

from motor.node_manager.core.services.native_engine.ascend_device_ids import (
    calc_endpoint_visible_device_ids,
    is_contiguous_logical_ids,
    list_engine_visible_device_ids,
    list_pod_device_ids,
    list_pod_physical_device_ids,
)


def test_list_pod_physical_device_ids_prefers_visible_env():
    env = {"ASCEND_VISIBLE_DEVICES": "14,10,11,15", "ASCEND_RT_VISIBLE_DEVICES": "0,1"}
    assert list_pod_physical_device_ids(env) == ["10", "11", "14", "15"]


def test_calc_endpoint_slices_remapped_davinci_nodes():
    pod = ["0", "1", "2", "3"]
    assert (
        calc_endpoint_visible_device_ids(0, 4, 2, pod_physical_ids=pod, single_container_flag=False, device_offset=0)
        == "0,1"
    )
    assert (
        calc_endpoint_visible_device_ids(1, 4, 2, pod_physical_ids=pod, single_container_flag=False, device_offset=0)
        == "2,3"
    )


def test_calc_endpoint_slices_passthrough_physical_nodes():
    pod = ["0", "1", "12", "13"]
    assert not is_contiguous_logical_ids(pod)
    assert (
        calc_endpoint_visible_device_ids(0, 4, 2, pod_physical_ids=pod, single_container_flag=False, device_offset=0)
        == "0,1"
    )
    assert (
        calc_endpoint_visible_device_ids(1, 4, 2, pod_physical_ids=pod, single_container_flag=False, device_offset=0)
        == "12,13"
    )


def test_calc_endpoint_single_container_uses_offset():
    assert (
        calc_endpoint_visible_device_ids(
            0,
            4,
            2,
            pod_physical_ids=["10", "11", "14", "15"],
            single_container_flag=True,
            device_offset=8,
        )
        == "8,9"
    )


def test_is_contiguous_logical_ids():
    assert is_contiguous_logical_ids(["0", "1", "2", "3"])
    assert not is_contiguous_logical_ids(["2", "3", "10", "11"])


def test_parse_allocated_device_ids_from_plugin_annotation():
    from motor.node_manager.core.services.native_engine.ascend_device_ids import (
        list_runtime_device_ids,
        parse_allocated_device_ids,
    )

    assert parse_allocated_device_ids("Ascend910-8,Ascend910-9,Ascend910-10,Ascend910-11") == [
        "8",
        "9",
        "10",
        "11",
    ]
    env = {
        "ASCEND_ALLOCATED_DEVICES": "Ascend910-8,Ascend910-9,Ascend910-10,Ascend910-11",
        "ASCEND_VISIBLE_DEVICES": "0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15",
    }
    assert list_runtime_device_ids(env, device_num=4) == ["8", "9", "10", "11"]
    assert (
        calc_endpoint_visible_device_ids(
            1, 4, 2, pod_physical_ids=["8", "9", "10", "11"], single_container_flag=False, device_offset=0
        )
        == "10,11"
    )


def test_list_pod_device_ids_preserves_start_device_order():
    env = {"MOTOR_START_DEVICES": "11,8,10,9", "ASCEND_VISIBLE_DEVICES": "8,9,10,11"}

    assert list_pod_device_ids(env, device_num=4) == ["11", "8", "10", "9"]


def test_list_pod_device_ids_falls_back_to_scheduler_allocation():
    env = {"ASCEND_ALLOCATED_DEVICES": "Ascend910-8,Ascend910-9,Ascend910-10,Ascend910-11"}

    assert list_pod_device_ids(env, device_num=4) == ["8", "9", "10", "11"]


def test_engine_visible_ids_stay_empty_on_device_plugin_pod():
    """A normal pod only has its own cards, so CANN must use logical 0..n-1."""
    env = {"ASCEND_ALLOCATED_DEVICES": "Ascend910-1"}
    with patch(
        "motor.node_manager.core.services.native_engine.ascend_device_ids.glob.glob",
        return_value=["/dev/davinci1"],
    ):
        assert list_engine_visible_device_ids(env) == []


def test_engine_visible_ids_slice_privileged_full_host():
    env = {"ASCEND_ALLOCATED_DEVICES": "Ascend910-8,Ascend910-9,Ascend910-10,Ascend910-11"}
    with patch(
        "motor.node_manager.core.services.native_engine.ascend_device_ids.glob.glob",
        return_value=[f"/dev/davinci{i}" for i in range(16)],
    ):
        assert list_engine_visible_device_ids(env) == ["8", "9", "10", "11"]


def test_engine_visible_ids_keep_allocation_when_dev_nodes_are_hidden():
    env = {"ASCEND_ALLOCATED_DEVICES": "Ascend910-8,Ascend910-9"}
    with patch(
        "motor.node_manager.core.services.native_engine.ascend_device_ids.glob.glob",
        return_value=[],
    ):
        assert list_engine_visible_device_ids(env) == ["8", "9"]
