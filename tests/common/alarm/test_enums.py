# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import enum

import pytest

from motor.common.alarm.enums import (
    Category,
    Cleared,
    ClearCategory,
    EventType,
    ScaleInReason,
    ServiceAffectedType,
    Severity,
)
from motor.common.alarm.cluster_connection_alarm import ClusterConnectionReason
from motor.common.alarm.coordinator_exception_alarm import CoordinatorExceptionReason
from motor.common.alarm.instance_exception_alarm import InstanceExceptionReason
from motor.common.alarm.master_to_slave_event import MasterToSlaveReason
from motor.common.alarm.req_congestion_event import RequestCongestionReason
from motor.common.alarm.scale_p2d_event import ScaleP2DReason
from motor.common.alarm.server_exception_event import ServerExceptionReason


ENUM_VALUE_CONTRACTS = (
    (
        Category,
        {
            "ALARM": 1,
            "CLEAR": 2,
            "EVENT": 3,
            "LEVEL_CHANGE": 4,
            "CONFIRM": 5,
            "UNCONFIRM": 6,
            "OTHER_CHANGE": 7,
        },
    ),
    (Cleared, {"NO": 0, "YES": 1}),
    (ClearCategory, {"AUTO": 1, "MANUAL": 2}),
    (Severity, {"CRITICAL": 1, "MAJOR": 2, "MINOR": 3, "WARNING": 4}),
    (
        EventType,
        {
            "COMMUNICATION": 1,
            "EQUIPMENT": 2,
            "PROCESSING_ERROR": 3,
            "QUALITY_OF_SERVICE": 4,
            "ENVIRONMENTAL": 5,
            "INTEGRITY_VIOLATION": 6,
            "OPERATIONAL": 7,
            "PHYSICAL_VIOLATION": 8,
            "SECURITY_VIOLATION": 9,
            "TIME_DOMAIN": 10,
            "PROPERTY_CHANGE": 11,
            "OBJECT_CREATION": 12,
            "OBJECT_DELETION": 13,
            "RELATION_CHANGE": 14,
            "STATE_CHANGE": 15,
            "ROUTE_CHANGE": 16,
            "PROTECTION_SWITCH": 17,
            "EXCEED_LIMIT": 18,
            "FILE_TRANSFER_STATUS": 19,
            "BACKUP_STATUS": 20,
            "HEARTBEAT": 21,
        },
    ),
    (ScaleInReason, {"INSTANCE_REDUCTION": 1}),
    (ServiceAffectedType, {"NO": 0, "YES": 1}),
)

REASON_VALUE_CONTRACTS = (
    (
        ClusterConnectionReason,
        {
            "REGISTER_FAILED": 1,
            "RANKTABLE_SUBSCRIBE_FAILED": 2,
            "FAULT_SUBSCRIBE_FAILED": 3,
            "CONNECTION_INTERRUPTED": 4,
        },
    ),
    (
        CoordinatorExceptionReason,
        {
            "INSTANCE_MISSING": 1,
            "COORDINATOR_EXCEPTION": 2,
        },
    ),
    (InstanceExceptionReason, {"INSTANCE_EXCEPTION": 1}),
    (MasterToSlaveReason, {"MASTER_COMPONENT_EXCEPTION": 1}),
    (RequestCongestionReason, {"DEALING_WITH_CONGESTION": 1}),
    (ScaleP2DReason, {"D_INSTANCE_RECOVERED_BY_SCALE_P2D": 1}),
    (
        ServerExceptionReason,
        {
            "HEARTBEAT_TIMEOUT": 1,
            "ENDPOINT_ABNORMAL": 2,
        },
    ),
)


@pytest.mark.parametrize(("enum_type", "expected_values"), ENUM_VALUE_CONTRACTS)
def test_enum_values_match_wire_contract(enum_type, expected_values):
    assert issubclass(enum_type, enum.IntEnum)
    assert {member.name: member.value for member in enum_type} == expected_values


@pytest.mark.parametrize(("enum_type", "expected_values"), REASON_VALUE_CONTRACTS)
def test_alarm_reason_enum_values_match_wire_contract(enum_type, expected_values):
    assert issubclass(enum_type, enum.IntEnum)
    assert {member.name: member.value for member in enum_type} == expected_values
