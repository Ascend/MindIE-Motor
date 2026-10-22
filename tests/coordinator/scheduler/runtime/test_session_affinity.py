# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Unit tests for the session affinity table and pin resolution."""

import pytest

from motor.coordinator.scheduler.runtime.session_affinity import (
    AFFINITY_ENV_FLAG,
    AFFINITY_MAX_ENV,
    AFFINITY_PLACEMENT_ENV,
    NEW_PIN_OUTCOMES,
    PLACEMENT_BALANCED,
    PLACEMENT_POLICY,
    SessionAffinityTable,
    create_session_affinity_from_env,
    resolve_session_pin,
    select_balanced_instance,
    session_affinity_placement_mode,
)

ROLE_U = "union"
ROLE_P = "prefill"
ROLE_D = "decode"


def test_get_returns_none_for_unknown_or_invalid_session():
    """Unknown or blank session ids never map to an instance."""
    table = SessionAffinityTable()
    table.record("s-1", 7, ROLE_U)

    assert table.get("s-missing", ROLE_U) is None
    assert table.get("", ROLE_U) is None
    assert table.get(None, ROLE_U) is None


def test_record_then_get_roundtrip():
    table = SessionAffinityTable()
    table.record("s-1", 7, ROLE_U)

    assert table.get("s-1", ROLE_U) is not None
    assert table.get("s-1", ROLE_U).instance_id == 7
    assert table.get("s-1", ROLE_U).epoch is None
    assert len(table) == 1


def test_record_stores_epoch():
    table = SessionAffinityTable()
    table.record("s-1", 7, ROLE_U, epoch=3)

    assert table.get("s-1", ROLE_U).epoch == 3


def test_role_scoping_isolates_pools():
    """The same session id pins independently per role (P/D split)."""
    table = SessionAffinityTable()
    table.record("s-1", 10, ROLE_P)
    table.record("s-1", 20, ROLE_D)

    assert table.get("s-1", ROLE_P).instance_id == 10
    assert table.get("s-1", ROLE_D).instance_id == 20
    assert table.get("s-1", ROLE_U) is None
    assert len(table) == 2


def test_role_scoped_drop_only_removes_one_role():
    table = SessionAffinityTable()
    table.record("s-1", 10, ROLE_P)
    table.record("s-1", 20, ROLE_D)
    table.drop("s-1", ROLE_P)

    assert table.get("s-1", ROLE_P) is None
    assert table.get("s-1", ROLE_D).instance_id == 20


def test_lru_eviction_drops_least_recently_used():
    """Beyond max_entries the least-recently-*touched* entry is evicted."""
    table = SessionAffinityTable(max_entries=2)
    table.record("s-a", 1, ROLE_U)
    table.record("s-b", 2, ROLE_U)
    assert table.get("s-a", ROLE_U) is not None

    table.record("s-c", 3, ROLE_U)

    assert table.get("s-b", ROLE_U) is None
    assert table.get("s-a", ROLE_U) is not None
    assert table.get("s-c", ROLE_U) is not None
    assert len(table) == 2


def test_record_refreshes_existing_entry():
    """Re-recording updates the pin and refreshes recency."""
    table = SessionAffinityTable(max_entries=2)
    table.record("s-a", 1, ROLE_U)
    table.record("s-b", 2, ROLE_U)
    table.record("s-a", 9, ROLE_U)
    table.record("s-c", 3, ROLE_U)

    assert table.get("s-a", ROLE_U).instance_id == 9
    assert table.get("s-b", ROLE_U) is None


def test_drop_removes_entry_and_tolerates_missing():
    table = SessionAffinityTable()
    table.record("s-1", 7, ROLE_U)
    table.drop("s-1", ROLE_U)
    table.drop("s-missing", ROLE_U)

    assert table.get("s-1", ROLE_U) is None
    assert len(table) == 0


def test_max_entries_floored_to_one():
    table = SessionAffinityTable(max_entries=0)
    table.record("s-a", 1, ROLE_U)
    table.record("s-b", 2, ROLE_U)

    assert len(table) == 1
    assert table.get("s-b", ROLE_U) is not None


# ---------------------------------------------------------------------------
# resolve_session_pin
# ---------------------------------------------------------------------------


def test_resolve_none_when_table_absent_or_no_identity():
    """Disabled affinity (no table) or identity-free requests never pin."""
    table = SessionAffinityTable()

    assert resolve_session_pin(None, "s-1", None, None, ROLE_U, {1}).outcome == "none"
    assert resolve_session_pin(table, None, None, None, ROLE_U, {1}).outcome == "none"


def test_resolve_hit_on_own_pin():
    table = SessionAffinityTable()
    table.record("s-1", 7, ROLE_U, epoch=0)

    decision = resolve_session_pin(table, "s-1", None, 0, ROLE_U, {7, 8})

    assert (decision.instance_id, decision.outcome) == (7, "hit")


def test_resolve_hit_when_request_has_no_epoch():
    """A request without epoch cannot invalidate an epoch-carrying pin."""
    table = SessionAffinityTable()
    table.record("s-1", 7, ROLE_U, epoch=2)

    assert resolve_session_pin(table, "s-1", None, None, ROLE_U, {7}).instance_id == 7


def test_resolve_epoch_mismatch_invalidates_and_falls_fresh():
    """A session.epoch bump drops the pin; the request goes through normal policy."""
    table = SessionAffinityTable()
    table.record("s-1", 7, ROLE_U, epoch=0)

    decision = resolve_session_pin(table, "s-1", None, 1, ROLE_U, {7, 8})

    assert (decision.instance_id, decision.outcome) == (None, "epoch")
    assert table.get("s-1", ROLE_U) is None


def test_resolve_epoch_mismatch_still_propagates_from_parent():
    """Child context rewrite does not sever the parent's shared-prefix link."""
    table = SessionAffinityTable()
    table.record("s-child", 7, ROLE_U, epoch=0)
    table.record("s-parent", 7, ROLE_U, epoch=0)

    decision = resolve_session_pin(table, "s-child", "s-parent", 1, ROLE_U, {7})

    assert (decision.instance_id, decision.outcome) == (7, "parent")
    assert table.get("s-child", ROLE_U) is None


def test_resolve_stale_pin_dropped_and_falls_fresh():
    """A pin to an instance outside the eligible set is dropped."""
    table = SessionAffinityTable()
    table.record("s-1", 7, ROLE_U)

    decision = resolve_session_pin(table, "s-1", None, None, ROLE_U, {8, 9})

    assert (decision.instance_id, decision.outcome) == (None, "stale")
    assert table.get("s-1", ROLE_U) is None


def test_resolve_stale_own_pin_falls_back_to_parent_pin():
    table = SessionAffinityTable()
    table.record("s-child", 7, ROLE_U)
    table.record("s-parent", 8, ROLE_U)

    decision = resolve_session_pin(table, "s-child", "s-parent", None, ROLE_U, {8})

    assert (decision.instance_id, decision.outcome) == (8, "parent")
    assert table.get("s-child", ROLE_U) is None
    assert table.get("s-parent", ROLE_U) is not None


def test_resolve_parent_pin_propagates_when_own_missing():
    """A fresh child session inherits its parent's instance."""
    table = SessionAffinityTable()
    table.record("s-parent", 8, ROLE_U)

    decision = resolve_session_pin(table, "s-child", "s-parent", None, ROLE_U, {7, 8})

    assert (decision.instance_id, decision.outcome) == (8, "parent")


def test_resolve_parent_pin_respects_role_scoping():
    """Parent propagation only sees pins from the same role's pool namespace."""
    table = SessionAffinityTable()
    table.record("s-parent", 8, ROLE_P)

    decision = resolve_session_pin(table, "s-child", "s-parent", None, ROLE_D, {8})

    assert (decision.instance_id, decision.outcome) == (None, "fresh")


def test_resolve_fresh_when_no_pin_exists():
    table = SessionAffinityTable()

    assert resolve_session_pin(table, "s-1", "s-parent", None, ROLE_U, {7}).outcome == "fresh"


def test_resolve_does_not_leak_pins_across_eligible_sets():
    """An ineligible parent pin is kept (not dropped) — only own pins are dropped."""
    table = SessionAffinityTable()
    table.record("s-parent", 99, ROLE_U)

    decision = resolve_session_pin(table, "s-child", "s-parent", None, ROLE_U, {7})

    assert (decision.instance_id, decision.outcome) == (None, "fresh")
    assert table.get("s-parent", ROLE_U) is not None


# ---------------------------------------------------------------------------
# env construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "0", "true", " on ", "1x"])
def test_create_from_env_disabled_returns_none(value, monkeypatch):
    if value is None:
        monkeypatch.delenv(AFFINITY_ENV_FLAG, raising=False)
    else:
        monkeypatch.setenv(AFFINITY_ENV_FLAG, value)
    assert create_session_affinity_from_env() is None


@pytest.mark.parametrize("value", ["1", " 1 "])
def test_create_from_env_enabled_returns_empty_table(value, monkeypatch):
    monkeypatch.setenv(AFFINITY_ENV_FLAG, value)
    table = create_session_affinity_from_env()

    assert isinstance(table, SessionAffinityTable)
    assert len(table) == 0


def test_create_from_env_max_entries_applies(monkeypatch):
    monkeypatch.setenv(AFFINITY_ENV_FLAG, "1")
    monkeypatch.setenv(AFFINITY_MAX_ENV, "2")
    table = create_session_affinity_from_env()
    for i in range(4):
        table.record(f"s-{i}", i, ROLE_U)

    assert len(table) == 2
    assert table.get("s-2", ROLE_U) is not None
    assert table.get("s-3", ROLE_U) is not None


def test_create_from_env_invalid_max_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(AFFINITY_ENV_FLAG, "1")
    monkeypatch.setenv(AFFINITY_MAX_ENV, "abc")
    table = create_session_affinity_from_env()

    assert isinstance(table, SessionAffinityTable)
    table.record("s-1", 1, ROLE_U)
    assert table.get("s-1", ROLE_U) is not None


def test_drop_all_roles_releases_every_role_pin():
    """Out-of-band finalize cleanup: one call forgets P/U/D pins (Phase 4)."""
    table = SessionAffinityTable()
    table.record("s-1", 1, ROLE_U, epoch=0)
    table.record("s-1", 2, ROLE_P, epoch=1)
    table.record("s-1", 3, ROLE_D)

    dropped = table.drop_all_roles("s-1")
    assert dropped == 3
    assert table.get("s-1", ROLE_U) is None
    assert table.get("s-1", ROLE_P) is None
    assert table.get("s-1", ROLE_D) is None
    assert len(table) == 0


def test_drop_all_roles_keeps_other_sessions_and_roles():
    table = SessionAffinityTable()
    table.record("s-1", 1, ROLE_U)
    table.record("s-2", 2, ROLE_U)
    table.record("s-2", 3, ROLE_D)

    assert table.drop_all_roles("s-1") == 1
    assert table.get("s-2", ROLE_U).instance_id == 2
    assert table.get("s-2", ROLE_D).instance_id == 3
    assert table.drop_all_roles("s-1") == 0  # idempotent


def test_drop_all_roles_invalid_inputs_noop():
    table = SessionAffinityTable()
    table.record("s-1", 1, ROLE_U)
    assert table.drop_all_roles("") == 0
    assert table.drop_all_roles(None) == 0
    assert table.drop_all_roles("s-missing") == 0
    assert table.get("s-1", ROLE_U).instance_id == 1


# ---------------------------------------------------------------------------
# balanced placement: count_pins_by_instance / select_balanced_instance / mode
# ---------------------------------------------------------------------------


def test_count_pins_by_instance_counts_per_role_pool():
    table = SessionAffinityTable()
    table.record("s-1", 1, ROLE_U)
    table.record("s-2", 1, ROLE_U)
    table.record("s-3", 2, ROLE_U)
    table.record("s-4", 9, ROLE_D)

    assert table.count_pins_by_instance(ROLE_U) == {1: 2, 2: 1}
    assert table.count_pins_by_instance(ROLE_D) == {9: 1}
    assert table.count_pins_by_instance(ROLE_P) == {}


def test_count_pins_by_instance_empty_table():
    assert SessionAffinityTable().count_pins_by_instance(ROLE_U) == {}


def test_select_balanced_instance_prefers_fewest_pinned_sessions():
    """Primary key: stickiness commitment spreads staggered session arrivals."""
    chosen = select_balanced_instance({1, 2}, {1: 5, 2: 3}, {1: 0.0, 2: 9000.0}, placement_seq=0)

    assert chosen == 2


def test_select_balanced_instance_tokens_break_pin_ties():
    """Secondary key: equal pins go to the engine with lighter in-flight load."""
    chosen = select_balanced_instance({1, 2}, {1: 4, 2: 4}, {1: 100.0, 2: 5000.0}, placement_seq=7)

    assert chosen == 1


def test_select_balanced_instance_seq_spreads_full_tie_deterministically():
    """Burst tie-break: monotonic seq alternates tied candidates (8 fresh → 4/4)."""
    picks = [select_balanced_instance({1, 2}, {}, {}, placement_seq=seq) for seq in range(8)]

    assert picks == [1, 2, 1, 2, 1, 2, 1, 2]


def test_select_balanced_instance_missing_metrics_default_to_zero():
    """Instances absent from the metric maps participate as zero load."""
    assert select_balanced_instance({1, 2}, {2: 1}, {}, placement_seq=0) == 1
    assert select_balanced_instance({1, 2}, {}, {1: 7.5}, placement_seq=0) == 2


def test_select_balanced_instance_single_and_empty_candidates():
    assert select_balanced_instance({3}, {3: 9}, {3: 999.0}, placement_seq=0) == 3
    assert select_balanced_instance(set(), {}, {}, placement_seq=0) is None


def test_record_balanced_without_session_id_returns_none():
    """Review (PR #1196): a fresh decision without an own session id (only an
    absent parent pin) has nothing to pin; return None so the request falls
    back to the policy instead of committing to an off-table instance.
    """
    table = SessionAffinityTable()
    assert table.record_balanced(None, [1, 2], ROLE_U, None, lambda: {}, 0) is None
    assert table.record_balanced("", [1, 2], ROLE_U, None, lambda: {}, 0) is None
    assert len(table) == 0


def test_new_pin_outcomes_cover_repin_paths():
    """stale/epoch also create new pins, so balanced placement must cover them."""
    assert NEW_PIN_OUTCOMES == {"fresh", "stale", "epoch"}


def test_placement_mode_default_and_balanced(monkeypatch):
    monkeypatch.delenv(AFFINITY_PLACEMENT_ENV, raising=False)
    assert session_affinity_placement_mode() == PLACEMENT_POLICY

    monkeypatch.setenv(AFFINITY_PLACEMENT_ENV, "balanced")
    assert session_affinity_placement_mode() == PLACEMENT_BALANCED

    monkeypatch.setenv(AFFINITY_PLACEMENT_ENV, " BALANCED ")
    assert session_affinity_placement_mode() == PLACEMENT_BALANCED


def test_placement_mode_invalid_value_falls_back_to_policy(monkeypatch):
    monkeypatch.setenv(AFFINITY_PLACEMENT_ENV, "round_robin")
    assert session_affinity_placement_mode() == PLACEMENT_POLICY
