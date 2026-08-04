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

from motor.common.resources.dispatch import MOTOR_DISPATCH_KEY
from motor.common.resources.endpoint import Endpoint, EndpointStatus
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.router.dispatch_session import AttemptContext
from motor.coordinator.router.sglang_native_dispatch import (
    _bootstrap_port,
    _stable_bootstrap_room,
    ensure_sglang_pd_pair,
    inject_sglang_pd_fields,
    is_sglang_resource,
)


def _resource(engine_type: str | None, *, instance_id: int = 1) -> ScheduledResource:
    endpoint = Endpoint(
        id=instance_id,
        ip="10.0.0.8",
        business_port="8000",
        mgmt_port="1026",
        status=EndpointStatus.NORMAL,
    )
    instance = Instance(
        job_name=f"job-{instance_id}",
        model_name="m",
        engine_type=engine_type,
        id=instance_id,
        role=PDRole.ROLE_P if instance_id == 1 else PDRole.ROLE_D,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
        endpoints={endpoint.ip: {endpoint.id: endpoint}},
    )
    return ScheduledResource(instance=instance, endpoint=endpoint)


def test_stable_bootstrap_room_is_stable_and_positive():
    room_a = _stable_bootstrap_room("pair-1", 1)
    room_b = _stable_bootstrap_room("pair-1", 1)
    room_c = _stable_bootstrap_room("pair-1", 2)
    assert room_a == room_b
    assert room_a != room_c
    assert room_a >= 0
    assert room_a < (1 << 63)


def test_bootstrap_port(monkeypatch):
    monkeypatch.setenv("DISAGGREGATION_BOOTSTRAP_PORT", "8998")
    assert _bootstrap_port() == "8998"

    monkeypatch.delenv("DISAGGREGATION_BOOTSTRAP_PORT", raising=False)
    with pytest.raises(RuntimeError, match="must be an integer"):
        _bootstrap_port()

    monkeypatch.setenv("DISAGGREGATION_BOOTSTRAP_PORT", "abc")
    with pytest.raises(RuntimeError, match="must be an integer"):
        _bootstrap_port()

    monkeypatch.setenv("DISAGGREGATION_BOOTSTRAP_PORT", "70000")
    with pytest.raises(RuntimeError, match="1-65535"):
        _bootstrap_port()


def test_is_sglang_resource():
    sglang_p = _resource("sglang", instance_id=1)
    vllm_d = _resource("vllm", instance_id=2)
    assert is_sglang_resource(sglang_p) is True
    assert is_sglang_resource(vllm_d) is False
    assert is_sglang_resource(None) is False


def test_ensure_sglang_pd_pair_rejects_mixed_engines():
    attempt = AttemptContext(
        root_request_id="r1",
        attempt_seq=1,
        pair_id="pair",
        prefill_resource=_resource("sglang", instance_id=1),
        decode_resource=_resource("vllm", instance_id=2),
    )
    with pytest.raises(RuntimeError, match="both prefill and decode"):
        ensure_sglang_pd_pair(attempt)


def test_inject_sglang_pd_fields(monkeypatch):
    monkeypatch.setenv("DISAGGREGATION_BOOTSTRAP_PORT", "9100")
    attempt = AttemptContext(
        root_request_id="r1",
        attempt_seq=3,
        pair_id="pair-xyz",
        prefill_resource=_resource("sglang", instance_id=1),
        decode_resource=_resource("sglang", instance_id=2),
    )
    req = {"model": "m", "prompt": "hi", "request_id": "r1#a3"}
    assert inject_sglang_pd_fields(req, attempt) is None
    assert req["bootstrap_host"] == "10.0.0.8"
    assert req["bootstrap_port"] == "9100"
    assert req["bootstrap_room"] == _stable_bootstrap_room("pair-xyz", 3)
    assert MOTOR_DISPATCH_KEY not in req


def test_inject_sglang_pd_fields_requires_bootstrap_port(monkeypatch):
    monkeypatch.delenv("DISAGGREGATION_BOOTSTRAP_PORT", raising=False)
    attempt = AttemptContext(
        root_request_id="r1",
        attempt_seq=1,
        pair_id="pair",
        prefill_resource=_resource("sglang", instance_id=1),
        decode_resource=_resource("sglang", instance_id=2),
    )
    with pytest.raises(RuntimeError, match="DISAGGREGATION_BOOTSTRAP_PORT"):
        inject_sglang_pd_fields({"model": "m"}, attempt)


def test_inject_sglang_pd_fields_rejects_mixed_engines(monkeypatch):
    monkeypatch.setenv("DISAGGREGATION_BOOTSTRAP_PORT", "9100")
    attempt = AttemptContext(
        root_request_id="r1",
        attempt_seq=1,
        pair_id="pair",
        prefill_resource=_resource("sglang", instance_id=1),
        decode_resource=_resource("vllm", instance_id=2),
    )
    with pytest.raises(RuntimeError, match="both prefill and decode"):
        inject_sglang_pd_fields({"model": "m"}, attempt)
