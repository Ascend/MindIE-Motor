# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest

from motor.common.alarm.coordinator_exception_alarm import COORDINATOR_EXCEPTION_ALARM_ID
from motor.common.alarm.enums import Category, Cleared
from motor.common.alarm.instance_exception_alarm import INSTANCE_EXCEPTION_ALARM_ID
from motor.common.alarm.precision_issue_alarm import PRECISION_ISSUE_ALARM_ID
from motor.common.alarm.record import Record
from motor.controller.observability.alarm.alarm_store import AlarmStore


@pytest.fixture()
def alarm_store(monkeypatch):
    # Reset ThreadSafeSingleton instance to avoid cross-test pollution.
    AlarmStore._instances.pop(AlarmStore, None)

    monkeypatch.setenv("NORTH_PLATFORM", "np1")
    monkeypatch.setenv("SERVICE_ID", "pod-1")

    store = AlarmStore()
    # Ensure clean internal state.
    store._alarms = {"np1": []}
    store._recoverable_alarms = {}
    store._active_precision_alarms = {}
    return store


def test_add_and_get_alarms_clears_after_fetch(alarm_store):
    record = Record(alarm_id="ALARM_X", alarm_name="x", additional_information="info")

    assert alarm_store.add_alarm(record) is True

    alarms = alarm_store.get_alarms("np1")
    assert len(alarms) == 1
    assert len(alarms[0]) == 1
    assert alarms[0][0]["alarmId"] == "ALARM_X"
    assert alarms[0][0]["alarmName"] == "x"
    assert alarms[0][0]["additionalInformation"].endswith("pod id=pod-1")

    # Fetching again should return nothing since alarms are cleared after read.
    assert alarm_store.get_alarms("np1") == []


def test_get_alarms_unknown_source_returns_empty_and_creates_bucket(alarm_store):
    assert alarm_store.get_alarms("unknown") == []
    assert "unknown" in alarm_store._alarms
    assert alarm_store._alarms["unknown"] == []


def test_non_exception_alarm_added_to_all_sources(alarm_store):
    alarm_store._alarms["np2"] = []
    record = Record(alarm_id="ALARM_Y", additional_information="info")

    assert alarm_store.add_alarm(record) is True

    assert len(alarm_store.get_alarms("np1")[0]) == 1
    assert len(alarm_store.get_alarms("np2")[0]) == 1


def test_instance_exception_alarm_deduplicates_until_cleared(alarm_store):
    alarm_key = f"{INSTANCE_EXCEPTION_ALARM_ID}_ins-1"

    record_no = Record(alarm_id=INSTANCE_EXCEPTION_ALARM_ID, instance_id="ins-1", cleared=Cleared.NO)
    assert alarm_store.add_alarm(record_no) is True
    assert alarm_key in alarm_store._recoverable_alarms
    assert len(alarm_store.get_alarms("np1")[0]) == 1

    # Adding the same "not cleared" alarm again should be ignored (already recoverable).
    assert alarm_store.add_alarm(record_no) is True
    assert alarm_store.get_alarms("np1") == []

    # When cleared, it should be emitted and removed from recoverable alarms.
    record_yes = Record(alarm_id=INSTANCE_EXCEPTION_ALARM_ID, instance_id="ins-1", cleared=Cleared.YES)
    assert alarm_store.add_alarm(record_yes) is True
    cleared_alarms = alarm_store.get_alarms("np1")
    assert len(cleared_alarms) == 1
    assert len(cleared_alarms[0]) == 1
    assert cleared_alarms[0][0]["alarmId"] == INSTANCE_EXCEPTION_ALARM_ID
    assert cleared_alarms[0][0]["cleared"] == Cleared.YES.value
    assert alarm_key not in alarm_store._recoverable_alarms


def test_coordinator_exception_alarm_uses_same_recovery_semantics(alarm_store):
    record_no = Record(alarm_id=COORDINATOR_EXCEPTION_ALARM_ID, instance_id="ins-2", cleared=Cleared.NO)
    assert alarm_store.add_alarm(record_no) is True
    assert len(alarm_store.get_alarms("np1")[0]) == 1

    # Duplicate NO should be suppressed.
    assert alarm_store.add_alarm(record_no) is True
    assert alarm_store.get_alarms("np1") == []


def test_precision_issue_alarm_tracks_active_until_cleared(alarm_store):
    moi = "service name=Coordinator, service ip=1.2.3.4, pId=1, instanceId=2"
    record_no = Record(
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        instance_id="2",
        p_instance_id="1",
        moi=moi,
        category=Category.ALARM,
        cleared=Cleared.NO,
    )
    assert alarm_store.add_alarm(record_no) is True
    assert moi in alarm_store._active_precision_alarms
    assert len(alarm_store.get_alarms("np1")[0]) == 1

    duplicate = Record(
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        instance_id="2",
        p_instance_id="1",
        moi=moi,
        category=Category.ALARM,
        cleared=Cleared.NO,
    )
    assert alarm_store.add_alarm(duplicate) is True
    assert alarm_store.get_alarms("np1") == []

    record_yes = Record(
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        instance_id="2",
        p_instance_id="1",
        moi=moi,
        category=Category.CLEAR,
        cleared=Cleared.YES,
    )
    assert alarm_store.add_alarm(record_yes) is True
    assert moi not in alarm_store._active_precision_alarms
    cleared = alarm_store.get_alarms("np1")
    assert len(cleared) == 1
    assert cleared[0][0]["cleared"] == Cleared.YES.value


def test_find_active_precision_alarm_matches_pd_group(alarm_store):
    moi = "service name=Coordinator, service ip=1.2.3.4, pId=1, instanceId=2"
    record_no = Record(
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        instance_id="2",
        p_instance_id="1",
        moi=moi,
        category=Category.ALARM,
        cleared=Cleared.NO,
    )
    alarm_store.add_alarm(record_no)
    found = alarm_store.find_active_precision_alarm(1, 2)
    assert found is record_no
    assert alarm_store.find_active_precision_alarm(2, 2) is None


def test_add_alarms_batches_raise_and_clear_in_one_fetch(alarm_store):
    moi = "service name=Coordinator, service ip=1.2.3.4, pId=1, instanceId=2"
    record_no = Record(
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        instance_id="2",
        p_instance_id="1",
        moi=moi,
        category=Category.ALARM,
        cleared=Cleared.NO,
    )
    record_yes = Record(
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        instance_id="2",
        p_instance_id="1",
        moi=moi,
        category=Category.CLEAR,
        cleared=Cleared.YES,
    )

    assert alarm_store.add_alarms([record_no, record_yes]) is True
    assert moi not in alarm_store._active_precision_alarms

    alarms = alarm_store.get_alarms("np1")
    assert len(alarms) == 1
    assert len(alarms[0]) == 2
    assert alarms[0][0]["cleared"] == Cleared.NO.value
    assert alarms[0][1]["cleared"] == Cleared.YES.value
