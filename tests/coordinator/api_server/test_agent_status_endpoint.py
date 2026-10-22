# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Endpoint tests for the out-of-band agent_status channel (Phase 4, WP-A).

Event payloads mirror the DSH statusSink JSONL (integration record §3.3):
``{"session": {"session_id"}, "agent_status": {...}}``. The server is built
with ``__new__`` + hand-set attributes so the tests exercise the real route
registration and handlers without scheduler/engine infrastructure.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_server.inference_server import InferenceServer
from motor.coordinator.domain.agent_session_status import AgentSessionStateMachine


def _event(session_id: str, state: str, **status_extra) -> dict:
    """Build a DSH-shaped event; omitted extras stay absent (DSH never sends
    ``tools`` on running/finalize events — see agent-hints status.ts).
    """
    agent_status = {"state": state, "state_since_ms": 1791604302656}
    agent_status.update({key: value for key, value in status_extra.items() if value is not None})
    return {"session": {"session_id": session_id}, "agent_status": agent_status}


class _FakeSchedulerConnection:
    def __init__(self) -> None:
        self.drop_session_pins_calls: list[str] = []
        self.client = SimpleNamespace(
            drop_session_pins=self._drop_session_pins,
            report_agent_status=AsyncMock(return_value=None),
        )

    def _drop_session_pins(self, session_id: str) -> int:
        self.drop_session_pins_calls.append(session_id)
        return 2

    def get_client(self):
        return self.client


def _build_server(enabled: bool = True) -> tuple[InferenceServer, _FakeSchedulerConnection]:
    server = InferenceServer.__new__(InferenceServer)
    server.coordinator_config = CoordinatorConfig()
    server._agent_status_machine = AgentSessionStateMachine() if enabled else None
    connection = _FakeSchedulerConnection()
    server._scheduler_connection = connection
    server._inference_app = FastAPI()
    if enabled:
        # Mirror the start-time gate in _register_routes.
        server._register_agent_status_routes()
    return server, connection


@pytest.fixture()
def client():
    server, connection = _build_server()
    with TestClient(server._inference_app) as test_client:
        yield test_client, connection


def test_tool_running_finalize_sequence(client):
    """T-A1: the integration-record §3.3 timeline lands applied + queryable."""
    test_client, _ = client
    for state in ("tool", "running", "finalize"):
        response = test_client.post(
            "/agent-status",
            json=_event(
                "session-a1952a88",
                state,
                reason="await_tool" if state == "tool" else None,
                tools=[{"name": "write", "call_id": "chatcmpl-tool"}] if state == "tool" else None,
            ),
        )
        assert response.status_code == 200
        assert response.json()["applied"] is True

    query = test_client.get("/agent-status", params={"session_id": "session-a1952a88"})
    assert query.status_code == 200
    record = query.json()
    assert record["state"] == "finalize"
    assert record["transition_count"] == 3


def test_finalize_triggers_pin_release(client):
    test_client, connection = client
    response = test_client.post("/agent-status", json=_event("s-pin", "finalize"))
    assert response.status_code == 200
    assert connection.drop_session_pins_calls == ["s-pin"]
    connection.client.report_agent_status.assert_awaited_once_with("s-pin", "finalize")


def test_structural_errors_answer_400(client):
    """Malformed structure is a client error (visible), never 5xx (T-A3)."""
    test_client, _ = client
    cases = [
        (),  # empty body
        (b"not-json",),
        (b'["not", "an", "object"]',),
        (b'{"agent_status": {"state": "running"}}',),  # missing session
        (b'{"session": {"session_id": "s-1"}}',),  # missing agent_status
        (b'{"session": {}, "agent_status": {"state": "running"}}',),  # missing session_id
        (b'{"session": {"session_id": ""}, "agent_status": {"state": "running"}}',),
    ]
    for case in cases:
        if not case:
            response = test_client.post("/agent-status")
        else:
            response = test_client.post("/agent-status", content=case[0])
        assert response.status_code == 400, f"case {case!r} -> {response.status_code}"


def test_non_utf8_body_answers_400_not_500(client):
    """Review (PR #1196): a body that is not valid UTF-8 raises UnicodeDecodeError
    before json.loads; it is a structural client error (400), never a 500.
    """
    test_client, _ = client
    response = test_client.post("/agent-status", content=b"\xff\xfe\xfa")
    assert response.status_code == 400


def test_field_level_invalid_state_answers_200_not_applied(client):
    """Field-level malformation: WARN + 200 (DSH retries only on non-2xx)."""
    test_client, _ = client
    response = test_client.post(
        "/agent-status",
        json={"session": {"session_id": "s-1"}, "agent_status": {"state": "exploded"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is False
    assert body["reason"] == "invalid_agent_status"
    assert test_client.get("/agent-status", params={"session_id": "s-1"}).status_code == 404


def test_unknown_session_creates_record(client):
    """T-A3: unknown session_id events create a record (Motor restart replay)."""
    test_client, _ = client
    response = test_client.post("/agent-status", json=_event("s-new", "running"))
    assert response.status_code == 200
    assert response.json()["previous_state"] is None
    assert test_client.get("/agent-status", params={"session_id": "s-new"}).json()["state"] == "running"


def test_duplicate_finalize_idempotent_and_terminal_guard(client):
    """T-A3: repeated finalize idempotent; post-finalize states rejected."""
    test_client, _ = client
    first = test_client.post("/agent-status", json=_event("s-1", "finalize"))
    assert first.json()["applied"] is True

    duplicate = test_client.post("/agent-status", json=_event("s-1", "finalize"))
    assert duplicate.status_code == 200
    assert duplicate.json()["applied"] is True
    assert duplicate.json()["reason"] == "idempotent"

    rejected = test_client.post("/agent-status", json=_event("s-1", "running"))
    assert rejected.status_code == 200
    assert rejected.json()["applied"] is False
    assert rejected.json()["reason"] == "terminal"
    assert test_client.get("/agent-status", params={"session_id": "s-1"}).json()["state"] == "finalize"


def test_get_requires_session_id(client):
    test_client, _ = client
    assert test_client.get("/agent-status").status_code == 400


def test_get_unknown_session_404(client):
    test_client, _ = client
    assert test_client.get("/agent-status", params={"session_id": "s-none"}).status_code == 404


def test_disabled_channel_has_no_routes():
    server, _ = _build_server(enabled=False)
    assert server._agent_status_machine is None
    paths = {route.path for route in server._inference_app.routes}
    assert "/agent-status" not in paths


def test_pause_suspend_accepted_degraded(client):
    """T-A2 degraded: pause/suspend migrate the machine, no 5xx (B1 pending)."""
    test_client, _ = client
    for state in ("pause", "suspend", "running"):
        response = test_client.post("/agent-status", json=_event("s-2", state, reason="human_approval"))
        assert response.status_code == 200
        assert response.json()["applied"] is True
