# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from datetime import datetime, timezone

from motor.common.alarm import record as record_module
from motor.common.alarm.enums import (
    Category,
    Cleared,
    ClearCategory,
    EventType,
    ServiceAffectedType,
    Severity,
)
from motor.common.alarm.record import Record


def test_record_defaults_when_environment_is_absent():
    record = Record()

    assert record.category is Category.ALARM
    assert record.cleared is Cleared.NO
    assert record.clear_category is ClearCategory.AUTO
    assert record.native_me_dn == "Unknown"
    assert record.origin_system == "Unknown"
    assert record.origin_system_name == "Unknown"
    assert record.origin_system_type == "Unknown"
    assert record.event_type is EventType.COMMUNICATION
    assert record.severity is Severity.CRITICAL
    assert record.service_affected_type is ServiceAffectedType.NO


def test_record_reads_identity_from_environment_at_creation(monkeypatch):
    monkeypatch.setenv("SERVICE_ID", "pod-7")
    monkeypatch.setenv("ENGINE_TYPE", "vllm")

    record = Record()

    assert record.native_me_dn == "pod-7"
    assert record.origin_system == "vllm"
    assert record.origin_system_name == "vllm"
    assert record.origin_system_type == "vllm"


def test_update_time_uses_both_time_sources(fixed_alarm_time):
    record = Record()

    record.update_time()

    assert record.occur_utc == fixed_alarm_time["occur_utc"]
    assert record.occur_time == fixed_alarm_time["occur_time"]


def test_utc_time_helper_converts_seconds_to_integer_milliseconds(monkeypatch):
    monkeypatch.setattr(record_module.time, "time", lambda: 123.4567)

    assert record_module.get_utc_time_in_millisec() == 123_456


def test_local_time_helper_uses_utc_epoch(monkeypatch):
    expected_datetime = datetime(2026, 7, 20, 8, 30, 15, 250000, tzinfo=timezone.utc)

    class FixedDateTime:
        @classmethod
        def now(cls, requested_timezone):
            assert requested_timezone is timezone.utc
            return expected_datetime

    monkeypatch.setattr(record_module, "datetime", FixedDateTime)

    assert record_module.get_local_time_in_millisec() == int(expected_datetime.timestamp() * 1000)


def test_format_maps_model_fields_to_external_contract(expected_format_keys):
    record = Record(
        category=Category.LEVEL_CHANGE,
        cleared=Cleared.YES,
        clear_category=ClearCategory.MANUAL,
        occur_utc=101,
        occur_time=202,
        native_me_dn="pod-a",
        origin_system="engine",
        origin_system_name="engine-name",
        origin_system_type="engine-type",
        location="rack-1",
        moi="controller-1",
        event_type=EventType.STATE_CHANGE,
        alarm_id="alarm-1",
        alarm_name="Alarm One",
        severity=Severity.WARNING,
        probable_cause="cause",
        reason_id=9,
        service_affected_type=ServiceAffectedType.YES,
        additional_information="details",
        instance_id="d-1",
        p_instance_id="p-1",
    )

    formatted = record.format()

    assert set(formatted) == expected_format_keys
    assert formatted == {
        "category": Category.LEVEL_CHANGE.value,
        "cleared": Cleared.YES.value,
        "clearCategory": ClearCategory.MANUAL.value,
        "occurUtc": 101,
        "occurTime": 202,
        "nativeMeDn": "pod-a",
        "originSystem": "engine",
        "originSystemName": "engine-name",
        "originSystemType": "engine-type",
        "location": "rack-1",
        "moi": "controller-1",
        "eventType": EventType.STATE_CHANGE.value,
        "alarmId": "alarm-1",
        "alarmName": "Alarm One",
        "severity": Severity.WARNING.value,
        "probableCause": "cause",
        "reasonId": 9,
        "serviceAffectedType": ServiceAffectedType.YES.value,
        "additionalInformation": "details, pod id=pod-a",
        "instanceId": "d-1",
        "pInstanceId": "p-1",
    }
