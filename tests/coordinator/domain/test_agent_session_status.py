# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Unit tests for the out-of-band agent session state machine (Phase 4)."""

import time

from motor.coordinator.domain.agent_session_status import (
    AgentSessionRecord,
    AgentSessionStateMachine,
)

import pytest


def test_apply_new_session_creates_record():
    machine = AgentSessionStateMachine()
    transition = machine.apply("s-1", "tool", reason="await_tool", tools=[{"name": "write", "call_id": "c1"}])

    assert transition.applied is True
    assert transition.reason == "applied"
    assert transition.previous_state is None
    record = machine.get("s-1")
    assert record is not None
    assert record.state == "tool"
    assert record.reason == "await_tool"
    assert record.tools == [{"name": "write", "call_id": "c1"}]
    assert record.transition_count == 1


def test_out_of_order_transitions_tolerated():
    """Any state may follow any state (running before tool, arbitrary jumps)."""
    machine = AgentSessionStateMachine()
    assert machine.apply("s-1", "running").applied is True
    assert machine.apply("s-1", "tool").applied is True
    assert machine.apply("s-1", "pause").applied is True
    assert machine.apply("s-1", "running").applied is True
    assert machine.get("s-1").state == "running"
    assert machine.get("s-1").transition_count == 4


def test_finalize_is_terminal_and_idempotent():
    machine = AgentSessionStateMachine()
    assert machine.apply("s-1", "tool").applied is True
    assert machine.apply("s-1", "finalize").applied is True
    assert machine.get("s-1").state == "finalize"

    # Duplicate finalize: idempotent, still applied (T-A3).
    duplicate = machine.apply("s-1", "finalize")
    assert duplicate.applied is True
    assert duplicate.reason == "idempotent"
    assert machine.get("s-1").transition_count == 2

    # Any other state after finalize: not applied (T-A3).
    rejected = machine.apply("s-1", "running")
    assert rejected.applied is False
    assert rejected.reason == "terminal"
    assert rejected.previous_state == "finalize"
    assert machine.get("s-1").state == "finalize"
    assert machine.get("s-1").transition_count == 2


def test_invalid_inputs_rejected_without_raising():
    machine = AgentSessionStateMachine()
    assert machine.apply("", "running").applied is False
    assert machine.apply("", "running").reason == "invalid_session"
    assert machine.apply("s-1", "").applied is False
    assert machine.apply("s-1", "").reason == "invalid_state"
    assert machine.apply(None, "running").applied is False
    assert machine.apply("s-1", None).applied is False
    assert len(machine) == 0


def test_get_unknown_or_invalid_session_returns_none():
    machine = AgentSessionStateMachine()
    machine.apply("s-1", "running")
    assert machine.get("s-missing") is None
    assert machine.get("") is None
    assert machine.get(None) is None


def test_ttl_expiry_uses_server_receipt_clock():
    machine = AgentSessionStateMachine(session_ttl_s=1)
    machine.apply("s-old", "running")
    # Age the record past the TTL (server clock, not state_since_ms).
    machine._records["s-old"].updated_at_ms = int(time.time() * 1000) - 10_000
    # A state_since_ms far in the future must NOT keep the record alive.
    machine.apply("s-fresh", "running", state_since_ms=int(time.time() * 1000) + 3_600_000)
    machine._records["s-fresh"].updated_at_ms = int(time.time() * 1000) - 10_000

    assert machine.get("s-old") is None
    assert machine.get("s-fresh") is None
    assert len(machine) == 0


def test_expiry_is_incremental_and_does_not_scan_live_table():
    """Review 194909359: expiry walks the oldest-first index and stops at the
    first live record instead of scanning every record on each access.
    """
    machine = AgentSessionStateMachine(session_ttl_s=1)
    for i in range(2000):
        machine.apply(f"live-{i}", "running")
    # Age one early record past the TTL; the rest of the table stays live.
    machine._records["live-0"].updated_at_ms = int(time.time() * 1000) - 10_000

    assert machine.get("live-5") is not None
    assert machine.get("live-0") is None
    assert len(machine) == 1999
    assert len(machine._expiry_order) == len(machine._records)


def test_expiry_index_tracks_updates_and_lru_evictions():
    """The oldest-first expiry index stays in sync with record updates/eviction."""
    machine = AgentSessionStateMachine(max_sessions=3)
    for sid in ("s-1", "s-2", "s-3"):
        machine.apply(sid, "running")
    machine.apply("s-1", "tool")  # refresh: moves to the newest TTL end
    machine.apply("s-4", "running")  # evicts the LRU record (s-2)

    assert len(machine._records) == len(machine._expiry_order) == 3
    assert machine.get("s-2") is None
    assert machine.get("s-1") is not None


def test_lru_bound_eviction():
    machine = AgentSessionStateMachine(max_sessions=2)
    machine.apply("s-1", "running")
    machine.apply("s-2", "running")
    machine.apply("s-3", "running")
    assert len(machine) == 2
    assert machine.get("s-1") is None  # oldest evicted
    assert machine.get("s-2") is not None
    assert machine.get("s-3") is not None


def test_lru_recency_protected_by_get():
    machine = AgentSessionStateMachine(max_sessions=2)
    machine.apply("s-1", "running")
    machine.apply("s-2", "running")
    machine.get("s-1")  # refresh recency
    machine.apply("s-3", "running")
    assert machine.get("s-1") is not None  # refreshed, survives
    assert machine.get("s-2") is None  # now the LRU victim


def test_update_limits_hot_reload():
    machine = AgentSessionStateMachine(session_ttl_s=3600, max_sessions=10)
    machine.apply("s-1", "running")
    machine._records["s-1"].updated_at_ms = int(time.time() * 1000) - 100_000

    machine.update_limits(session_ttl_s=10, max_sessions=10)
    assert machine.get("s-1") is None  # expired under the new TTL

    machine.update_limits(session_ttl_s=3600, max_sessions=1)
    machine.apply("s-2", "running")
    machine.apply("s-3", "running")
    assert len(machine) == 1  # capacity shrunk, LRU evicted


def test_record_to_dict_shape():
    record = AgentSessionRecord(
        state="tool",
        reason="await_tool",
        tools=[{"name": "write"}],
        state_since_ms=1790000000000,
        updated_at_ms=1790000001000,
        transition_count=3,
    )
    payload = record.to_dict()
    assert payload == {
        "state": "tool",
        "reason": "await_tool",
        "tools": [{"name": "write"}],
        "state_since_ms": 1790000000000,
        "updated_at_ms": 1790000001000,
        "transition_count": 3,
    }


def test_transition_to_dict_shape():
    machine = AgentSessionStateMachine()
    transition = machine.apply("s-1", "running")
    payload = transition.to_dict()
    assert payload["applied"] is True
    assert payload["session_id"] == "s-1"
    assert payload["previous_state"] is None


@pytest.mark.parametrize("state", ["running", "tool", "pause", "suspend", "finalize"])
def test_spec_whitelist_states_accepted(state):
    """The machine accepts any non-empty state; whitelist enforcement is the
    endpoint's job (AgentStatusHint). Spec states must all round-trip.
    """
    machine = AgentSessionStateMachine()
    assert machine.apply("s-1", state).applied is True
    assert machine.get("s-1").state == state
