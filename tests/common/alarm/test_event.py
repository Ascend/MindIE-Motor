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

from motor.common.alarm.enums import Category, Cleared, EventType, ServiceAffectedType, Severity
from motor.common.alarm.event import Event
from motor.common.alarm.master_to_slave_event import (
    MasterToSlaveComponent,
    MasterToSlaveEvent,
    MasterToSlaveReason,
)
from motor.common.alarm.req_congestion_event import (
    ReqCongestionEvent,
    RequestCongestionReason,
)
from motor.common.alarm.scale_p2d_event import ScaleP2DEvent, ScaleP2DReason
from motor.common.alarm.server_exception_event import ServerExceptionEvent, ServerExceptionReason


def assert_event_defaults(event):
    assert isinstance(event, Event)
    assert event.category is Category.EVENT
    assert event.cleared is Cleared.YES


def assert_event_time(event, fixed_alarm_time):
    assert event.occur_utc == fixed_alarm_time["occur_utc"]
    assert event.occur_time == fixed_alarm_time["occur_time"]


def test_event_creation_and_formatted_properties(expected_format_keys):
    event = Event()

    formatted = event.format()

    assert_event_defaults(event)
    assert set(formatted) == expected_format_keys
    assert formatted["category"] == Category.EVENT.value
    assert formatted["cleared"] == Cleared.YES.value


@pytest.mark.parametrize(
    ("component", "expected_component_name", "expected_alarm_id", "expected_alarm_name", "expected_probable_cause"),
    [
        (
            MasterToSlaveComponent.CONTROLLER,
            "Controller",
            "0xFC001000",
            "Controller Master To Slave Alarm",
            "1:failures caused the original main controller to malfunction",
        ),
        (
            MasterToSlaveComponent.COORDINATOR,
            "Coordinator",
            "0xFC001008",
            "Coordinator Master To Slave Alarm",
            "1:failures caused the original main coordinator to malfunction",
        ),
    ],
)
def test_master_to_slave_event_uses_component_configuration(
    monkeypatch,
    fixed_alarm_time,
    component,
    expected_component_name,
    expected_alarm_id,
    expected_alarm_name,
    expected_probable_cause,
):
    monkeypatch.setenv("POD_IP", "203.0.113.5")

    event = MasterToSlaveEvent(component, MasterToSlaveReason.MASTER_COMPONENT_EXCEPTION)

    assert_event_defaults(event)
    assert event.alarm_id == expected_alarm_id
    assert event.alarm_name == expected_alarm_name
    assert event.probable_cause == expected_probable_cause
    assert event.reason_id == 1
    assert event.location == f"service name={expected_component_name}, service ip=203.0.113.5"
    assert event.moi == event.location
    assert event.additional_information == event.location
    assert event.severity is Severity.WARNING
    assert event.service_affected_type is ServiceAffectedType.NO
    assert_event_time(event, fixed_alarm_time)


def test_request_congestion_event_preserves_diagnostic_information(monkeypatch, fixed_alarm_time):
    monkeypatch.setenv("POD_IP", "203.0.113.6")

    event = ReqCongestionEvent(RequestCongestionReason.DEALING_WITH_CONGESTION, "queue depth=128")

    assert_event_defaults(event)
    assert event.reason_id == 1
    assert event.location == "service name=Coordinator, service ip=203.0.113.6"
    assert event.moi == event.location
    assert event.additional_information == "queue depth=128"
    assert event.alarm_id == "0xFC001005"
    assert_event_time(event, fixed_alarm_time)


def test_scale_p2d_event_describes_recovered_and_killed_instances(fixed_alarm_time):
    event = ScaleP2DEvent(
        ScaleP2DReason.D_INSTANCE_RECOVERED_BY_SCALE_P2D,
        d_instance_id=7,
        d_instance_job_name="decode-job",
        killed_p_instance_ids=[1, 3],
    )

    assert_event_defaults(event)
    assert event.reason_id == 1
    assert event.instance_id == "7"
    assert event.location == (
        "service name=Controller, d instance id=7, d instance job name=decode-job, killed p instance ids=[1,3]"
    )
    assert event.moi == event.location
    assert event.additional_information == event.location
    assert event.alarm_id == "0xFC001007"
    assert_event_time(event, fixed_alarm_time)


@pytest.mark.parametrize(
    ("reason", "expected_reason_id"),
    [
        (ServerExceptionReason.HEARTBEAT_TIMEOUT, 1),
        (ServerExceptionReason.ENDPOINT_ABNORMAL, 2),
    ],
)
def test_server_exception_event_serializes_endpoint_ids(fixed_alarm_time, reason, expected_reason_id):
    event = ServerExceptionEvent(reason, endpoint_ip="198.51.100.4", endpoint_ids=[2, "endpoint-b"])

    assert_event_defaults(event)
    assert event.reason_id == expected_reason_id
    assert event.location == ("service name=Controller, endpoint ip=198.51.100.4, endpoint ids=[2,endpoint-b]")
    assert event.moi == event.location
    assert event.additional_information == event.location
    assert event.alarm_id == "0xFC001003"
    assert event.event_type is EventType.STATE_CHANGE
    assert event.severity is Severity.MAJOR
    assert event.service_affected_type is ServiceAffectedType.YES
    assert_event_time(event, fixed_alarm_time)
