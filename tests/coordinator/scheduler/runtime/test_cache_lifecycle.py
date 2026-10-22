# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Unit tests for the cache_control lifecycle wiring (design 20261015-03, step ①).

Covers the worker-side pieces: recording an explicit retention on the scheduling path,
the keeper that releases elapsed bindings, and the finalize/drop paths that must cancel
a pending release. Instances are built with ``__new__`` plus fake attributes, following
the neighboring ``test_agent_status_channel`` pattern (no sockets, no real clock waits
except where the keeper loop itself is exercised).
"""

import asyncio
from types import SimpleNamespace

import pytest

from motor.coordinator.domain.cache_declaration import (
    LIFECYCLE_SWEEP_ENV,
    CacheDeclarationTable,
)
from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient
from motor.coordinator.scheduler.runtime.session_affinity import SessionAffinityTable

SHM_KEY = "ut-t12-" + str(__import__("os").getpid())


def _client(
    affinity: SessionAffinityTable | None,
    declarations: CacheDeclarationTable | None,
) -> AsyncSchedulerClient:
    client = AsyncSchedulerClient.__new__(AsyncSchedulerClient)
    client._session_affinity = affinity
    client._cache_declarations = declarations
    client._cache_lifecycle_task = None
    return client


def _hint(*, decl_type: str = "ephemeral", ttl: int = 60, ttl_explicit: bool, msg_offset: int | None = None):
    cache_control = SimpleNamespace(type=decl_type, ttl=ttl, ttl_explicit=ttl_explicit, msg_offset=msg_offset)
    return SimpleNamespace(cache_control=cache_control, session_id="s-1")


def test_record_declaration_requires_an_explicit_ttl():
    """A bare cache_control must not change behaviour: ``ttl`` defaults to 300."""
    declarations = CacheDeclarationTable()
    client = _client(None, declarations)

    client._record_cache_declaration(_hint(decl_type="pin", ttl=300, ttl_explicit=False), "s-1")
    assert len(declarations) == 0

    client._record_cache_declaration(_hint(ttl=60, ttl_explicit=True, msg_offset=3), "s-1")
    assert len(declarations) == 1
    recorded = declarations.get("s-1", now_ms=0)
    assert recorded is not None
    assert recorded.ttl_s == 60
    assert recorded.msg_offset == 3


def test_record_declaration_is_inert_without_table_or_session():
    """Disabled feature and session-less requests are both silent no-ops."""
    client = _client(None, None)
    client._record_cache_declaration(_hint(ttl_explicit=True), "s-1")

    declarations = CacheDeclarationTable()
    client = _client(None, declarations)
    client._record_cache_declaration(_hint(ttl_explicit=True), None)
    client._record_cache_declaration(SimpleNamespace(cache_control=None), "s-1")

    assert len(declarations) == 0


def test_record_declaration_rejects_unknown_type():
    """An unexpected type is dropped rather than stored with a guessed semantic."""
    declarations = CacheDeclarationTable()
    client = _client(None, declarations)

    client._record_cache_declaration(_hint(decl_type="forever", ttl_explicit=True), "s-1")

    assert len(declarations) == 0


def test_sweep_releases_the_pin_of_an_elapsed_declaration():
    """The keeper's core contract: elapsed retention ⇒ binding released."""
    affinity = SessionAffinityTable()
    affinity.record("s-1", 7, "union")
    declarations = CacheDeclarationTable()
    client = _client(affinity, declarations)
    # Declared far in the past, so the real clock already considers it elapsed.
    declarations.declare("s-1", "ephemeral", 60, now_ms=0)

    client._sweep_cache_declarations()

    assert affinity.get("s-1", "union") is None
    assert len(declarations) == 0


def test_sweep_keeps_the_pin_of_an_active_declaration():
    """A declaration still inside its window must not touch the binding."""
    affinity = SessionAffinityTable()
    affinity.record("s-1", 7, "union")
    declarations = CacheDeclarationTable()
    client = _client(affinity, declarations)
    declarations.declare("s-1", "pin", 3600)

    client._sweep_cache_declarations()

    assert affinity.get("s-1", "union").instance_id == 7
    assert len(declarations) == 1


def test_sweep_without_affinity_table_still_clears_the_declaration():
    """Declarations are bookkeeping; the sweep must stay safe with affinity disabled."""
    declarations = CacheDeclarationTable()
    client = _client(None, declarations)
    declarations.declare("s-1", "ephemeral", 60, now_ms=0)

    client._sweep_cache_declarations()

    assert len(declarations) == 0


@pytest.mark.asyncio
async def test_finalize_drops_declaration_and_pin_together():
    """finalize releases immediately, so the keeper must not release again later."""
    affinity = SessionAffinityTable()
    affinity.record("s-1", 7, "union")
    declarations = CacheDeclarationTable()
    client = _client(affinity, declarations)
    declarations.declare("s-1", "pin", 3600)

    await client._on_agent_status_event("s-1", "finalize")

    assert len(declarations) == 0
    assert affinity.get("s-1", "union") is None
    # Nothing left for the keeper: a later sweep is a no-op for this session.
    client._sweep_cache_declarations()
    assert len(declarations) == 0


def test_drop_session_pins_also_forgets_the_declaration():
    """The explicit drop API clears bookkeeping even when no pin is left here."""
    affinity = SessionAffinityTable()
    affinity.record("s-1", 7, "union")
    declarations = CacheDeclarationTable()
    client = _client(affinity, declarations)
    declarations.declare("s-1", "pin", 3600)

    assert client.drop_session_pins("s-1") == 1
    assert len(declarations) == 0
    assert client.drop_session_pins("s-1") == 0


def test_keeper_start_is_a_noop_when_disabled():
    """With the feature off there is no table and therefore no background task."""
    client = _client(None, None)

    client._start_cache_lifecycle_task()

    assert client._cache_lifecycle_task is None


@pytest.mark.asyncio
async def test_keeper_loop_releases_after_the_interval(monkeypatch):
    """End-to-end keeper check: start the task, let one period pass, stop it cleanly."""
    monkeypatch.setenv(LIFECYCLE_SWEEP_ENV, "1")
    affinity = SessionAffinityTable()
    affinity.record("s-1", 7, "union")
    declarations = CacheDeclarationTable()
    client = _client(affinity, declarations)
    declarations.declare("s-1", "ephemeral", 60, now_ms=0)

    client._start_cache_lifecycle_task()
    try:
        await asyncio.sleep(1.2)
    finally:
        await client._stop_cache_lifecycle_task()

    assert affinity.get("s-1", "union") is None
    assert client._cache_lifecycle_task is None


def _filled_affinity(max_entries: int):
    """Three bindings in a table that holds three: one wanted, two already elapsed."""
    affinity = SessionAffinityTable(max_entries=max_entries)
    declarations = CacheDeclarationTable()
    client = _client(affinity, declarations)
    affinity.record("s-keep", 3, "union")
    declarations.declare("s-keep", "pin", 3600)
    for session_id, instance_id in (("s-short-1", 1), ("s-short-2", 2)):
        affinity.record(session_id, instance_id, "union")
        # Elapsed on the real clock, so the next keeper tick releases it.
        declarations.declare(session_id, "ephemeral", 60, now_ms=0)
    return client, affinity, declarations


def test_x3_expiry_returns_capacity_so_active_bindings_survive():
    """Experiment X3: releasing elapsed bindings is what keeps later sessions from evicting wanted ones."""
    # A1 - no release: the next session overflows the table and takes the oldest binding.
    _, affinity_control, _ = _filled_affinity(max_entries=3)
    affinity_control.record("s-new", 4, "union")
    assert affinity_control.get("s-keep", "union") is None

    # A2 - one keeper tick releases the elapsed declarations first, so capacity is back
    # and the still-wanted binding survives the same insertion.
    client, affinity_released, declarations = _filled_affinity(max_entries=3)
    client._sweep_cache_declarations()
    assert len(declarations) == 1
    assert len(affinity_released) == 1

    affinity_released.record("s-new", 4, "union")
    assert affinity_released.get("s-keep", "union") is not None


def test_touch_refreshes_only_declared_bindings():
    """Protection reads declared bindings only, and stays safe with affinity disabled."""
    affinity = SessionAffinityTable()
    affinity.record("s-declared", 1, "union")
    affinity.record("s-undeclared", 2, "union")
    declarations = CacheDeclarationTable()
    client = _client(affinity, declarations)
    declarations.declare("s-declared", "pin", 3600, role="union")

    assert client._touch_cache_declarations() == 1

    # A declaration whose role has no binding (or no recorded role) yields no hit.
    declarations.declare("s-elsewhere", "pin", 3600, role="prefill")
    declarations.declare("s-norole", "pin", 3600)
    assert client._touch_cache_declarations() == 1

    # Disabled feature / no affinity table: inert.
    assert _client(None, declarations)._touch_cache_declarations() == 0
    assert _client(affinity, None)._touch_cache_declarations() == 0


def test_x2_protection_keeps_declared_bindings_under_capacity_pressure():
    """Experiment X2: the keeper's touch pass is what saves a declared binding from LRU.

    A table of 3 receives one declared session followed by five others: without the
    protection pass the declared binding is evicted like any other; with it (one pass per
    arrival, exactly what the keeper does) it survives.
    """

    def _fill(protect: bool) -> SessionAffinityTable:
        affinity = SessionAffinityTable(max_entries=3)
        declarations = CacheDeclarationTable()
        client = _client(affinity, declarations)
        affinity.record("s-declared", 1, "union")
        declarations.declare("s-declared", "pin", 3600, role="union")
        for index in range(5):
            affinity.record(f"s-other-{index}", 100 + index, "union")
            if protect:
                assert client._touch_cache_declarations() == 1
        return affinity

    unprotected = _fill(protect=False)
    assert unprotected.get("s-declared", "union") is None, "without protection LRU takes it"

    protected = _fill(protect=True)
    assert protected.get("s-declared", "union") is not None, "the touched binding survives"


def test_expired_declaration_defers_release_when_pin_renewed_elsewhere():
    """Review 194528478: the ledger is worker-local but the shared table is not.
    When another worker re-recorded the pin after this worker's declaration
    (renewal stamp newer than the declaration's wall stamp), this worker's
    keeper must defer — a longer declaration elsewhere governs.
    """
    import time
    import uuid

    from motor.coordinator.scheduler.runtime.session_affinity_shared import SharedSessionAffinityTable

    affinity = SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY + "-t12-" + uuid.uuid4().hex[:8])
    declarations = CacheDeclarationTable(max_declarations=64)
    client = _client(affinity, declarations)

    affinity.record("s-x", 7, "union")
    declarations.declare("s-x", "pin", 60, role="union")
    time.sleep(0.02)
    # Another worker renews: re-record (renewal stamp) + its own longer
    # declaration published onto the shared slot (declare_retention).
    affinity.record("s-x", 7, "union", epoch=1)
    affinity.declare_retention("s-x", "union", time.time() + 3600)
    assert affinity.peek_renewal("s-x", "union") > declarations.get("s-x").wall_created_s

    declaration = declarations.get("s-x")
    declaration.expire_ms = 0  # simulate the 60s elapsing
    client._sweep_cache_declarations()

    assert affinity.get("s-x", "union") is not None, "renewed pin must survive the other worker's expiry"


def test_sweep_release_is_batch_bounded():
    """Review 194527836: a burst of simultaneous expiries must not release the
    whole batch in one event-loop pass — the keeper releases at most
    _SWEEP_RELEASE_BATCH per tick and requeues the rest.
    """
    import uuid

    from motor.coordinator.scheduler.runtime import scheduler_client as sc_module
    from motor.coordinator.scheduler.runtime.session_affinity_shared import SharedSessionAffinityTable

    affinity = SharedSessionAffinityTable(max_entries=128, shm_key=SHM_KEY + "-batch-" + uuid.uuid4().hex[:8])
    declarations = CacheDeclarationTable(max_declarations=128)
    client = _client(affinity, declarations)

    total = sc_module._SWEEP_RELEASE_BATCH + 5
    for i in range(total):
        session = f"s-batch-{i}"
        affinity.record(session, 1, "union")
        declarations.declare(session, "pin", 60)
        declaration = declarations.get(session)
        declaration.expire_ms = 0  # simulate elapsing

    client._sweep_cache_declarations()
    remaining = sum(1 for i in range(total) if affinity.get(f"s-batch-{i}", "union") is not None)
    assert remaining == 5, f"expected exactly 5 deferred, got {remaining}"

    client._sweep_cache_declarations()
    client._sweep_cache_declarations()
    remaining = sum(1 for i in range(total) if affinity.get(f"s-batch-{i}", "union") is not None)
    assert remaining == 0, "subsequent ticks must drain the deferred batch"
