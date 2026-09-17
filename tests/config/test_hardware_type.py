# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.config.node_manager import HardwareType


def test_hardware_type_a5_is_ascend950_only():
    assert HardwareType.TYPE_ASCEND950.value == "Ascend950"
    assert HardwareType.is_a5("Ascend950")
    assert not HardwareType.is_a5("800I-A2")
    assert not HardwareType.is_a5("800I-A3")
    assert not HardwareType.is_a5("850-Atlas-8p-8")
    assert {member.value for member in HardwareType} == {"800I-A2", "800I-A3", "Ascend950"}
