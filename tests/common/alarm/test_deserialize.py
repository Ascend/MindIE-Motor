# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

from motor.common.alarm.deserialize import deserialize_incoming_record
from motor.common.alarm.enums import Category, Cleared
from motor.common.alarm.precision_issue_alarm import PRECISION_ISSUE_ALARM_ID


def test_deserialize_tolerates_unknown_enum_field_values() -> None:
    body = {
        "alarm_id": PRECISION_ISSUE_ALARM_ID,
        "category": Category.ALARM.value,
        "cleared": Cleared.NO.value,
        "severity": 99,
        "instance_id": "2",
        "p_instance_id": "1",
    }

    record = deserialize_incoming_record(body)

    assert record.alarm_id == PRECISION_ISSUE_ALARM_ID
    assert record.category == Category.ALARM
    assert record.severity == 99
