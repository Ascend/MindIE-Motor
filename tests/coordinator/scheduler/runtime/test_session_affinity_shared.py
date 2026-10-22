# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for the cross-process shared session affinity table."""

import os
import time
import uuid

import pytest

from motor.coordinator.scheduler.runtime.session_affinity_shared import (
    SharedSessionAffinityTable,
)

SHM_KEY = "ut-" + os.environ.get("PYTEST_XDIST_WORKER", "main")


@pytest.fixture()
def table():
    return SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY)


def test_record_then_get_roundtrip(table):
    table.record("s-1", 7, "union")
    pin = table.get("s-1", "union")
    assert pin is not None and pin.instance_id == 7 and pin.epoch is None


def test_get_unknown_returns_none(table):
    assert table.get("s-missing", "union") is None


def test_record_with_epoch_roundtrip(table):
    table.record("s-e", 3, "union", epoch=2)
    pin = table.get("s-e", "union")
    assert pin.epoch == 2


def test_drop_removes_entry(table):
    table.record("s-d", 9, "union")
    table.drop("s-d", "union")
    assert table.get("s-d", "union") is None


def test_drop_unknown_is_noop(table):
    table.drop("s-nope", "union")


def test_role_scoping(table):
    """Same session under different roles are independent entries."""
    table.record("s-r", 1, "prefill")
    table.record("s-r", 2, "decode")
    assert table.get("s-r", "prefill").instance_id == 1
    assert table.get("s-r", "decode").instance_id == 2


def test_re_record_updates_instance(table):
    table.record("s-u", 1, "union")
    table.record("s-u", 5, "union")
    assert table.get("s-u", "union").instance_id == 5


def test_lru_eviction_when_full():
    """A full table evicts the LRU entry; a refreshed entry survives."""
    t = SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY + "-lru-" + uuid.uuid4().hex[:8])
    for i in range(64):
        t.record("evict-%d" % i, i, "union")
    t.get("evict-0", "union")  # refresh the oldest


def test_engine_type_change_resolves_stale_not_hard_fail():
    """Review 194528650: eligibility for pin resolution must carry the same
    engine/dispatch filters as the pinned path — an incompatible pin (engine
    switched) resolves as stale and falls back to the normal policy instead of
    503-ing while healthy compatible instances exist.
    """
    from motor.coordinator.scheduler.runtime.session_affinity import (
        SessionAffinityTable,
        resolve_session_pin,
    )

    table = SessionAffinityTable(max_entries=64)
    table.record("s-e", 1, "decode")  # pinned on the old engine's instance
    # New engine deployment: instance 1 no longer passes the filter, instance 2 does.
    decision = resolve_session_pin(table, "s-e", None, None, "decode", eligible_instance_ids={2})
    assert decision.instance_id is None
    assert decision.outcome in ("stale", "fresh")
    assert table.get("s-e", "decode") is None  # incompatible pin cleaned up


def test_balanced_renewal_stamp_distinct_from_touch():
    """renewed_at must only move on record (keeper touch is get-based)."""
    table = SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY + "-bal-" + uuid.uuid4().hex[:8])
    table.record("s", 1, "union")
    stamp = table.peek_renewal("s", "union")
    time.sleep(0.02)
    table.get("s", "union")
    assert table.peek_renewal("s", "union") == stamp


def _balanced_burst_worker(key, worker_id, sessions, results_q):
    """Each worker races N fresh balanced placements on the same shm table."""
    table = SharedSessionAffinityTable(max_entries=64, shm_key=key)
    time.sleep(0.05)  # let all workers attach before the burst
    for sid in sessions:
        chosen = table.record_balanced(sid, [1, 2], "union", None, lambda: {1: 0.0, 2: 0.0}, worker_id, endpoint_id=0)
        results_q.put((sid, chosen))


def test_balanced_fresh_burst_spreads_across_processes():
    """Review 194528732: count-read/selection/commit must be one critical
    section — two processes racing a fresh burst must not double-commit onto
    the same instance (previously reproduced as {1: 2}).
    """
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    key = SHM_KEY + "-burst-" + uuid.uuid4().hex[:8]
    results = ctx.Queue()
    procs = []
    for worker_id in range(2):
        sessions = [f"burst-{worker_id}-{i}" for i in range(8)]
        procs.append(
            ctx.Process(
                target=_balanced_burst_worker,
                args=(key, worker_id, sessions, results),
            )
        )
    for proc in procs:
        proc.start()
    pairs = [results.get(timeout=60) for _ in range(16)]
    for proc in procs:
        proc.join(timeout=60)

    counts = {1: 0, 2: 0}
    for _, chosen in pairs:
        assert chosen in (1, 2)
        counts[chosen] += 1
    assert counts == {1: 8, 2: 8}, f"burst distribution skewed: {counts}"


def test_record_balanced_replacing_moves_counter_to_chosen():
    """Review (PR #1196): re-pointing an existing pin via record_balanced must
    credit the chosen instance as well as dropping the old one, or the shared
    per-instance counts drift and balanced placement skews.
    """
    table = SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY + "-drift-" + uuid.uuid4().hex[:8])
    table.record("s-drift", 1, "union")
    assert table.count_pins_by_instance("union") == {1: 1}

    chosen = table.record_balanced("s-drift", [2], "union", None, lambda: {}, placement_seq=0)

    assert chosen == 2
    assert table.get("s-drift", "union").instance_id == 2
    assert table.count_pins_by_instance("union") == {2: 1}


def test_record_balanced_without_session_id_returns_none():
    """Review (PR #1196): a fresh decision without an own session id (only an
    absent parent pin) has nothing to pin; it must return None instead of
    raising in _slot_session / committing off-table.
    """
    table = SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY + "-none-" + uuid.uuid4().hex[:8])
    assert table.record_balanced(None, [1, 2], "union", None, lambda: {}, placement_seq=0) is None
    assert table.record_balanced("", [1, 2], "union", None, lambda: {}, placement_seq=0) is None
    assert table.count_pins_by_instance("union") == {}


def test_count_pins_by_instance_out_of_range_id_falls_back_to_scan():
    """Review (PR #1196): instance ids at/above _COUNTER_INSTANCES have no counter
    slot; the overflow marker must force a scan so those instances are counted.
    """
    table = SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY + "-bigid-" + uuid.uuid4().hex[:8])
    table.record("s-small", 3, "union")
    table.record("s-big", 100, "union")

    assert table.count_pins_by_instance("union") == {3: 1, 100: 1}

    table.drop("s-big", "union")
    assert table.count_pins_by_instance("union") == {3: 1}


def test_eviction_decrements_evicted_occupant_counter_across_roles():
    """Review 194979025: evicting a live different-key slot must subtract the
    occupant's own (role, instance) count, not the incoming pin's role.
    """
    table = SharedSessionAffinityTable(max_entries=1, shm_key=SHM_KEY + "-evict-" + uuid.uuid4().hex[:8])
    table.record("s-a", 1, "prefill")
    table.record("s-b", 2, "decode")  # only one slot: evicts s-a

    assert table.get("s-a", "prefill") is None
    assert table.get("s-b", "decode").instance_id == 2
    assert table.count_pins_by_instance("prefill") == {}
    assert table.count_pins_by_instance("decode") == {2: 1}


def test_eviction_does_not_inherit_retention():
    """Review 194979025: an evicted occupant's cache_control promise must not be
    carried onto the new session (it would defer that session's own release).
    """
    table = SharedSessionAffinityTable(max_entries=1, shm_key=SHM_KEY + "-evret-" + uuid.uuid4().hex[:8])
    table.record("s-a", 1, "union")
    expires_at = time.time() + 1000
    table.declare_retention("s-a", "union", expires_at)
    assert table.peek_retention_expires("s-a", "union") == pytest.approx(expires_at)

    table.record("s-b", 2, "union")  # evicts s-a

    assert table.peek_retention_expires("s-b", "union") == 0.0


def test_record_balanced_rebind_keeps_retention_not_endpoint():
    """Review 194981811: slot_retention must be unpacked from retention_expires,
    not from pinned_endpoint — a same-key rebind keeps the declared promise.
    """
    table = SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY + "-rebal-" + uuid.uuid4().hex[:8])
    table.record("s-r", 1, "union", endpoint_id=7)
    expires_at = time.time() + 500
    table.declare_retention("s-r", "union", expires_at)

    chosen = table.record_balanced("s-r", [1], "union", None, lambda: {}, placement_seq=0, endpoint_id=7)

    assert chosen == 1
    assert table.peek_retention_expires("s-r", "union") == pytest.approx(expires_at)


def test_record_balanced_fresh_pin_has_zero_retention():
    """A fresh balanced pin inherits no promise, even with a recorded endpoint."""
    table = SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY + "-freshret-" + uuid.uuid4().hex[:8])
    chosen = table.record_balanced("s-f", [5], "union", None, lambda: {}, placement_seq=0, endpoint_id=3)

    assert chosen == 5
    assert table.peek_retention_expires("s-f", "union") == 0.0
    assert table.get("s-f", "union").endpoint_id == 3


def test_eviction_skips_retention_protected_pins():
    """Review 194980525: LRU eviction must not drop a pin with a live
    cache_control promise while an unprotected slot is available.
    """
    table = SharedSessionAffinityTable(max_entries=2, shm_key=SHM_KEY + "-prot-" + uuid.uuid4().hex[:8])
    table.record("s-old", 1, "union")
    table.record("s-mid", 2, "union")
    table.declare_retention("s-old", "union", time.time() + 3600)

    table.record("s-new", 3, "union")  # must evict s-mid, keep the promised s-old

    assert table.get("s-old", "union") is not None
    assert table.get("s-mid", "union") is None
    assert table.get("s-new", "union") is not None
    assert table.count_pins_by_instance("union") == {1: 1, 3: 1}


def test_eviction_falls_back_when_all_pins_protected():
    """A full table of promised pins still accepts an insert (evict oldest)."""
    table = SharedSessionAffinityTable(max_entries=1, shm_key=SHM_KEY + "-allprot-" + uuid.uuid4().hex[:8])
    table.record("s-a", 1, "union")
    table.declare_retention("s-a", "union", time.time() + 3600)

    table.record("s-b", 2, "union")

    assert table.get("s-a", "union") is None
    assert table.get("s-b", "union").instance_id == 2
    assert table.count_pins_by_instance("union") == {2: 1}


def test_pin_hit_carries_recorded_endpoint():
    """Review 194528817: the pin records the endpoint/DP that owns the KV and
    resolve returns it, so a hit returns to the recorded DP instead of
    re-picking by load.
    """
    table = SharedSessionAffinityTable(max_entries=64, shm_key=SHM_KEY + "-dp-" + uuid.uuid4().hex[:8])
    from motor.coordinator.scheduler.runtime.session_affinity import resolve_session_pin

    table.record("s-dp", 5, "union", endpoint_id=1)
    decision = resolve_session_pin(table, "s-dp", None, None, "union", {5})
    assert decision.instance_id == 5
    assert decision.endpoint_id == 1
    # re-record on the other DP moves the recorded owner
    table.record("s-dp", 5, "union", endpoint_id=2)
    decision = resolve_session_pin(table, "s-dp", None, None, "union", {5})
    assert decision.endpoint_id == 2
