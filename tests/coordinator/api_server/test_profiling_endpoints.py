# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""HTTP tests for the Coordinator observability-plane profiling API."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import Instance
from motor.config.coordinator import CoordinatorConfig, MGMT_API_KEY_HEADER
from motor.coordinator.api_server.observability_server import ObservabilityServer
from motor.coordinator.profiling.models import ProfilingStartRequest
from motor.coordinator.profiling.service import ProfilingService


class _FakeInstanceProvider:
    def __init__(self, instances: dict[int, Instance]) -> None:
        self._instances = instances

    async def get_all_instances(self) -> tuple[dict[int, Instance], dict[int, Instance]]:
        return self._instances, {}


class _EngineRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, action, address, tls_config, timeout, body) -> None:
        self.calls.append((action.value, address))


def _instance(instance_id: int = 0, role: str = "prefill", dp_ranks: tuple[int, ...] = (0, 1)) -> Instance:
    pod_ip = f"10.0.0.{instance_id + 1}"
    endpoints = {rank: Endpoint(id=rank, ip=pod_ip, business_port=str(8000 + rank)) for rank in dp_ranks}
    return Instance(
        job_name=f"inst-{instance_id}",
        model_name="deepseek-v3",
        id=instance_id,
        role=role,
        endpoints={pod_ip: endpoints},
    )


def _build_server(enable: bool = True) -> tuple[ObservabilityServer, _EngineRecorder]:
    config = CoordinatorConfig()
    config.profiling_config.enable = enable
    server = ObservabilityServer.__new__(ObservabilityServer)
    server.coordinator_config = config
    server._central_lifecycle = None
    server._mgmt_api_key_config = config.mgmt_api_key_config
    server._mgmt_api_key = ""
    server._instance_provider = _FakeInstanceProvider({0: _instance()})
    engine = _EngineRecorder()
    server._profiling_service = ProfilingService(
        config=config.profiling_config,
        tls_config=None,
        instances_provider=server._get_profiling_instances,
        engine_call=engine,
    )
    server.observability_app = FastAPI()
    server._register_routes()
    return server, engine


@pytest.fixture()
def client():
    server, engine = _build_server()
    with TestClient(server.observability_app) as test_client:
        yield test_client, server, engine


def test_profiling_api_is_closed_until_enabled():
    server, _ = _build_server(enable=False)
    with TestClient(server.observability_app) as test_client:
        response = test_client.post("/profiling/start", json={})

    assert response.status_code == 404
    assert "profiling_config.enable" in response.json()["detail"]


def test_start_returns_metrics_dimensioned_targets(client):
    test_client, _, engine = client

    response = test_client.post("/profiling/start", json={"roles": ["prefill"], "dps": [0]})

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "start"
    assert "profiling_id" not in body
    assert body["service"] == "deepseek-v3"
    assert body["succeeded"] == 1
    assert body["targets"] == [
        {
            "service": "deepseek-v3",
            "instance_id": 0,
            "role": "prefill",
            "dp_rank": 0,
            "pod_ip": "10.0.0.1",
            "address": "10.0.0.1:8000",
            "status": "started",
            "detail": "",
            "profile_prefix": "motor_profile_svc-deepseek-v3_ins-0_role-prefill_dp-0",
        }
    ]
    assert engine.calls == [("start", "10.0.0.1:8000")]


def test_start_rejects_unknown_roles(client):
    test_client, _, engine = client

    response = test_client.post("/profiling/start", json={"roles": ["nope"]})

    assert response.status_code == 400
    assert engine.calls == []


def test_start_rejects_an_empty_body(client):
    test_client, _, _ = client

    response = test_client.post("/profiling/start")

    assert response.status_code == 400


def test_start_rejects_an_oversized_body(client):
    test_client, _, engine = client

    response = test_client.post("/profiling/start", json={"profiler_options": {"blob": "x" * (1024 * 1024 + 1)}})

    assert response.status_code == 413
    assert engine.calls == []


async def test_profiling_parser_stops_reading_when_body_exceeds_limit() -> None:
    server, _ = _build_server()
    chunks = iter((b" " * (1024 * 1024), b" ", b"unread"))
    read_count = 0

    async def receive() -> dict:
        nonlocal read_count
        read_count += 1
        return {"type": "http.request", "body": next(chunks), "more_body": read_count < 3}

    request = Request({"type": "http"}, receive)
    with pytest.raises(HTTPException) as error:
        await server._parse_profiling_request(request, ProfilingStartRequest)

    assert error.value.status_code == 413
    assert read_count == 2
    assert next(chunks) == b"unread"


def test_stop_uses_same_selector_as_start(client):
    test_client, _, engine = client
    selector = {"roles": ["prefill"], "dps": [1]}
    start = test_client.post("/profiling/start", json=selector)
    stop = test_client.post("/profiling/stop", json=selector)

    assert start.status_code == stop.status_code == 200
    assert stop.json()["action"] == "stop"
    assert stop.json()["succeeded"] == 1
    assert engine.calls == [("start", "10.0.0.1:8001"), ("stop", "10.0.0.1:8001")]


def test_profiling_status_route_has_been_removed(client):
    test_client, _, _ = client
    assert test_client.get("/profiling/status").status_code == 404


def test_standby_coordinator_rejects_profiling(client, monkeypatch):
    test_client, server, _ = client
    server.coordinator_config.standby_config.enable_master_standby = True
    monkeypatch.setattr(
        "motor.coordinator.api_server.observability_server.is_master_from_role_shm",
        lambda *args, **kwargs: False,
    )

    response = test_client.post("/profiling/start", json={})

    assert response.status_code == 503
    assert "not master" in response.json()["detail"]


def test_management_api_key_gates_profiling(client):
    test_client, server, _ = client
    server._mgmt_api_key_config = SimpleNamespace(enable_api_key=True)
    server._mgmt_api_key = "secret"

    unauthorized = test_client.post("/profiling/start", json={})
    authorized = test_client.post(
        "/profiling/start",
        json={},
        headers={MGMT_API_KEY_HEADER: "secret"},
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_start_rejects_misspelled_selector_keys(client):
    test_client, _, engine = client

    response = test_client.post("/profiling/start", json={"role": ["prefill"], "dp": [0]})

    assert response.status_code == 400
    assert "Extra inputs are not permitted" in response.json()["detail"]
    assert engine.calls == []


def test_stop_rejects_removed_profiling_id(client):
    test_client, _, engine = client

    response = test_client.post("/profiling/stop", json={"profiling_id": "prof-missing"})

    assert response.status_code == 400
    assert "Extra inputs are not permitted" in response.json()["detail"]
    assert engine.calls == []
