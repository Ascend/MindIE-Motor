# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Experiment X1 (design 20261015-03 §5): retention release on the real scheduling path.

Drives ``select_and_allocate`` against a real SHM/CAS instance pool with a real affinity
table, and builds the hint with the real ``parse_agent_hint`` — so the evidence covers
parsing (``ttl_explicit``), the request-path registration and the keeper, not the
declaration table alone.

- **A2 (feature on)**: an explicit ``ttl_s`` is recorded when the session is scheduled;
  once the window elapses the keeper releases the binding, and the next request for that
  session is a **fresh** placement again (``session affinity fresh`` in the log).
- **A1 (control, no explicit TTL)**: nothing is recorded, so the binding survives the same
  elapsed time and the next request is still a **hit** — today's behaviour.

Elapsed time is simulated by declaring with a past ``now_ms`` instead of sleeping, so the
experiment is deterministic.
"""

import logging
from unittest.mock import AsyncMock

import pytest

from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import PDRole
from motor.coordinator.domain import InstanceManager
from motor.coordinator.domain.agent_hint import parse_agent_hint
from motor.coordinator.domain.cache_declaration import CacheDeclarationTable
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.runtime.session_affinity import SessionAffinityTable
from motor.config.coordinator import CoordinatorConfig
from tests.coordinator.scheduler.test_scheduler_client import (  # pylint: disable=unused-import
    _cas_shm_name,
    _client_with_shm,
    _make_cas_instance,
    _seed_shm_tokens,
    native_lib,  # noqa: F401 - pytest fixture, imported so this module can request it
)

_ROLE = PDRole.ROLE_P.value


def _scheduled_affinity_outcome(caplog) -> str:
    """Extract the ``aff=`` label from the scheduling log line of the last request.

    The outcome is emitted on the ``scheduled role=... aff=<outcome>`` INFO line; the
    separate ``session affinity <outcome>`` line only fires for hit/parent/stale/epoch.
    """
    for record in reversed(caplog.records):
        message = record.getMessage()
        if not message.startswith("scheduled role="):
            continue
        for token in message.split():
            if token.startswith("aff="):
                return token.split("=", 1)[1]
    raise AssertionError("no scheduling log line captured")


def _request(req_id: str, session_id: str, cache_control: dict) -> RequestInfo:
    """Build a request whose hint comes from the real header parser."""
    hint = parse_agent_hint({}, header_hint={"session": {"session_id": session_id}, "cache_control": cache_control})
    return RequestInfo(
        req_id=req_id,
        req_data={},
        req_len=8,
        api="completions",
        token_ids=[1, 2, 3, 4],
        agent_hint_info=hint,
    )


async def _client_with_lifecycle():
    """Real instance pool + SHM CAS, plus real affinity/declaration tables."""
    config = CoordinatorConfig()
    instance_manager = InstanceManager(config)
    await instance_manager.refresh_instances(
        EventType.ADD,
        [_make_cas_instance(1, 10), _make_cas_instance(2, 20)],
    )
    client, writer = await _client_with_shm(instance_manager, _cas_shm_name("al"))
    # Seed both endpoints' workload, as the neighboring CAS tests do: an unseeded pool
    # makes the second call score against different availability and skews the aff label.
    _seed_shm_tokens(writer, 1, 10, 1.0)
    _seed_shm_tokens(writer, 2, 20, 50.0)
    # The in-process harness has no transport, so ``get_available_instances`` comes back
    # empty and affinity resolution would degrade to ``stale`` on every second call
    # (the policy path reads the local instance cache instead). Stub it so the
    # experiment measures the lifecycle, not that harness limitation.
    client.get_available_instances = AsyncMock(
        return_value={1: _make_cas_instance(1, 10), 2: _make_cas_instance(2, 20)}
    )
    client._session_affinity = SessionAffinityTable()
    client._cache_declarations = CacheDeclarationTable()
    return client, writer


@pytest.mark.asyncio
@pytest.mark.usefixtures("native_lib")
async def test_x1_explicit_retention_releases_binding_on_the_scheduling_path(caplog):
    """A2: declared retention ⇒ release on expiry ⇒ next request is fresh again."""
    client, writer = await _client_with_lifecycle()
    try:
        declarations = client._cache_declarations
        affinity = client._session_affinity
        declared = {"type": "ephemeral", "ttl_s": 60}

        caplog.set_level(logging.INFO)
        assert await client.select_and_allocate(PDRole.ROLE_P, _request("x1-a", "s-x1", declared)) is not None
        assert len(declarations) == 1, "the request path must record the explicit retention"
        assert affinity.get("s-x1", _ROLE) is not None
        assert _scheduled_affinity_outcome(caplog) == "fresh"

        # Simulate the declared 60s window elapsing, then one keeper tick.
        declarations.declare("s-x1", "ephemeral", 60, now_ms=0)
        client._sweep_cache_declarations()

        assert affinity.get("s-x1", _ROLE) is None, "keeper must release the elapsed binding"
        assert len(declarations) == 0

        caplog.clear()
        assert await client.select_and_allocate(PDRole.ROLE_P, _request("x1-b", "s-x1", declared)) is not None
        assert affinity.get("s-x1", _ROLE) is not None, "the re-placed session is pinned again"
        assert _scheduled_affinity_outcome(caplog) == "fresh"
    finally:
        client._workload_reader.detach()
        writer.release()


@pytest.mark.asyncio
@pytest.mark.usefixtures("native_lib")
async def test_x1_control_without_explicit_ttl_keeps_the_binding(caplog):
    """A1: no explicit TTL ⇒ nothing recorded ⇒ the binding survives (unchanged behaviour)."""
    client, writer = await _client_with_lifecycle()
    try:
        declarations = client._cache_declarations
        affinity = client._session_affinity
        bare = {"type": "pin"}

        caplog.set_level(logging.INFO)
        assert await client.select_and_allocate(PDRole.ROLE_P, _request("x1-c", "s-x1", bare)) is not None
        assert len(declarations) == 0, "a bare cache_control must not enter the lifecycle"
        assert affinity.get("s-x1", _ROLE) is not None

        # Arbitrarily much time later: the keeper has nothing to release.
        client._sweep_cache_declarations()
        assert affinity.get("s-x1", _ROLE) is not None

        caplog.clear()
        assert await client.select_and_allocate(PDRole.ROLE_P, _request("x1-d", "s-x1", bare)) is not None
        assert _scheduled_affinity_outcome(caplog) == "hit", "the untouched binding must still be applied"
    finally:
        client._workload_reader.detach()
        writer.release()
