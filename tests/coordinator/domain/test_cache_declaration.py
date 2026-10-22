# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of MulanPSL2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for the cache_control declaration table (design 20261015-03)."""

import pytest

from motor.coordinator.domain.cache_declaration import (
    DECL_TYPE_EPHEMERAL,
    DECL_TYPE_PIN,
    LIFECYCLE_ENV_FLAG,
    LIFECYCLE_MAX_ENV,
    LIFECYCLE_SWEEP_ENV,
    LIFECYCLE_TTL_MAX_ENV,
    CacheDeclarationTable,
    cache_lifecycle_sweep_interval_s,
    create_cache_declaration_table_from_env,
)

_ENV_NAMES = (LIFECYCLE_ENV_FLAG, LIFECYCLE_MAX_ENV, LIFECYCLE_TTL_MAX_ENV, LIFECYCLE_SWEEP_ENV)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate every test from an operator's environment."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_declare_records_expiry_window():
    """A declaration carries the clamped TTL and the resulting expiry instant."""
    table = CacheDeclarationTable()
    decl = table.declare("s-1", DECL_TYPE_EPHEMERAL, 60, msg_offset=10, now_ms=1_000)

    assert decl is not None
    assert decl.session_id == "s-1"
    assert decl.decl_type == DECL_TYPE_EPHEMERAL
    assert decl.ttl_s == 60
    assert decl.msg_offset == 10
    assert decl.expire_ms == 1_000 + 60_000
    assert len(table) == 1
    assert decl.remaining_s(1_000) == 60
    assert decl.remaining_s(1_000 + 30_000) == 30
    assert not decl.is_expired(1_000 + 59_999)
    assert decl.is_expired(1_000 + 60_000)
    assert decl.age_s(1_000 + 5_000) == 5
    assert decl.to_dict() == {
        "session_id": "s-1",
        "type": DECL_TYPE_EPHEMERAL,
        "ttl_s": 60,
        "msg_offset": 10,
        "roles": [],
    }


def test_declare_clamps_ttl_into_spec_window_and_ceiling():
    """Parser clamps to [60, 3600]; the table re-clamps defensively (incl. configured ceiling)."""
    table = CacheDeclarationTable()
    assert table.declare("low", DECL_TYPE_PIN, 5, now_ms=0).ttl_s == 60
    assert table.declare("high", DECL_TYPE_PIN, 99_999, now_ms=0).ttl_s == 3600

    table.update_limits(16, 120, now_ms=0)
    assert table.declare("cfg", DECL_TYPE_PIN, 600, now_ms=0).ttl_s == 120
    table.update_limits(16, 1, now_ms=0)  # ceiling below the floor is lifted to the floor
    assert table.declare("floor", DECL_TYPE_PIN, 600, now_ms=0).ttl_s == 60


def test_declare_renews_existing_declaration():
    """Re-declaring the same session overwrites in place and extends the window."""
    table = CacheDeclarationTable()
    table.declare("s-1", DECL_TYPE_EPHEMERAL, 60, now_ms=0)
    renewed = table.declare("s-1", DECL_TYPE_PIN, 600, msg_offset=3, now_ms=10_000)

    assert len(table) == 1
    assert renewed is not None
    current = table.get("s-1", now_ms=10_000)
    assert current is not None
    assert current.decl_type == DECL_TYPE_PIN
    assert current.msg_offset == 3
    assert current.expire_ms == 10_000 + 600_000


def test_sweep_returns_expired_and_keeps_active():
    """Only the elapsed declaration is handed to the keeper."""
    table = CacheDeclarationTable()
    table.declare("short", DECL_TYPE_EPHEMERAL, 60, now_ms=0)
    table.declare("long", DECL_TYPE_PIN, 600, now_ms=0)

    assert table.sweep(now_ms=59_999) == []
    expired = table.sweep(now_ms=60_000)
    assert [decl.session_id for decl in expired] == ["short"]
    assert table.get("short", now_ms=60_000) is None
    assert table.get("long", now_ms=60_000) is not None
    assert len(table) == 1


def test_sweep_orders_by_expiry_and_drains():
    """Keeper processes oldest expiry first, and a second sweep is empty."""
    table = CacheDeclarationTable()
    table.declare("b", DECL_TYPE_PIN, 120, now_ms=0)
    table.declare("a", DECL_TYPE_PIN, 60, now_ms=0)

    expired = table.sweep(now_ms=600_000)
    assert [decl.session_id for decl in expired] == ["a", "b"]
    assert table.sweep(now_ms=600_000) == []
    assert table.pending_release_count == 0


def test_lazy_expiry_does_not_lose_the_release():
    """Regression: a get() that trips the TTL must queue the release, not swallow it.

    The request path may read a declaration long before the keeper's next tick; if the
    lazy path simply deleted it, that session's affinity pin would never be released.
    """
    table = CacheDeclarationTable()
    table.declare("s-1", DECL_TYPE_EPHEMERAL, 60, now_ms=0)

    assert table.get("s-1", now_ms=60_000) is None
    assert len(table) == 0
    assert table.pending_release_count == 1

    expired = table.sweep(now_ms=60_001)
    assert [decl.session_id for decl in expired] == ["s-1"]


def test_redeclare_cancels_pending_release():
    """A client asking again for the same prefix must not have it released underneath it."""
    table = CacheDeclarationTable()
    table.declare("s-1", DECL_TYPE_EPHEMERAL, 60, now_ms=0)
    table.get("s-1", now_ms=60_000)  # expires -> pending

    table.declare("s-1", DECL_TYPE_PIN, 600, now_ms=60_001)
    assert table.pending_release_count == 0
    assert table.sweep(now_ms=60_002) == []
    assert table.get("s-1", now_ms=60_002) is not None


def test_drop_forgets_active_and_pending():
    """finalize forgets the session outright, so the keeper cannot release it twice."""
    table = CacheDeclarationTable()
    table.declare("s-1", DECL_TYPE_PIN, 60, now_ms=0)
    assert table.drop("s-1") is True
    assert table.drop("s-1") is False

    table.declare("s-2", DECL_TYPE_PIN, 60, now_ms=0)
    table.get("s-2", now_ms=60_000)  # expires -> pending
    assert table.drop("s-2") is True
    assert table.pending_release_count == 0
    assert table.sweep(now_ms=60_001) == []


def test_get_does_not_refresh_recency():
    """Regression: reads must not reorder LRU, or a stale declaration outlives newer ones."""
    table = CacheDeclarationTable(max_declarations=2)
    table.declare("old", DECL_TYPE_PIN, 600, now_ms=0)
    table.declare("new", DECL_TYPE_PIN, 600, now_ms=1_000)

    assert table.get("old", now_ms=1_000) is not None
    table.declare("newest", DECL_TYPE_PIN, 600, now_ms=2_000)

    assert table.get("old", now_ms=2_000) is None
    assert table.get("new", now_ms=2_000) is not None
    assert table.get("newest", now_ms=2_000) is not None


def test_capacity_eviction_keeps_newest():
    """Least-recently-declared is evicted first; eviction drops bookkeeping only."""
    table = CacheDeclarationTable(max_declarations=2)
    table.declare("a", DECL_TYPE_PIN, 600, now_ms=0)
    table.declare("b", DECL_TYPE_PIN, 600, now_ms=1)
    table.declare("c", DECL_TYPE_PIN, 600, now_ms=2)

    assert len(table) == 2
    assert table.get("a", now_ms=3) is None
    assert table.get("b", now_ms=3) is not None
    assert table.get("c", now_ms=3) is not None
    # An evicted declaration is bookkeeping-only: no release is queued for it.
    assert table.sweep(now_ms=3) == []


def test_update_limits_shrinks_capacity():
    """Hot reload applies immediately and evicts down to the new bound."""
    table = CacheDeclarationTable(max_declarations=8)
    for index in range(5):
        table.declare(f"s-{index}", DECL_TYPE_PIN, 600, now_ms=index)

    table.update_limits(2, 3600, now_ms=5)

    assert len(table) == 2
    assert table.get("s-4", now_ms=10) is not None
    assert table.get("s-3", now_ms=10) is not None
    assert table.get("s-0", now_ms=10) is None


def test_malformed_input_is_rejected_without_raising():
    """Malformed declarations degrade silently; they never fail a request."""
    table = CacheDeclarationTable()

    assert table.declare("", DECL_TYPE_PIN, 60) is None
    assert table.declare(None, DECL_TYPE_PIN, 60) is None
    assert table.declare("s", "forever", 60) is None
    assert table.declare("s", DECL_TYPE_PIN, "abc") is None
    assert table.declare("s", DECL_TYPE_PIN, None) is None
    assert table.declare("s", DECL_TYPE_PIN, True) is None
    assert table.get("") is None
    assert table.get(None) is None
    assert table.drop("") is False
    assert len(table) == 0
    assert table.sweep() == []


def test_bad_msg_offset_degrades_to_none():
    """Range is advisory here: a malformed offset must not drop the whole declaration."""
    table = CacheDeclarationTable()
    decl = table.declare("s", DECL_TYPE_PIN, 60, msg_offset="3")

    assert decl is not None
    assert decl.msg_offset is None
    assert decl.ttl_s == 60


def test_snapshot_is_most_recent_first_and_detached():
    """Diagnostics read a copy, newest first, without touching the live table."""
    table = CacheDeclarationTable()
    table.declare("a", DECL_TYPE_PIN, 60, now_ms=0)
    table.declare("b", DECL_TYPE_PIN, 60, now_ms=1)

    snapshot = table.snapshot()
    assert [decl.session_id for decl in snapshot] == ["b", "a"]
    snapshot.clear()
    assert len(table) == 2


def test_pending_release_queue_is_bounded():
    """A keeper that never runs must not let the pending queue grow without limit."""
    table = CacheDeclarationTable(max_declarations=2)
    for index in range(5):
        table.declare(f"s-{index}", DECL_TYPE_EPHEMERAL, 60, now_ms=index)

    assert table.sweep(now_ms=600_000) != []
    # The earlier five were capacity-evicted (no release queued); only the two
    # survivors expire here, and the sweep drains them in the same call.
    assert table.pending_release_count == 0


def test_factory_is_off_unless_enabled():
    """The feature must be inert on a baseline deployment."""
    assert create_cache_declaration_table_from_env() is None


def test_factory_enables_and_honors_knobs(monkeypatch):
    """Enabling the flag wires the capacity and TTL ceiling through to the table."""
    monkeypatch.setenv(LIFECYCLE_ENV_FLAG, "1")
    monkeypatch.setenv(LIFECYCLE_MAX_ENV, "16")
    monkeypatch.setenv(LIFECYCLE_TTL_MAX_ENV, "120")

    table = create_cache_declaration_table_from_env()

    assert table is not None
    assert table.max_declarations == 16
    assert table.ttl_max_s == 120
    assert table.declare("s", DECL_TYPE_PIN, 600, now_ms=0).ttl_s == 120


@pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
def test_factory_accepts_truthy_flag_spellings(monkeypatch, value):
    """Operators write flags in several ways; only an explicit off/absent is off."""
    monkeypatch.setenv(LIFECYCLE_ENV_FLAG, value)

    assert create_cache_declaration_table_from_env() is not None


@pytest.mark.parametrize("value", ["0", "false", "no"])
def test_factory_treats_falsey_flag_as_off(monkeypatch, value):
    """An explicit false must not enable the feature."""
    monkeypatch.setenv(LIFECYCLE_ENV_FLAG, value)

    assert create_cache_declaration_table_from_env() is None


@pytest.mark.parametrize("bad", ["abc", "0", "-4"])
def test_factory_falls_back_on_invalid_knobs(monkeypatch, bad):
    """A malformed knob degrades to the default instead of failing startup."""
    monkeypatch.setenv(LIFECYCLE_ENV_FLAG, "1")
    monkeypatch.setenv(LIFECYCLE_MAX_ENV, bad)
    monkeypatch.setenv(LIFECYCLE_TTL_MAX_ENV, bad)

    table = create_cache_declaration_table_from_env()

    assert table is not None
    assert table.max_declarations == 65536
    assert table.ttl_max_s == 3600


def test_sweep_interval_defaults_and_falls_back(monkeypatch):
    """The keeper period defaults to 5s and never drops below the floor."""
    assert cache_lifecycle_sweep_interval_s() == 5

    monkeypatch.setenv(LIFECYCLE_SWEEP_ENV, "30")
    assert cache_lifecycle_sweep_interval_s() == 30

    for bad in ("0", "-1", "abc"):
        monkeypatch.setenv(LIFECYCLE_SWEEP_ENV, bad)
        assert cache_lifecycle_sweep_interval_s() == 5


def test_declare_accumulates_roles_across_renewals():
    """Protection must cover every role pool the session was scheduled in."""
    table = CacheDeclarationTable()
    table.declare("s-1", "pin", 600, role="prefill", now_ms=0)
    table.declare("s-1", "pin", 600, role="decode", now_ms=1)
    table.declare("s-1", "pin", 600, role="prefill", now_ms=2)  # duplicate role

    declaration = table.get("s-1", now_ms=3)
    assert declaration is not None
    assert declaration.roles == ("prefill", "decode")


def test_declare_ignores_blank_role():
    """A blank role is not storable, but must not reject the declaration."""
    table = CacheDeclarationTable()
    declaration = table.declare("s-1", "pin", 600, role="", now_ms=0)

    assert declaration is not None
    assert declaration.roles == ()


def test_active_excludes_expired_and_keeps_order():
    """``active`` is the keeper's protection worklist: live declarations, newest first."""
    table = CacheDeclarationTable()
    table.declare("gone", "ephemeral", 60, role="union", now_ms=0)
    table.declare("kept-a", "pin", 3600, role="union", now_ms=10)
    table.declare("kept-b", "pin", 3600, role="prefill", now_ms=20)

    active = table.active(now_ms=60_000)

    assert [declaration.session_id for declaration in active] == ["kept-b", "kept-a"]
    assert len(table) == 2
