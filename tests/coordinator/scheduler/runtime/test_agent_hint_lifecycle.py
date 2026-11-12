# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Unit tests for the Agent Hint KVC lifecycle controller (L0/L1)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from motor.config.coordinator import KvcLifecycleConfig
from motor.coordinator.scheduler.runtime.agent_hint_lifecycle import (
    AgentHintLifecycleManager,
    CentralAgentLifecycle,
    build_manage_request_body,
    summarize_edit_results,
)


def test_build_manage_request_body_session_edit_has_no_block_coords():
    body = build_manage_request_body("s-1", "evict", model="m")

    assert body["model"] == "m"
    assert body["messages"]
    hint = body["agent_hint"]
    assert hint["session_id"] == "s-1"
    assert hint["context_management"] == {
        "manage_request": True,
        "edits": [{"type": "evict", "target": "session"}],
    }


def test_build_manage_request_body_messages_requires_block_coords():
    with pytest.raises(ValueError):
        build_manage_request_body("s-1", "evict", model="m", target="messages")

    body = build_manage_request_body(
        "s-1", "evict", model="m", target="messages", block_start=0, block_end=6
    )
    assert body["agent_hint"]["context_management"]["edits"][0]["block_end"] == 6


@pytest.mark.asyncio
async def test_finalize_is_idempotent_and_sends_one_evict():
    sender = AsyncMock(return_value=[{"op_status": True, "actual_op_block_num": 3}])
    manager = AgentHintLifecycleManager(sender=sender)
    try:
        await manager.on_status("s-1", "finalize")
        await manager.on_status("s-1", "finalize")

        sender.assert_awaited_once_with("s-1", "evict")
        assert manager.stats()["evict_total"] == 1
        assert manager.stats()["edit_success"] == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_non_finalize_status_only_records_state():
    sender = AsyncMock(return_value=[])
    manager = AgentHintLifecycleManager(sender=sender)
    try:
        await manager.on_status("s-1", "pause")

        sender.assert_not_awaited()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_sender_failure_is_counted_and_never_raised():
    sender = AsyncMock(side_effect=RuntimeError("boom"))
    manager = AgentHintLifecycleManager(sender=sender)
    try:
        await manager.on_status("s-1", "finalize")

        assert manager.stats()["send_fail"] == 1
        assert manager.stats()["evict_total"] == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_missing_sender_degrades_to_warning():
    manager = AgentHintLifecycleManager(sender=None)
    try:
        await manager.on_status("s-1", "finalize")

        assert manager.stats()["evict_total"] == 0
    finally:
        await manager.aclose()


def test_summarize_edit_results_counts_status_and_ignores_garbage():
    assert summarize_edit_results(
        [{"op_status": True}, {"op_status": False}, "ignored", {}]
    ) == (1, 1)
    assert summarize_edit_results(None) == (0, 0)


@pytest.mark.asyncio
async def test_scheduler_client_manage_request_without_pin_degrades():
    """No affinity pin -> no engine call and no exception (WARN-only degradation)."""
    from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient

    client = AsyncSchedulerClient.__new__(AsyncSchedulerClient)
    client._kvc_lifecycle = SimpleNamespace(enabled=True, manage_timeout_s=1)
    client._session_affinity = None

    assert await client.send_session_manage_request("s-1", "evict") == []


@pytest.mark.asyncio
async def test_scheduler_client_manage_request_disabled_returns_empty():
    from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient

    client = AsyncSchedulerClient.__new__(AsyncSchedulerClient)
    client._kvc_lifecycle = SimpleNamespace(enabled=False, manage_timeout_s=1)

    assert await client.send_session_manage_request("s-1", "evict") == []


@pytest.mark.asyncio
async def test_pause_offloads_once_then_resume_prefetches_and_pause_rearms():
    sender = AsyncMock(return_value=[{"op_status": True}])
    manager = AgentHintLifecycleManager(sender=sender, idle_offload_s=0.01)
    try:
        await manager.on_status("s-1", "pause")
        await manager.on_status("s-1", "pause")
        await asyncio.sleep(0.05)
        sender.assert_awaited_once_with("s-1", "offload")

        await manager.on_status("s-1", "resume")
        await manager.on_status("s-1", "pause")
        await asyncio.sleep(0.05)
        assert sender.await_count == 3
        assert [call.args for call in sender.await_args_list] == [
            ("s-1", "offload"),
            ("s-1", "prefetch"),
            ("s-1", "offload"),
        ]
        assert manager.stats()["offload_total"] == 2
        assert manager.stats()["prefetch_total"] == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_running_before_idle_timer_cancels_offload():
    sender = AsyncMock(return_value=[{"op_status": True}])
    manager = AgentHintLifecycleManager(sender=sender, idle_offload_s=0.05)
    try:
        await manager.on_status("s-1", "pause")
        await manager.on_status("s-1", "running")
        await asyncio.sleep(0.1)

        sender.assert_not_awaited()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_idle_evict_runs_after_offload():
    calls: list[str] = []

    async def sender(_session_id: str, edit_type: str):
        calls.append(edit_type)
        return [{"op_status": True}]

    manager = AgentHintLifecycleManager(sender=sender, idle_offload_s=0.01, idle_evict_s=0.03)
    try:
        await manager.on_status("s-1", "pause")
        await asyncio.sleep(0.06)

        assert calls == ["offload", "evict"]
        assert manager.stats()["offload_total"] == 1
        assert manager.stats()["evict_total"] == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_resume_after_offload_prefetches():
    calls: list[str] = []

    async def sender(_session_id: str, edit_type: str):
        calls.append(edit_type)
        return [{"op_status": True}]

    manager = AgentHintLifecycleManager(sender=sender, idle_offload_s=0.01)
    try:
        await manager.on_status("s-1", "pause")
        await asyncio.sleep(0.03)
        await manager.on_status("s-1", "resume")

        assert calls == ["offload", "prefetch"]
        assert manager.stats()["prefetch_total"] == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_running_after_offload_does_not_prefetch_without_resume():
    calls: list[str] = []

    async def sender(_session_id: str, edit_type: str):
        calls.append(edit_type)
        return [{"op_status": True}]

    manager = AgentHintLifecycleManager(sender=sender, idle_offload_s=0.01)
    try:
        await manager.on_status("s-1", "pause")
        await asyncio.sleep(0.03)
        await manager.on_status("s-1", "running")

        assert calls == ["offload"]
        assert manager.stats()["prefetch_total"] == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_zero_idle_delays_disable_idle_actions():
    sender = AsyncMock(return_value=[{"op_status": True}])
    manager = AgentHintLifecycleManager(sender=sender, idle_offload_s=0.0, idle_evict_s=0.0)
    try:
        await manager.on_status("s-1", "pause")
        await asyncio.sleep(0.02)

        sender.assert_not_awaited()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_repeated_pause_does_not_restart_idle_timer():
    calls: list[str] = []

    async def sender(_session_id: str, edit_type: str):
        calls.append(edit_type)
        return [{"op_status": True}]

    manager = AgentHintLifecycleManager(sender=sender, idle_offload_s=0.05)
    try:
        await manager.on_status("s-1", "pause")
        await asyncio.sleep(0.03)
        await manager.on_status("s-1", "pause")
        await asyncio.sleep(0.04)

        assert calls == ["offload"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_finalize_then_new_epoch_running_revives_and_allows_offload_again():
    calls: list[str] = []

    async def sender(_session_id: str, edit_type: str):
        calls.append(edit_type)
        return [{"op_status": True}]

    manager = AgentHintLifecycleManager(sender=sender, idle_offload_s=0.01)
    try:
        await manager.on_status("s-1", "finalize", epoch=0)
        await manager.on_status("s-1", "running", epoch=1)
        await manager.on_status("s-1", "pause", epoch=1)
        await asyncio.sleep(0.03)

        assert calls == ["evict", "offload"]
        assert manager.stats()["epoch_revive"] == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_capacity_gate_skips_actions_without_pressure():
    sender = AsyncMock(return_value=[{"op_status": True}])
    pressure = {"value": False}
    manager = AgentHintLifecycleManager(
        sender=sender,
        idle_offload_s=0.01,
        pressure_provider=lambda: pressure["value"],
    )
    try:
        await manager.on_status("s-1", "tool")
        await asyncio.sleep(0.03)
        sender.assert_not_awaited()
        assert manager.stats()["gate_skip"] == 1

        pressure["value"] = True
        await manager.on_status("s-1", "resume")
        await manager.on_status("s-1", "pause")
        await asyncio.sleep(0.03)
        sender.assert_awaited_once_with("s-1", "offload")
    finally:
        await manager.aclose()


def test_affinity_load_gate_drops_pins_above_relative_load():
    from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient

    client = AsyncSchedulerClient.__new__(AsyncSchedulerClient)
    client._affinity_load_gate_ratio = 0.5
    client._instance_active_tokens = lambda: {1: 100.0, 2: 140.0, 3: 1000.0}

    assert client._apply_affinity_load_gate({1, 2, 3}) == {1, 2}


def test_affinity_load_gate_is_disabled_at_zero_and_never_empties():
    from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient

    client = AsyncSchedulerClient.__new__(AsyncSchedulerClient)
    client._affinity_load_gate_ratio = 0.0
    client._instance_active_tokens = lambda: {1: 0.0, 2: 1000.0}

    assert client._apply_affinity_load_gate({1, 2}) == {1, 2}

    client._affinity_load_gate_ratio = 0.5
    client._instance_active_tokens = lambda: {1: 0.0, 2: 1000.0}
    # The gate may drop all non-zero instances; an all-gated pool falls back.
    assert client._apply_affinity_load_gate({1, 2}) == {1}
    client._instance_active_tokens = lambda: {1: 1000.0, 2: 1000.0}
    assert client._apply_affinity_load_gate({1, 2}) == {1, 2}


def test_capacity_pressure_uses_kv_usage_high_water(monkeypatch):
    from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient
    import motor.coordinator.scheduler.runtime.scheduler_client as scheduler_client_module

    client = AsyncSchedulerClient.__new__(AsyncSchedulerClient)
    client._kvc_lifecycle = SimpleNamespace(offload_kv_usage_high_water=0.8)
    endpoint = SimpleNamespace(id=1)
    instance = SimpleNamespace(id=1, get_all_endpoints=lambda include_headless=True: [endpoint])
    client._instances_from_cache = lambda: {1: instance}
    monkeypatch.setattr(scheduler_client_module, "get_endpoint_kv_cache_usage", lambda _i, _e: 0.5)
    assert client.capacity_pressure() is False
    monkeypatch.setattr(scheduler_client_module, "get_endpoint_kv_cache_usage", lambda _i, _e: 0.9)
    assert client.capacity_pressure() is True
    monkeypatch.setattr(scheduler_client_module, "get_endpoint_kv_cache_usage", lambda _i, _e: None)
    assert client.capacity_pressure() is False


def test_capacity_pressure_disabled_by_zero_threshold():
    from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient

    client = AsyncSchedulerClient.__new__(AsyncSchedulerClient)
    client._kvc_lifecycle = SimpleNamespace(offload_kv_usage_high_water=0.0)
    assert client.capacity_pressure() is True


@pytest.mark.asyncio
async def test_stats_listener_receives_counter_snapshots():
    snapshots: list[dict[str, int]] = []
    sender = AsyncMock(return_value=[{"op_status": True}])
    manager = AgentHintLifecycleManager(sender=sender)
    manager.set_stats_listener(snapshots.append)
    try:
        await manager.on_status("s-1", "finalize")
        assert snapshots
        assert snapshots[-1]["evict_total"] == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_central_lifecycle_cancels_idle_offload_on_later_running():
    """One owner sees pause then running, so the idle timer must be cancelled."""
    sender = AsyncMock(return_value=[{"op_status": True}])
    central = CentralAgentLifecycle(
        kvc_config=KvcLifecycleConfig(enabled=True, idle_offload_s=0.05),
    )
    central.set_sender(sender)
    try:
        await central.apply("s-central", "pause")
        await asyncio.sleep(0.01)
        await central.apply("s-central", "running")
        await asyncio.sleep(0.1)

        sender.assert_not_awaited()
        assert central.stats()["offload_total"] == 0
    finally:
        await central.aclose()


@pytest.mark.asyncio
async def test_central_lifecycle_offload_then_prefetch_on_same_owner():
    sender = AsyncMock(return_value=[{"op_status": True}])
    central = CentralAgentLifecycle(
        kvc_config=KvcLifecycleConfig(enabled=True, idle_offload_s=0.02, prefetch_enabled=True),
    )
    central.set_sender(sender)
    try:
        await central.apply("s-central", "pause")
        await asyncio.sleep(0.08)
        assert sender.await_args_list[-1].args == ("s-central", "offload")

        await central.apply("s-central", "resume")
        assert sender.await_args_list[-1].args == ("s-central", "prefetch")
        assert central.stats()["offload_total"] == 1
        assert central.stats()["prefetch_total"] == 1
    finally:
        await central.aclose()


@pytest.mark.asyncio
async def test_central_lifecycle_finalize_evicts_and_releases_pins():
    sender = AsyncMock(return_value=[{"op_status": True}])
    release = AsyncMock(return_value=None)
    central = CentralAgentLifecycle(kvc_config=KvcLifecycleConfig(enabled=True))
    central.set_sender(sender)
    try:
        result = await central.apply("s-central", "finalize", finalize_release=release)

        assert result["applied"] is True
        release.assert_awaited_once_with("s-central", None)
        sender.assert_awaited_once_with("s-central", "evict")
        assert central.stats()["evict_total"] == 1
        assert central.get("s-central")["state"] == "finalize"
    finally:
        await central.aclose()


@pytest.mark.asyncio
async def test_tool_and_suspend_aliases_are_pause():
    sender = AsyncMock(return_value=[{"op_status": True}])
    manager = AgentHintLifecycleManager(sender=sender, idle_offload_s=0.01)
    try:
        await manager.on_status("s-tool", "tool")
        await manager.on_status("s-suspend", "suspend")
        await asyncio.sleep(0.03)

        assert [call.args for call in sender.await_args_list] == [
            ("s-tool", "offload"),
            ("s-suspend", "offload"),
        ]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_resume_after_finalize_does_not_revive():
    sender = AsyncMock(return_value=[{"op_status": True}])
    manager = AgentHintLifecycleManager(sender=sender)
    try:
        await manager.on_status("s-1", "finalize", epoch=0)
        await manager.on_status("s-1", "resume", epoch=1)

        sender.assert_awaited_once_with("s-1", "evict")
        assert manager.stats()["epoch_revive"] == 0
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_manage_request_build_error_keeps_prior_results_and_continues(monkeypatch):
    """A ValueError for one pin must not discard results or skip remaining pins."""
    from motor.coordinator.scheduler.runtime import scheduler_client as sc_module

    client = sc_module.AsyncSchedulerClient.__new__(sc_module.AsyncSchedulerClient)
    client._kvc_lifecycle = SimpleNamespace(enabled=True, manage_timeout_s=1)
    pins = [SimpleNamespace(instance_id=1, endpoint_id=10), SimpleNamespace(instance_id=2, endpoint_id=20)]
    instances = {1: SimpleNamespace(model_name="m1"), 2: SimpleNamespace(model_name="m2")}
    client._session_pins_for_manage = lambda _sid: pins
    client._instances_from_cache = lambda: instances
    client._endpoint_for_pin = lambda _instance, _endpoint_id: SimpleNamespace(ip="127.0.0.1", business_port=8000)
    client._post_manage_request = AsyncMock(return_value={"op_status": True})
    calls = {"count": 0}

    def _build_once_then_fail(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise ValueError("bad body")
        return {"body": True}

    monkeypatch.setattr(sc_module, "build_manage_request_body", _build_once_then_fail)

    results = await client.send_session_manage_request("s-1", "evict")

    assert results == [{"op_status": True}]
    assert client._post_manage_request.await_count == 1


def test_endpoint_for_pin_falls_back_when_pinned_endpoint_is_gone(monkeypatch):
    """A stale endpoint_id must fall back to a healthy endpoint, not drop the pin."""
    from motor.coordinator.scheduler.runtime import scheduler_client as sc_module

    fallback = object()
    instance = SimpleNamespace(id=7, get_all_endpoints=lambda include_headless=True: [])
    monkeypatch.setattr(sc_module, "select_endpoint_for_instance", lambda _instance: fallback)

    assert sc_module.AsyncSchedulerClient._endpoint_for_pin(instance, 99) is fallback
