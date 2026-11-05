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

from motor.common.alarm.deserialize import deserialize_incoming_record
from motor.common.alarm.enums import Category, Cleared, EventType, ServiceAffectedType, Severity
from motor.common.alarm.precision_issue_alarm import (
    build_precision_issue_alarm,
    build_precision_issue_clear_alarm,
    build_precision_moi,
)


@pytest.mark.parametrize(
    ("environment", "model_id", "expected_native_me_dn"),
    [
        ({"SERVICE_ID": "  service-1  ", "sys_id": "system-1"}, "model-1", "service-1"),
        ({"SERVICE_ID": "   ", "sys_id": " system-1 "}, "model-1", "system-1"),
        ({}, " model-1 ", "model-1"),
    ],
)
def test_precision_alarm_identity_fallbacks(
    monkeypatch,
    fixed_alarm_time,
    environment,
    model_id,
    expected_native_me_dn,
):
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.setenv("POD_IP", "192.0.2.20")

    payload = build_precision_issue_alarm(
        p_instance_id=4,
        d_instance_id=9,
        precision_issue_count=6,
        probe_failure_count=2,
        model_id=model_id,
    )

    assert payload["native_me_dn"] == expected_native_me_dn
    assert payload["category"] == Category.ALARM.value
    assert payload["cleared"] == Cleared.NO.value
    assert payload["event_type"] == EventType.PROCESSING_ERROR.value
    assert payload["alarm_id"] == "0xFC001009"
    assert payload["severity"] == Severity.MAJOR.value
    assert payload["service_affected_type"] == ServiceAffectedType.YES.value
    assert payload["location"] == "service name=Coordinator, service ip=192.0.2.20"
    assert payload["moi"] == "service name=Coordinator, service ip=192.0.2.20, pId=4, instanceId=9"
    assert payload["instance_id"] == "9"
    assert payload["p_instance_id"] == "4"
    assert payload["occur_utc"] == fixed_alarm_time["occur_utc"]
    assert payload["occur_time"] == fixed_alarm_time["occur_time"]
    assert payload["additional_information"] == (
        "precision_issue_count=6, probe_failure_count=2, p_instance_id=4, d_instance_id=9"
    )


def test_precision_alarm_supports_missing_prefill_instance(monkeypatch, fixed_alarm_time):
    monkeypatch.setenv("SERVICE_ID", "prefill-test-service")
    monkeypatch.setenv("POD_IP", "192.0.2.20")
    payload = build_precision_issue_alarm(
        p_instance_id=None,
        d_instance_id=3,
        precision_issue_count=1,
        probe_failure_count=0,
    )

    expected_information = "precision_issue_count=1, probe_failure_count=0, p_instance_id=, d_instance_id=3"
    assert payload["p_instance_id"] == ""
    assert payload["instance_id"] == "3"
    assert payload["moi"] == "service name=Coordinator, service ip=192.0.2.20, instanceId=3"
    assert payload["additional_information"] == expected_information
    northbound = deserialize_incoming_record(payload).format()
    assert northbound["pInstanceId"] == ""
    assert northbound["additionalInformation"] == (
        "precision_issue_count=1, probe_failure_count=0, p_instance_id=, d_instance_id=3, pod id=prefill-test-service"
    )
    assert payload["occur_utc"] == fixed_alarm_time["occur_utc"]
    assert payload["occur_time"] == fixed_alarm_time["occur_time"]


def test_build_precision_moi_prefers_explicit_pod_ip(monkeypatch):
    monkeypatch.setenv("POD_IP", "198.51.100.20")

    moi = build_precision_moi(p_instance_id=4, d_instance_id=9, pod_ip="192.0.2.20")

    assert moi == "service name=Coordinator, service ip=192.0.2.20, pId=4, instanceId=9"


def test_build_precision_moi_caches_environment_ip(monkeypatch):
    monkeypatch.setenv("POD_IP", "192.0.2.20")

    first_moi = build_precision_moi(p_instance_id=4, d_instance_id=9)
    monkeypatch.setenv("POD_IP", "198.51.100.20")
    second_moi = build_precision_moi(p_instance_id=4, d_instance_id=9)

    assert first_moi == "service name=Coordinator, service ip=192.0.2.20, pId=4, instanceId=9"
    assert second_moi == first_moi


def test_build_precision_moi_uses_environment_without_prefill_instance(monkeypatch):
    monkeypatch.setenv("POD_IP", "203.0.113.20")

    moi = build_precision_moi(p_instance_id=None, d_instance_id=9)

    assert moi == "service name=Coordinator, service ip=203.0.113.20, instanceId=9"


@pytest.mark.parametrize("p_instance_id", [4, None])
def test_precision_alarm_clear_reuses_raise_moi_after_pod_ip_changes(monkeypatch, p_instance_id):
    monkeypatch.setenv("POD_IP", "192.0.2.20")
    raised = build_precision_issue_alarm(
        p_instance_id=p_instance_id,
        d_instance_id=9,
        precision_issue_count=6,
        probe_failure_count=2,
    )

    monkeypatch.setenv("POD_IP", "198.51.100.20")
    cleared = build_precision_issue_clear_alarm(
        p_instance_id=p_instance_id,
        d_instance_id=9,
        alarm_moi=raised["moi"],
    )

    assert raised["moi"] == (
        "service name=Coordinator, service ip=192.0.2.20, pId=4, instanceId=9"
        if p_instance_id is not None
        else "service name=Coordinator, service ip=192.0.2.20, instanceId=9"
    )
    assert cleared["moi"] == raised["moi"]
    assert cleared["location"] == "service name=Coordinator, service ip=198.51.100.20"
    assert cleared["category"] == Category.CLEAR.value
    assert cleared["cleared"] == Cleared.YES.value
    p_instance_text = "" if p_instance_id is None else "4"
    assert raised["additional_information"] == (
        f"precision_issue_count=6, probe_failure_count=2, p_instance_id={p_instance_text}, d_instance_id=9"
    )
    assert cleared["additional_information"] == (
        f"precision_issue_count=0, probe_failure_count=0, p_instance_id={p_instance_text}, d_instance_id=9"
    )
