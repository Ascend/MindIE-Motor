# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest

from motor.common.alarm.alarm import Alarm
from motor.common.alarm.cluster_connection_alarm import (
    ClusterConnectionAlarm,
    ClusterConnectionReason,
)
from motor.common.alarm.coordinator_exception_alarm import (
    CoordinatorExceptionAlarm,
    CoordinatorExceptionReason,
)
from motor.common.alarm.enums import Category, Cleared, EventType, ServiceAffectedType, Severity
from motor.common.alarm.instance_exception_alarm import (
    InstanceExceptionAlarm,
    InstanceExceptionReason,
)
from motor.common.alarm.service_level_degradation import ServiceLevelDegradation


def assert_alarm_time(record, fixed_alarm_time):
    assert record.occur_utc == fixed_alarm_time["occur_utc"]
    assert record.occur_time == fixed_alarm_time["occur_time"]


def test_alarm_creation_clear_and_formatted_query(expected_format_keys):
    alarm = Alarm()

    assert alarm.category is Category.ALARM
    assert alarm.cleared is Cleared.NO

    alarm.clear()
    formatted = alarm.format()

    assert alarm.category is Category.CLEAR
    assert alarm.cleared is Cleared.YES
    assert set(formatted) == expected_format_keys
    assert formatted["category"] == Category.CLEAR.value
    assert formatted["cleared"] == Cleared.YES.value


@pytest.mark.parametrize("is_clear", [False, True])
def test_cluster_connection_alarm_builds_location_and_clear_state(monkeypatch, fixed_alarm_time, is_clear):
    monkeypatch.setenv("POD_IP", "10.0.0.7")
    monkeypatch.setenv("MINDX_SERVER_IP", "10.0.0.8")

    alarm = ClusterConnectionAlarm(ClusterConnectionReason.CONNECTION_INTERRUPTED, is_clear=is_clear)

    assert alarm.reason_id == 4
    assert alarm.alarm_id == "0xFC001006"
    assert alarm.event_type is EventType.STATE_CHANGE
    assert alarm.severity is Severity.CRITICAL
    assert alarm.service_affected_type is ServiceAffectedType.YES
    assert alarm.location == "service name=Controller, service ip=10.0.0.7, cluster ip=10.0.0.8"
    assert alarm.moi == alarm.location
    assert alarm.additional_information == alarm.location
    assert alarm.cleared is (Cleared.YES if is_clear else Cleared.NO)
    assert alarm.category is (Category.CLEAR if is_clear else Category.ALARM)
    assert_alarm_time(alarm, fixed_alarm_time)


def test_cluster_connection_alarm_omits_empty_cluster_ip(fixed_alarm_time):
    alarm = ClusterConnectionAlarm(ClusterConnectionReason.REGISTER_FAILED)

    assert alarm.location == "service name=Controller, service ip="
    assert_alarm_time(alarm, fixed_alarm_time)


@pytest.mark.parametrize("is_cleared", [False, True])
def test_coordinator_exception_alarm_builds_payload(monkeypatch, fixed_alarm_time, is_cleared):
    monkeypatch.setenv("POD_IP", "192.0.2.10")

    alarm = CoordinatorExceptionAlarm(CoordinatorExceptionReason.INSTANCE_MISSING, is_cleared=is_cleared)

    assert alarm.alarm_id == "0xFC001004"
    assert alarm.reason_id == 1
    assert alarm.location == "service name=Coordinator, service ip=192.0.2.10"
    assert alarm.moi == alarm.location
    assert alarm.additional_information == alarm.location
    assert alarm.cleared is (Cleared.YES if is_cleared else Cleared.NO)
    assert_alarm_time(alarm, fixed_alarm_time)


@pytest.mark.parametrize("is_cleared", [False, True])
def test_instance_exception_alarm_tracks_instance(monkeypatch, fixed_alarm_time, is_cleared):
    monkeypatch.setenv("POD_IP", "192.0.2.11")

    alarm = InstanceExceptionAlarm(
        instance_id="decode-3",
        reason_id=InstanceExceptionReason.INSTANCE_EXCEPTION,
        is_cleared=is_cleared,
    )

    assert alarm.alarm_id == "0xFC001002"
    assert alarm.instance_id == "decode-3"
    assert alarm.reason_id == 1
    assert "inst_id=decode-3" in alarm.location
    assert "service ip=192.0.2.11" in alarm.location
    assert alarm.moi == alarm.location
    assert alarm.additional_information == alarm.location
    assert alarm.cleared is (Cleared.YES if is_cleared else Cleared.NO)
    assert_alarm_time(alarm, fixed_alarm_time)


def test_service_level_degradation_defaults():
    alarm = ServiceLevelDegradation()

    assert alarm.category is Category.LEVEL_CHANGE
    assert alarm.cleared is Cleared.NO
    assert alarm.event_type is EventType.STATE_CHANGE
    assert alarm.alarm_id == "0xFC001001"
    assert alarm.severity is Severity.MAJOR
    assert alarm.reason_id == 0
    assert alarm.service_affected_type is ServiceAffectedType.YES
