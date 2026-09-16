# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO, THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# See the Mulan PSL v2 for more details.

"""Behavioral tests for the coordinator Program scheduler."""

from motor.coordinator.scheduler.program import (
    AdmissionDisposition,
    AdmissionRequest,
    CapacitySnapshot,
    ProgramIdentity,
    ProgramScheduler,
    ProgramSchedulerConfig,
    ProgressTTLMode,
)


def _request(
    request_id: str,
    program_id: str,
    prompt: int = 100,
    arrived_at: float = 0.0,
    shared_prefix: int = 0,
) -> AdmissionRequest:
    return AdmissionRequest(
        request_id=request_id,
        identity=ProgramIdentity(program_id),
        prompt_tokens=prompt,
        max_output_tokens=0,
        shared_prefix_tokens=shared_prefix,
        arrived_at=arrived_at,
    )


def _capacity(total: int = 300) -> CapacitySnapshot:
    return CapacitySnapshot(total_kv_tokens=total, safety_ratio=1.0)


def test_disabled_scheduler_bypasses_without_queueing():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=False))

    decision = scheduler.arrive(_request("r1", "p1"), _capacity())

    assert decision.disposition is AdmissionDisposition.BYPASS
    assert scheduler.queued_request_count == 0
    # Bypass mode intentionally does not create scheduler state.
    assert not scheduler.complete("r1", total_tokens=120, now=1.0)


def test_capacity_admission_queues_other_program_and_resumes_after_ttl_pause():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=True, ttl_seconds=10.0))

    first = scheduler.arrive(_request("r1", "p1"), _capacity())
    second = scheduler.arrive(_request("r2", "p2"), _capacity())

    assert first.disposition is AdmissionDisposition.ADMITTED
    assert second.disposition is AdmissionDisposition.QUEUED
    assert scheduler.snapshot()["queued_requests"] == 1

    assert scheduler.complete("r1", total_tokens=100, now=0.0)
    assert scheduler.snapshot()["acting_programs"] == 1
    resumed = scheduler.tick(_capacity(), now=10.0)

    assert [item.request_id for item in resumed] == ["r2"]
    assert scheduler.snapshot()["queued_requests"] == 0


def test_same_program_overlapping_requests_keep_reasoning_status():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=True))

    first = scheduler.arrive(_request("r1", "p1"), _capacity())
    second = scheduler.arrive(_request("r2", "p1", prompt=120), _capacity(500))

    assert first.disposition is AdmissionDisposition.ADMITTED
    assert second.disposition is AdmissionDisposition.ADMITTED
    assert scheduler.complete("r1", total_tokens=100, now=1.0)
    assert scheduler.snapshot()["reasoning_programs"] == 1
    assert scheduler.snapshot()["acting_programs"] == 0

    assert scheduler.complete("r2", total_tokens=120, now=2.0)
    assert scheduler.snapshot()["acting_programs"] == 1


def test_force_resume_bypasses_capacity_after_wait_deadline():
    config = ProgramSchedulerConfig(enabled=True, force_resume_timeout_seconds=5.0)
    scheduler = ProgramScheduler(config)
    scheduler.arrive(_request("r1", "p1", prompt=250), _capacity(400))
    queued = scheduler.arrive(_request("r2", "p2", prompt=250, arrived_at=0.0), _capacity(300))

    assert queued.disposition is AdmissionDisposition.QUEUED
    resumed = scheduler.tick(_capacity(300), now=6.0)

    assert [item.request_id for item in resumed] == ["r2"]


def test_cancel_queued_request_terminates_unserved_generation():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=True))
    scheduler.arrive(_request("r1", "p1", prompt=250), _capacity(400))
    queued = scheduler.arrive(_request("r2", "p2", prompt=250), _capacity(400))

    assert queued.program is not None
    assert scheduler.cancel("r2")
    assert scheduler.queued_request_count == 0
    assert scheduler.snapshot()["paused_programs"] == 0
    assert queued.program not in scheduler.programs


def test_completion_arms_acting_ttl_then_paused_retention_expires():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=True, ttl_seconds=2.0, paused_program_ttl_seconds=5.0))
    scheduler.arrive(_request("r1", "p1"), _capacity())
    scheduler.complete("r1", total_tokens=100, now=0.0)

    scheduler.tick(_capacity(), now=2.0)
    assert scheduler.snapshot()["paused_programs"] == 1
    scheduler.tick(_capacity(), now=7.0)
    assert scheduler.snapshot()["paused_programs"] == 0
    assert scheduler.programs == ()


def test_new_program_waits_behind_existing_queue():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=True, decode_buffer_tokens=0))
    scheduler.arrive(_request("active", "active", prompt=200), _capacity(300))
    queued = scheduler.arrive(_request("older", "older", prompt=200, arrived_at=1.0), _capacity(300))
    later = scheduler.arrive(_request("later", "later", prompt=1, arrived_at=2.0), _capacity(300))

    assert queued.disposition is AdmissionDisposition.QUEUED
    assert later.disposition is AdmissionDisposition.QUEUED
    assert later.reason == "queue_precedence"


def test_privilege_does_not_bypass_capacity():
    scheduler = ProgramScheduler(
        ProgramSchedulerConfig(
            enabled=True,
            decode_buffer_tokens=0,
            target_min_segment_rounds=1,
            target_max_segment_rounds=1,
        )
    )
    scheduler.arrive(_request("active", "active", prompt=200), _capacity(300))
    candidate = scheduler._materialize(ProgramIdentity("candidate"))
    candidate.segment_served_rounds = 1

    decision = scheduler.arrive(_request("r2", "candidate", prompt=200), _capacity(300))

    assert decision.disposition is AdmissionDisposition.QUEUED
    assert not candidate.is_privileged


def test_expired_shared_prefix_is_not_counted_for_admission():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=True, ttl_seconds=1.0, decode_buffer_tokens=0))
    first = scheduler.arrive(_request("r1", "p1", prompt=200, shared_prefix=150), _capacity(300))
    assert first.program is not None
    scheduler.complete("r1", total_tokens=200, now=0.0)
    scheduler._programs[first.program].shared_prefix_fresh_until = 1.0
    scheduler.tick(_capacity(300), now=1.0)

    decision = scheduler.arrive(_request("r2", "p1", prompt=200, arrived_at=2.0), _capacity(180))

    assert decision.disposition is AdmissionDisposition.QUEUED


def test_max_segment_rounds_do_not_bypass_capacity():
    scheduler = ProgramScheduler(
        ProgramSchedulerConfig(
            enabled=True, decode_buffer_tokens=0, target_min_segment_rounds=1, target_max_segment_rounds=1
        )
    )
    first = scheduler.arrive(_request("r1", "p1", prompt=200), _capacity(300))
    assert first.program is not None
    scheduler.complete("r1", total_tokens=200, now=0.0)
    queued = scheduler.arrive(_request("r2", "p2", prompt=200, arrived_at=1.0), _capacity(300))

    assert queued.disposition is AdmissionDisposition.QUEUED
    assert queued.program is not None
    scheduler._programs[queued.program].segment_served_rounds = 1
    assert scheduler.tick(_capacity(300), now=2.0) == ()


def test_cancelled_paused_program_is_removed_and_next_arrival_advances_generation():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=True, decode_buffer_tokens=0))
    scheduler.arrive(_request("r1", "active", prompt=200), _capacity(300))
    queued = scheduler.arrive(_request("r2", "p1", prompt=200), _capacity(300))
    assert queued.program is not None

    assert scheduler.cancel("r2")
    assert queued.program not in scheduler.programs
    replacement = scheduler.arrive(_request("r3", "p1", prompt=1), _capacity(300))

    assert replacement.program is not None
    assert replacement.program.generation == queued.program.generation + 1


def test_auto_mode_records_theoretical_ttl_without_initial_retention():
    scheduler = ProgramScheduler(
        ProgramSchedulerConfig(
            enabled=True,
            mode=ProgressTTLMode.AUTO,
            rolling_window_size=2,
            ttl_seconds=4.0,
        )
    )
    assert scheduler.arrive(_request("r1", "p1"), _capacity(500)).disposition is AdmissionDisposition.ADMITTED
    assert scheduler.complete("r1", total_tokens=100, now=1.0)
    # AUTO starts fail-open but keeps continuity observations for later hysteresis.
    assert scheduler.snapshot()["continuity_window_samples"] == 1


def test_related_program_inherits_bounded_privilege():
    scheduler = ProgramScheduler(
        ProgramSchedulerConfig(enabled=True, privileged_max_context_tokens=100, privileged_lookahead_rounds=4)
    )
    parent = scheduler.arrive(_request("r1", "parent", prompt=50), _capacity(500))
    assert parent.disposition is AdmissionDisposition.ADMITTED
    scheduler.complete("r1", total_tokens=50, now=1.0)
    scheduler.tick(_capacity(500), now=1.1)
    # A child arrival with the parent relationship transfers the acting slot.
    child_request = AdmissionRequest(
        request_id="r2",
        identity=ProgramIdentity("child", parent_program_id="parent"),
        prompt_tokens=50,
        arrived_at=2.0,
    )
    child = scheduler.arrive(child_request, _capacity(500))
    assert child.disposition is AdmissionDisposition.ADMITTED
    assert scheduler.snapshot()["privileged_programs"] == 1


def test_capacity_repair_pauses_idle_ordinary_program():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=True, decode_buffer_tokens=0))
    scheduler.arrive(_request("r1", "p1", prompt=200), _capacity(500))
    scheduler.complete("r1", total_tokens=200, now=1.0)
    # Native usage leaves no pause headroom; the acting program is reclaimed.
    scheduler.tick(CapacitySnapshot(total_kv_tokens=500, native_used_kv_tokens=501, safety_ratio=1.0), now=2.0)
    assert scheduler.snapshot()["paused_programs"] == 1


def test_terminal_release_removes_idle_generation_and_allows_new_generation():
    scheduler = ProgramScheduler(ProgramSchedulerConfig(enabled=True))
    admitted = scheduler.arrive(_request("r1", "p1"), _capacity(500))
    assert admitted.program is not None
    scheduler.complete("r1", total_tokens=100, now=1.0)
    assert scheduler.release("p1", admitted.program.generation)
    replacement = scheduler.arrive(_request("r2", "p1"), _capacity(500))
    assert replacement.program is not None
    assert replacement.program.generation == admitted.program.generation + 1
