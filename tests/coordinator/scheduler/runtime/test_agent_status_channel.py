# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Unit tests for the agent_status out-of-band channel (Phase 4, WP-A T-A4).

Covers the worker-side pin release path (``_on_agent_status_event`` /
``drop_session_pins``), the DEALER relay (``report_agent_status``) and the
Mgmt relay/fan-out (``_handle_agent_status_notify`` / ``_publish_agent_status``)
without real sockets: instances are built with ``__new__`` and fake attributes,
following the focused-test pattern of the neighboring circuit-breaker tests.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient
from motor.coordinator.scheduler.runtime.scheduler_server import (
    _SchedulerRequestDispatcher,
)
from motor.coordinator.scheduler.runtime.session_affinity import SessionAffinityTable
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    AGENT_STATUS_TOPIC,
    SchedulerRequest,
    SchedulerRequestType,
    SchedulerResponseType,
)


def _client_with_affinity(table: SessionAffinityTable | None) -> AsyncSchedulerClient:
    client = AsyncSchedulerClient.__new__(AsyncSchedulerClient)
    client._session_affinity = table
    client._transport = SimpleNamespace(connected=True, send_request=AsyncMock(return_value=None))
    return client


@pytest.mark.asyncio
async def test_on_agent_status_finalize_drops_all_role_pins():
    table = SessionAffinityTable()
    table.record("s-1", 1, "union")
    table.record("s-1", 2, "prefill")
    table.record("s-2", 3, "union")
    client = _client_with_affinity(table)

    await client._on_agent_status_event("s-1", "finalize")

    assert table.get("s-1", "union") is None
    assert table.get("s-1", "prefill") is None
    assert table.get("s-2", "union").instance_id == 3


@pytest.mark.asyncio
async def test_on_agent_status_non_finalize_keeps_pins():
    table = SessionAffinityTable()
    table.record("s-1", 1, "union")
    client = _client_with_affinity(table)

    await client._on_agent_status_event("s-1", "tool")
    await client._on_agent_status_event("s-1", "pause")

    assert table.get("s-1", "union").instance_id == 1


@pytest.mark.asyncio
async def test_on_agent_status_degenerate_inputs_safe():
    client = _client_with_affinity(SessionAffinityTable())
    await client._on_agent_status_event("", "finalize")  # no raise
    await client._on_agent_status_event("s-1", "")  # no raise


@pytest.mark.asyncio
async def test_on_agent_status_without_affinity_table_safe():
    client = _client_with_affinity(None)
    await client._on_agent_status_event("s-1", "finalize")  # no raise


def test_drop_session_pins_local_table():
    table = SessionAffinityTable()
    table.record("s-1", 1, "union")
    client = _client_with_affinity(table)
    assert client.drop_session_pins("s-1") == 1
    assert client.drop_session_pins("s-1") == 0


def test_drop_session_pins_without_affinity_returns_zero():
    client = _client_with_affinity(None)
    assert client.drop_session_pins("s-1") == 0


@pytest.mark.asyncio
async def test_report_agent_status_sends_notify_request():
    client = _client_with_affinity(None)
    await client.report_agent_status("s-1", "finalize")
    # Fire-and-forget: yield to the loop so the created task runs.
    for _ in range(3):
        await asyncio.sleep(0)
    client._transport.send_request.assert_awaited_once()
    request = client._transport.send_request.await_args.args[0]
    assert isinstance(request, SchedulerRequest)
    assert request.request_type == SchedulerRequestType.AGENT_STATUS_NOTIFY
    assert request.data == {"session_id": "s-1", "state": "finalize"}


@pytest.mark.asyncio
async def test_report_agent_status_dropped_when_disconnected():
    client = _client_with_affinity(None)
    client._transport = SimpleNamespace(connected=False, send_request=AsyncMock(return_value=None))
    await client.report_agent_status("s-1", "finalize")  # warn + drop, no raise
    client._transport.send_request.assert_not_awaited()


def _dispatcher_with_pub_socket(pub_socket) -> _SchedulerRequestDispatcher:
    dispatcher = _SchedulerRequestDispatcher.__new__(_SchedulerRequestDispatcher)
    dispatcher._pub_socket = pub_socket
    return dispatcher


def _request(session_id, state) -> SchedulerRequest:
    return SchedulerRequest(
        request_type=SchedulerRequestType.AGENT_STATUS_NOTIFY,
        request_id="req-1",
        data={"session_id": session_id, "state": state},
    )


@pytest.mark.asyncio
async def test_dispatcher_finalize_publishes_on_agent_status_topic():
    pub = MagicMock()
    pub.send_multipart = AsyncMock(return_value=None)
    dispatcher = _dispatcher_with_pub_socket(pub)

    response = await dispatcher._handle_agent_status_notify(_request("s-1", "finalize"))

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data == {"relayed": True}
    pub.send_multipart.assert_awaited_once()
    topic, payload = pub.send_multipart.await_args.args[0]
    assert topic == AGENT_STATUS_TOPIC
    import msgspec

    assert msgspec.msgpack.decode(payload) == {"session_id": "s-1", "state": "finalize"}


@pytest.mark.asyncio
async def test_dispatcher_observability_states_do_not_fan_out():
    pub = MagicMock()
    pub.send_multipart = AsyncMock(return_value=None)
    dispatcher = _dispatcher_with_pub_socket(pub)

    response = await dispatcher._handle_agent_status_notify(_request("s-1", "tool"))

    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data == {"relayed": False, "reason": "observability_only"}
    pub.send_multipart.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatcher_invalid_fields_answer_success_not_error():
    """T-A4 never fails a request: bad notify payloads are lenient no-ops."""
    pub = MagicMock()
    pub.send_multipart = AsyncMock(return_value=None)
    dispatcher = _dispatcher_with_pub_socket(pub)

    for data in ({}, {"session_id": "", "state": "finalize"}, {"session_id": "s-1", "state": None}):
        request = SchedulerRequest(
            request_type=SchedulerRequestType.AGENT_STATUS_NOTIFY,
            request_id="req-1",
            data=data,
        )
        response = await dispatcher._handle_agent_status_notify(request)
        assert response.response_type == SchedulerResponseType.SUCCESS
        assert response.data["relayed"] is False
    pub.send_multipart.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatcher_without_pub_socket_is_safe():
    dispatcher = _dispatcher_with_pub_socket(None)
    response = await dispatcher._handle_agent_status_notify(_request("s-1", "finalize"))
    assert response.response_type == SchedulerResponseType.SUCCESS
    assert response.data == {"relayed": True}


@pytest.mark.asyncio
async def test_dispatcher_publish_error_never_raises():
    pub = MagicMock()
    pub.send_multipart = AsyncMock(side_effect=RuntimeError("zmq down"))
    dispatcher = _dispatcher_with_pub_socket(pub)
    response = await dispatcher._handle_agent_status_notify(_request("s-1", "finalize"))
    assert response.response_type == SchedulerResponseType.SUCCESS


@pytest.mark.asyncio
async def test_pub_events_drive_local_state_machine():
    """Review 194528015: PUB fan-out must drive the worker's own state machine,
    so finalize's terminal semantics (idempotent finalize, no re-entry into
    running) hold on every worker — not only the one that received the POST.
    """
    from motor.coordinator.domain.agent_session_status import AgentSessionStateMachine

    table = SessionAffinityTable()
    table.record("s-9", 1, "union")
    machine = AgentSessionStateMachine(session_ttl_s=600, max_sessions=64)
    client = _client_with_affinity(table)
    client.agent_status_state_listener = machine.apply

    await client._on_agent_status_event("s-9", "finalize")
    record = machine.get("s-9")
    assert record is not None and record.state == "finalize"

    # A late/stray running event on this worker must NOT resurrect the session.
    await client._on_agent_status_event("s-9", "running")
    record = machine.get("s-9")
    assert record.state == "finalize"
    assert table.get("s-9", "union") is None


@pytest.mark.asyncio
async def test_pub_state_listener_failure_never_breaks_pin_release():
    """The listener is best-effort: a broken listener must not skip the
    finalize pin release.
    """
    table = SessionAffinityTable()
    table.record("s-9", 1, "union")
    client = _client_with_affinity(table)

    def broken(session_id, state):
        raise RuntimeError("boom")

    client.agent_status_state_listener = broken
    await client._on_agent_status_event("s-9", "finalize")
    assert table.get("s-9", "union") is None
